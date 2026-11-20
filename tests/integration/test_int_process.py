"""GENUINE process interruption (SIGKILL of real OS processes), as opposed to the simulated
exceptions/cancellations elsewhere:

* the API process is killed after its transaction committed but before the response left
  (a really lost response), on a database migrated from EMPTY with the Alembic CLI;
* a worker process is killed while its real mTLS request is inside the (loopback, fake)
  8028 gateway, and another is killed after claiming but before beginning an attempt.

Children use only loopback, generated keys/certificates and 0900000xxx numbers.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from itsupport import (
    OTP_TEXT,
    ROOT,
    Certs,
    FakeClock,
    Harness,
    Keys,
    TlsFakeProvider,
    build_harness,
    create_database,
    drop_database,
    fake_number,
    hdr,
    libpq,
    make_caller,
    needle_forms,
    new_key,
    sms_body,
)

from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.worker.rate_limit import RATE_LOCK_CLASS

pytestmark = pytest.mark.postgres
HERE = Path(__file__).resolve().parent
LEASE = 8.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def child_env(url: str, keys: Keys, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "OAN_MSG_DATABASE_URL": url,
        **keys.env(),
        **extra,
    }
    return env


class Proc:
    def __init__(self, args: list[str], env: dict[str, str], log: Path) -> None:
        self.log = log
        self._fh = log.open("wb")
        self.p = subprocess.Popen(  # noqa: S603 (fixed interpreter + harness file)
            [sys.executable, *args], env=env, cwd=ROOT, stdout=self._fh, stderr=subprocess.STDOUT
        )

    def kill(self) -> None:
        if self.p.poll() is None:
            os.kill(self.p.pid, signal.SIGKILL)
        self.p.wait(10)
        self._fh.close()

    def output(self) -> str:
        return self.log.read_text(errors="replace")


@pytest.fixture
def procs() -> Iterator[list[Proc]]:
    started: list[Proc] = []
    yield started
    for p in started:
        p.kill()


def wait_until(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def scalar(url: str, sql: str, *params: object) -> object:
    with psycopg.connect(libpq(url)) as conn:
        row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]


def assert_no_content_in(text: str, keys: Keys, numbers: list[str]) -> None:
    for needle in [OTP_TEXT, *keys.secret_needles()] + [
        f for n in numbers for f in needle_forms(n)
    ]:
        assert needle not in text, "subprocess output leaked content or a secret"


# ------------------------------------------------------------------------ API


async def test_api_process_killed_after_commit_before_response_then_replayed(
    admin_url: str, keys: Keys, tmp_path: Path, procs: list[Proc]
) -> None:
    url = create_database(admin_url, "oan_msg_itproc")
    try:
        # Migration from an EMPTY database through the Alembic CLI (deployment path).
        assert scalar(url, "SELECT count(*) FROM pg_tables WHERE schemaname='public'") == 0
        mig = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
            cwd=ROOT,
            env=child_env(url, keys),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert mig.returncode == 0, mig.stderr[-500:]
        assert scalar(url, "SELECT version_num FROM alembic_version") == "0005"

        engine = make_engine(url)
        sf = make_session_factory(engine)
        token = await make_caller(sf, keys, "esignet", bot=None)
        port = free_port()
        env = child_env(url, keys)

        def start_api(name: str) -> Proc:
            p = Proc([str(HERE / "_proc_api.py"), str(port)], env, tmp_path / f"{name}.log")
            procs.append(p)

            def up() -> bool:
                try:
                    return (
                        httpx.get(f"http://127.0.0.1:{port}/api/v1/messages/x").status_code == 401
                    )
                except httpx.HTTPError:
                    return False

            wait_until(up, 30, "API process to start")
            return p

        first = start_api("api-1")
        now = datetime.now(UTC)
        body = sms_body(lambda: now)
        key = new_key()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=3.0) as c:
            with pytest.raises(httpx.ReadTimeout):
                await c.post(
                    "/api/v1/messages/sms",
                    json=body,
                    headers={**hdr(token, key), "X-It-Hold-Response": "1"},
                )
        # The caller never saw a response, yet the delivery is durably stored.
        assert (
            scalar(url, "SELECT status FROM messages WHERE idempotency_key=%s", key) == "accepted"
        )
        mid = str(scalar(url, "SELECT id FROM messages WHERE idempotency_key=%s", key))
        assert "IT-RESPONSE-WITHHELD" in first.output()
        first.kill()  # genuine process death between commit and response
        assert first.p.returncode == -signal.SIGKILL

        second = start_api("api-2")
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
            r = await c.post("/api/v1/messages/sms", json=body, headers=hdr(token, key))
            assert (r.status_code, r.json()["id"]) == (200, mid)
            changed = {**body, "text": "Your OAN code is 000000."}
            assert (
                await c.post("/api/v1/messages/sms", json=changed, headers=hdr(token, key))
            ).status_code == 409
            view = (await c.get(f"/api/v1/messages/{mid}", headers=hdr(token))).json()
            assert view["status"] == "accepted"
        assert scalar(url, "SELECT count(*) FROM messages") == 1

        # One gateway submission for the delivery (in-process worker, mocked HTTP).
        clock = FakeClock(datetime.now(UTC))
        h = build_harness(engine, sf, keys, clock)
        try:
            await h.worker().run_once()
            assert len(h.sms.calls) == 1
        finally:
            await h.close()
        await engine.dispose()
        second.kill()
        for p in (first, second):
            assert_no_content_in(p.output(), keys, [fake_number(101)])
            assert token not in p.output()
    finally:
        drop_database(admin_url, url)


# --------------------------------------------------------------------- worker


def worker_env(url: str, keys: Keys, certs: Certs, gw: TlsFakeProvider, wid: str) -> dict[str, str]:
    return child_env(
        url,
        keys,
        OAN_MSG_SMS8028_CLIENT_SECRET=keys.sms_secret,
        OAN_MSG_SMS8028_CLIENT_CERT=str(certs["gwclient_cert"]),
        OAN_MSG_SMS8028_CLIENT_KEY=str(certs["gwclient_key"]),
        OAN_MSG_SMS8028_CA_CERT=str(certs["ca_cert"]),
        OAN_MSG_SMS8028_BASE_URL=f"https://localhost:{gw.port}/sms-api/v1",
        IT_LEASE_SECONDS=str(LEASE),
        IT_WORKER_ID=wid,
    )


async def _accept_now(h: Harness) -> tuple[str, str]:
    h.clock.now = datetime.now(UTC)
    token = await h.caller("esignet", bot=None)
    mid = (await h.post_sms(token, new_key())).json()["id"]
    return token, mid


async def test_worker_killed_while_gateway_holds_request_is_unknown_never_resent(
    h: Harness, certs: Certs, database_url: str, tmp_path: Path, procs: list[Proc]
) -> None:
    token, mid = await _accept_now(h)
    gw = TlsFakeProvider(certs, require_client_cert=True, mode="hang")
    try:
        w = Proc(
            [str(HERE / "_proc_worker.py")],
            worker_env(database_url, h.keys, certs, gw, "pk-1"),
            tmp_path / "w1.log",
        )
        procs.append(w)
        assert await asyncio.to_thread(gw.got_request.wait, 30), w.output()[-2000:]
        # The real adapter reached the gateway over verified mTLS with the right body.
        path, body = gw.requests[0]
        assert path == "/sms-api/v1/sms/send-sms"
        assert body["smstext"] == OTP_TEXT and body["phonenums"] == [fake_number(101)]
        w.kill()  # genuine death after the request reached the provider
        assert w.p.returncode == -signal.SIGKILL
        assert (await h.status(token, mid))["status"] == "sending"

        gw.mode = "ok"
        gw.release.set()
        # A replacement worker process after the lease: recovers to unknown, never resends.
        await asyncio.sleep(LEASE + 0.5)
        w2 = Proc(
            [str(HERE / "_proc_worker.py")],
            worker_env(database_url, h.keys, certs, gw, "pk-2"),
            tmp_path / "w2.log",
        )
        procs.append(w2)
        await asyncio.to_thread(
            wait_until,
            lambda: (
                scalar(database_url, "SELECT status FROM messages WHERE id=%s", uuid.UUID(mid))
                == "unknown"
            ),
            20,
            "recovery to unknown",
        )
        await asyncio.sleep(1.5)  # several more worker cycles
        w2.kill()
    finally:
        gw.close()
    view = await h.status(token, mid)
    assert (view["status"], view["failure_code"], view["attempts"]) == ("unknown", "interrupted", 1)
    assert len(gw.requests) == 1
    assert (await h.one("SELECT count(*) FROM message_content"))[0] == 0
    for p in procs:
        assert_no_content_in(p.output(), h.keys, [fake_number(101)])


async def test_worker_killed_after_claim_before_attempt_is_sent_exactly_once_later(
    h: Harness, certs: Certs, database_url: str, tmp_path: Path, procs: list[Proc]
) -> None:
    token, mid = await _accept_now(h)
    gw = TlsFakeProvider(certs, require_client_cert=True, mode="ok")
    lock = psycopg.connect(libpq(database_url), autocommit=True)
    try:
        # Hold the SMS rate-limit coordination lock so the worker claims but cannot begin.
        lock.execute("SELECT pg_advisory_lock(%s, 1)", (RATE_LOCK_CLASS,))
        w = Proc(
            [str(HERE / "_proc_worker.py")],
            worker_env(database_url, h.keys, certs, gw, "pk-a"),
            tmp_path / "wa.log",
        )
        procs.append(w)
        await asyncio.to_thread(
            wait_until,
            lambda: (
                scalar(database_url, "SELECT lease_owner FROM messages WHERE id=%s", uuid.UUID(mid))
                == "pk-a"
            ),
            30,
            "claim by the first worker",
        )
        w.kill()  # genuine death after claim, before begin_attempt
        view = await h.status(token, mid)
        assert (view["status"], view["attempts"]) == ("accepted", 0)
        assert gw.requests == []
        lock.execute("SELECT pg_advisory_unlock(%s, 1)", (RATE_LOCK_CLASS,))

        w2 = Proc(
            [str(HERE / "_proc_worker.py")],
            worker_env(database_url, h.keys, certs, gw, "pk-b"),
            tmp_path / "wb.log",
        )
        procs.append(w2)
        await asyncio.to_thread(
            wait_until,
            lambda: (
                scalar(database_url, "SELECT status FROM messages WHERE id=%s", uuid.UUID(mid))
                == "sent"
            ),
            LEASE + 20,
            "send by the replacement worker",
        )
        await asyncio.sleep(1.0)
        w2.kill()
    finally:
        lock.close()
        gw.close()
    view = await h.status(token, mid)
    assert (view["status"], view["attempts"]) == ("sent", 1)
    assert len(gw.requests) == 1
    for p in procs:
        assert_no_content_in(p.output(), h.keys, [fake_number(101)])


def test_children_are_isolated_from_parent_interpreter() -> None:
    # Guard: the harness scripts are not test modules and are never collected.
    assert not (HERE / "_proc_api.py").name.startswith("test_")
    assert timedelta(seconds=LEASE) > timedelta(seconds=4)
