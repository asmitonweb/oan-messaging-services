"""MSG-08: the REAL runtime entrypoints as OS processes (not harness copies).

* worker: ``python -m oan_messaging.runtime worker`` sends through the real 8028 adapter
  (mTLS, loopback fake gateway), writes its liveness heartbeat and stops cleanly on SIGTERM;
* operator commands: ``suspend``/``resume``/``status`` (exit 2 on alert);
* API: ``uvicorn --factory oan_messaging.runtime.api:create_app`` health and readiness.

Generated keys and certificates, 0900000xxx numbers and loopback only.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from itsupport import ROOT, Certs, Harness, TlsFakeProvider, fake_number, new_key
from test_int_process import (
    Proc,
    _accept_now,
    assert_no_content_in,
    child_env,
    free_port,
    procs,  # noqa: F401 (fixture)
    scalar,
    wait_until,
    worker_env,
)

pytestmark = pytest.mark.postgres


def run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (fixed interpreter and module)
        [sys.executable, "-m", "oan_messaging.runtime", *args],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


async def test_worker_entrypoint_sends_heartbeats_and_stops_on_sigterm(
    h: Harness,
    certs: Certs,
    database_url: str,
    tmp_path: Path,
    procs: list[Proc],  # noqa: F811
) -> None:
    token, mid = await _accept_now(h)
    gw = TlsFakeProvider(certs, require_client_cert=True, mode="ok")
    heartbeat = tmp_path / "worker.alive"
    env = worker_env(database_url, h.keys, certs, gw, "rt-1") | {
        "OAN_MSG_WORKER_CHANNELS": "sms",
        "OAN_MSG_WORKER_HEARTBEAT_FILE": str(heartbeat),
    }
    try:
        w = Proc(["-m", "oan_messaging.runtime", "worker"], env, tmp_path / "w.log")
        procs.append(w)
        await asyncio.to_thread(
            wait_until,
            lambda: (
                scalar(database_url, "SELECT status FROM messages WHERE id=%s::uuid", mid) == "sent"
            ),
            30,
            "worker entrypoint to send",
        )
        assert len(gw.requests) == 1
        probe = run_cli(env, "check-heartbeat", "--max-age", "10")
        assert probe.returncode == 0, probe.stderr
        w.p.send_signal(signal.SIGTERM)
        assert w.p.wait(30) == 0, w.output()[-2000:]
    finally:
        gw.close()
    time.sleep(1.2)
    stale = run_cli(env, "check-heartbeat", "--max-age", "1")
    assert stale.returncode == 1 and "stale" in stale.stderr
    missing = run_cli(
        env | {"OAN_MSG_WORKER_HEARTBEAT_FILE": str(tmp_path / "none")}, "check-heartbeat"
    )
    assert missing.returncode == 1
    assert (await h.status(token, mid))["status"] == "sent"
    assert_no_content_in(w.output(), h.keys, [fake_number(101)])


async def test_operator_commands_suspend_resume_and_status(h: Harness, database_url: str) -> None:
    env = child_env(database_url, h.keys)
    suspend = run_cli(env, "suspend", "--channel", "sms")
    assert suspend.returncode == 0, suspend.stderr[-2000:]
    status = run_cli(env, "status", "--fail-on-alert")
    assert status.returncode == 2, status.stderr[-2000:]
    report = json.loads(status.stdout)
    assert "suspended:sms" in report["alerts"]
    assert report["provider_scopes"]["sms"]["suspended"] is True
    assert run_cli(env, "resume", "--channel", "sms").returncode == 0
    clear = run_cli(env, "status", "--fail-on-alert")
    assert clear.returncode == 0, clear.stdout + clear.stderr[-2000:]
    assert json.loads(clear.stdout)["alerts"] == []
    for out in (suspend, status, clear):
        assert_no_content_in(out.stdout + out.stderr, h.keys, [])
    assert run_cli(env, "suspend").returncode == 2  # --channel required (argparse)


async def test_misconfigured_worker_fails_fast_without_leaking_settings(
    h: Harness, database_url: str
) -> None:
    env = child_env(database_url, h.keys) | {"OAN_MSG_WORKER_CHANNELS": "sms"}
    out = run_cli(env, "worker")  # no 8028 credentials configured
    assert out.returncode == 1
    assert "component failed error_type=" in out.stderr
    assert_no_content_in(out.stdout + out.stderr, h.keys, [])


async def test_api_entrypoint_health_and_readiness(
    h: Harness,
    database_url: str,
    tmp_path: Path,
    procs: list[Proc],  # noqa: F811
) -> None:
    async def serve(url: str, name: str) -> tuple[Proc, int]:
        port = free_port()
        p = Proc(
            [
                "-m",
                "uvicorn",
                "--factory",
                "oan_messaging.runtime.api:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            child_env(url, h.keys),
            tmp_path / f"{name}.log",
        )
        procs.append(p)

        def up() -> bool:
            try:
                return httpx.get(f"http://127.0.0.1:{port}/livez", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False

        await asyncio.to_thread(wait_until, up, 30, f"{name} API to start")
        return p, port

    _, port = await serve(database_url, "api")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        assert (await client.get("/readyz")).status_code == 200
        token = await h.caller("rt-api", bot=None)
        accepted = await client.post(
            "/api/v1/messages/sms",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": new_key()},
            json={
                "recipient": fake_number(5),
                "text": "Your code expires soon.",
                "source_reference": "evt-rt",
                "purpose": "transactional",
            },
        )
        assert accepted.status_code == 202, accepted.text
    dead = database_url.replace(
        database_url.split("@", 1)[1].split("/", 1)[0], f"127.0.0.1:{free_port()}"
    )
    api, port = await serve(dead, "api-dead")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        assert (await client.get("/livez")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
    assert_no_content_in(api.output(), h.keys, [])
    assert dead.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1] not in api.output()


async def test_operator_onboards_a_caller_and_rotates_its_credential(
    h: Harness, database_url: str
) -> None:
    env = child_env(database_url, h.keys)
    made = run_cli(env, "caller", "--caller", "esignet", "--channels", "sms", "--purposes", "otp")
    assert made.returncode == 0, made.stderr[-2000:]

    def issue() -> tuple[str, str]:
        out = run_cli(env, "issue-credential", "--caller", "esignet")
        assert out.returncode == 0, out.stderr[-2000:]
        token = out.stdout.strip()
        assert token.startswith("oanmsg_") and token not in out.stderr
        found = re.search(r"credential_id=([0-9a-f-]{36})", out.stderr)
        assert found is not None, out.stderr[-2000:]
        return found.group(1), token

    old_id, old = issue()
    new_id, new = issue()
    h.clock.now = datetime.now(UTC)
    for token in (old, new):
        assert (await h.post_sms(token, new_key())).status_code == 202
    assert (await h.post_sms(new, new_key(), purpose="transactional")).status_code == 403
    listed = run_cli(env, "list-credentials", "--caller", "esignet")
    rows = json.loads(listed.stdout)
    assert [r["credential_id"] for r in rows] == [old_id, new_id]
    assert old not in listed.stdout and new not in listed.stdout
    # Rotation: revoke only the old token; the new one keeps working.
    one = run_cli(env, "revoke-credentials", "--caller", "esignet", "--credential", old_id)
    assert one.returncode == 0, one.stderr[-2000:]
    assert (await h.post_sms(old, new_key())).status_code == 401
    assert (await h.post_sms(new, new_key())).status_code == 202
    again = run_cli(env, "revoke-credentials", "--caller", "esignet", "--credential", old_id)
    assert again.returncode == 1  # already revoked: reported, not silently accepted
    assert run_cli(env, "revoke-credentials", "--caller", "esignet").returncode == 2
    everything = run_cli(env, "revoke-credentials", "--caller", "esignet", "--all")
    assert everything.returncode == 0
    assert (await h.post_sms(new, new_key())).status_code == 401
    for out in (one, again, everything, listed):
        assert old not in out.stdout + out.stderr and new not in out.stdout + out.stderr
    refused = run_cli(
        env, "caller", "--caller", "bulk", "--channels", "sms", "--purposes", "broadcast"
    )
    assert refused.returncode == 1 and "component failed" in refused.stderr
    assert run_cli(env, "issue-credential").returncode == 2  # --caller required


async def test_worker_refuses_to_start_without_a_key_version_in_use(
    h: Harness, database_url: str
) -> None:
    import base64
    import os

    await _accept_now(h)  # stores content encrypted under the configured key version
    other = base64.b64encode(os.urandom(32)).decode()
    env = child_env(database_url, h.keys) | {
        "OAN_MSG_WORKER_CHANNELS": "sms",
        "OAN_MSG_CONTENT_KEYS": f"zz9:{other}",
        "OAN_MSG_CONTENT_KEY_CURRENT": "zz9",
    }
    out = run_cli(env, "worker")
    assert out.returncode == 1
    assert "key versions in use but not configured: content:" in out.stderr
    assert other not in out.stderr
    assert_no_content_in(out.stdout + out.stderr, h.keys, [fake_number(101)])


async def test_database_permission_error_at_startup_logs_type_only(
    h: Harness, database_url: str
) -> None:
    """Found in the kind install test: before grants, a permission error escaped as a full
    traceback with SQL text. Start-up failures must log the error type only."""
    import psycopg
    from itsupport import libpq

    role = "oan_msg_nogrant_" + new_key()[:8].lower().replace("-", "")
    password = new_key()
    with psycopg.connect(libpq(database_url), autocommit=True) as conn:
        conn.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
    try:
        rest = database_url.split("@", 1)[1]
        env = child_env(f"postgresql+psycopg://{role}:{password}@{rest}", h.keys) | {
            "OAN_MSG_WORKER_CHANNELS": "sms"
        }
        out = run_cli(env, "worker")
        assert out.returncode == 1
        assert "component failed error_type=ProgrammingError" in out.stderr
        assert "Traceback" not in out.stderr and "SELECT" not in out.stderr
        assert password not in out.stderr
    finally:
        with psycopg.connect(libpq(database_url), autocommit=True) as conn:
            conn.execute(f'DROP OWNED BY "{role}"')
            conn.execute(f'DROP ROLE "{role}"')
