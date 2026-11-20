"""Helpers for the local integration trial (host side). Talks only to 127.0.0.1.

python stack/tools/trial.py smoke
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

STACK = Path(__file__).resolve().parents[1]
API = "https://127.0.0.1:18000"
CA = str(STACK / "trial-ca.crt")  # exported from the certs volume
INBOX = "http://127.0.0.1:18028/inbox"


def runtime(*args: str) -> str:
    """Run an OAN operator command in the trial stack; returns stdout."""
    out = subprocess.run(  # noqa: S603 - fixed command; args are operator subcommands
        [  # noqa: S607 - local trial: docker from PATH
            "docker",
            "compose",
            "run",
            "--rm",
            "-T",
            "oan-api",
            "python",
            "-m",
            "oan_messaging.runtime",
            *args,
        ],
        cwd=STACK,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


def onboard(caller: str, purposes: str, channels: str = "sms") -> str:
    runtime("caller", "--caller", caller, "--channels", channels, "--purposes", purposes)
    return runtime("issue-credential", "--caller", caller).splitlines()[-1]


def inbox(phone: str | None = None) -> list[dict[str, str]]:
    params = {"phone": phone} if phone else None
    return list(httpx.get(INBOX, params=params, timeout=5).json())


def clear_inbox() -> None:
    httpx.delete(INBOX, timeout=5)


def wait_inbox(phone: str, count: int = 1, timeout: float = 30) -> list[dict[str, str]]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = inbox(phone)
        if len(rows) >= count:
            return rows
        time.sleep(0.5)
    raise AssertionError(f"gateway did not receive {count} message(s) for the test number")


def status(token: str, mid: str) -> dict[str, Any]:
    r = httpx.get(
        f"{API}/api/v1/messages/{mid}",
        verify=CA,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return dict(r.json())


def wait_status(token: str, mid: str, want: str, timeout: float = 30) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = status(token, mid)
        if s["status"] == want:
            return s
        time.sleep(0.5)
    raise AssertionError(f"message never reached {want}: {s['status']}")


def ops_status() -> dict[str, Any]:
    return dict(json.loads(runtime("status")))


def smoke() -> None:
    token = onboard("smoke", "otp,transactional")
    clear_inbox()
    phone = "0900000501"
    body = {
        "recipient": phone,
        "text": "Smoke test code 123456. It expires in 5 minutes.",
        "source_reference": "smoke:1",
        "purpose": "otp",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    r = httpx.post(
        f"{API}/api/v1/messages/sms",
        json=body,
        timeout=10,
        verify=CA,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    got = wait_inbox(phone)
    assert got[-1]["text"] == body["text"]
    final = wait_status(token, mid, "delivered")
    print(
        f"smoke ok: accepted -> gateway (sms_id {got[-1]['sms_id']}) -> "
        f"{final['status']} (final={final['final']})"
    )
    print("ops alerts:", ops_status()["alerts"])


if __name__ == "__main__":
    {"smoke": smoke}[sys.argv[1]]()
