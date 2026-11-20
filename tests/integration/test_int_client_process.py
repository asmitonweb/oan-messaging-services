"""The accepted MSG-06 client over REAL sockets to a REAL API process.

The API runs in its own process (``_proc_api.py``: ``create_app()`` with the production
lifespan, settings from ``OAN_MSG_*``) serving HTTPS on loopback with a generated
certificate. The client verifies it with the generated CA, exactly as configured in
production (no verification switch exists). ``IT_HOLD_POSTS=1`` makes the process commit the
first POST and then never send its response, so the client sees a genuine read timeout
on a real connection. Nothing is simulated inside the client or the service.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from alembic import command
from itclient import EXAMPLES  # noqa: F401  (puts examples/ on sys.path)
from itfixtures import alembic_config
from itsupport import (
    OTP_TEXT,
    ROOT,
    Certs,
    Keys,
    create_database,
    drop_database,
    fake_number,
    libpq,
    make_caller,
    needle_forms,
)
from oan_client import ClientConfig, OanClient, OanRejected, Secret, SmsDelivery

from oan_messaging.storage.db import make_engine, make_session_factory

pytestmark = pytest.mark.postgres
HERE = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def api_process(
    admin_url: str, keys: Keys, certs: Certs, tmp_path: Path
) -> Iterator[tuple[str, int, Path]]:
    url = create_database(admin_url, "oan_msg_itclient")
    command.upgrade(alembic_config(url), "head")
    port = free_port()
    log = tmp_path / "api.log"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "OAN_MSG_DATABASE_URL": url,
        "IT_HOLD_POSTS": "1",
        **keys.env(),
    }
    with log.open("wb") as fh:
        proc = subprocess.Popen(  # noqa: S603 (fixed interpreter + harness file)
            [
                sys.executable,
                str(HERE / "_proc_api.py"),
                str(port),
                str(certs["server_cert"]),
                str(certs["server_key"]),
            ],
            env=env,
            cwd=ROOT,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        try:
            ctx = ssl.create_default_context(cafile=str(certs["ca_cert"]))
            deadline = time.monotonic() + 30
            while True:
                try:
                    r = httpx.get(f"https://127.0.0.1:{port}/api/v1/messages/x", verify=ctx)
                    if r.status_code == 401:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise AssertionError("API process did not start")
                time.sleep(0.1)
            yield url, port, log
        finally:
            proc.kill()
            proc.wait(10)
            drop_database(admin_url, url)


def scalar_rows(url: str, sql: str) -> list[tuple[object, ...]]:
    with psycopg.connect(libpq(url)) as conn:
        return [tuple(r) for r in conn.execute(sql).fetchall()]


async def test_genuinely_lost_response_over_tls_is_replayed_by_the_real_client(
    api_process: tuple[str, int, Path], keys: Keys, certs: Certs
) -> None:
    url, port, log = api_process
    engine = make_engine(url)
    try:
        token = await make_caller(make_session_factory(engine), keys, "auth", bot=None)
        other = await make_caller(make_session_factory(engine), keys, "grievance", bot=None)
    finally:
        await engine.dispose()

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    def client(tok: str) -> OanClient:
        return OanClient(
            ClientConfig(
                base_url=f"https://127.0.0.1:{port}",
                token=Secret(tok),
                read_timeout=2.0,
                connect_timeout=2.0,
                ca_bundle=str(certs["ca_cert"]),
            ),
            sleep=no_wait,
        )

    now = datetime.now(UTC)
    d = SmsDelivery(
        idempotency_key=str(uuid.uuid4()),
        recipient=fake_number(301),
        text=OTP_TEXT,
        source_reference="auth:password-reset:req-9",
        purpose="otp",
        expires_at=now + timedelta(minutes=5),
        first_submitted_at=now,
    )
    async with client(token) as c:
        sub = await c.submit(d)  # attempt 1 commits, response withheld; attempt 2 replays
        view = await c.get_status(sub.id)
    async with client(other) as g:
        with pytest.raises(OanRejected) as cross:
            await g.get_status(sub.id)

    rows = scalar_rows(url, "SELECT id::text, status, expires_at FROM messages")
    assert len(rows) == 1 and rows[0][0] == sub.id
    assert sub.replayed is True  # the first response really was lost
    assert rows[0][2] == d.expires_at and view.status == "accepted"
    assert cross.value.status == 404
    out = log.read_text(errors="replace")
    assert "IT-RESPONSE-WITHHELD" in out
    for needle in [OTP_TEXT, token, other, *needle_forms(fake_number(301))]:
        assert needle not in out, "API process output leaked content or a credential"
