"""Consent Manager end-to-end trial (host side; talks to 127.0.0.1 only).

Real Consent Manager HTTP API (auth off, dev) with OAN delivery on:
partner + OTP policy -> consent request -> issue OTP -> the SMS reaches the fake 8028
gateway through OAN -> read the code from it like a handset -> verify -> approve.
Negative paths: unknown subject phone (422, no SMS); wrong code; logs never contain the code.
"""

from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx

CM = "http://127.0.0.1:18200"
INBOX = "http://127.0.0.1:18028/inbox"
HERE = Path(__file__).resolve().parent
PHONE = "0900000601"  # CONSENT_MANAGER_OAN_OTP_PHONE_MAP: FIN-1001 -> 0900000601


def ok(r: httpx.Response, *codes: int) -> dict:
    assert r.status_code in codes, (
        f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}"
    )
    return r.json() if r.content else {}


def wait_code(after: int, timeout: float = 30) -> tuple[str, str]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = httpx.get(INBOX, params={"phone": PHONE}, timeout=5).json()
        if len(rows) > after:
            text = rows[-1]["text"]
            return re.search(r"\b(\d{6})\b", text).group(1), text
        time.sleep(0.5)
    raise AssertionError("no OTP SMS reached the gateway")


def main() -> None:
    c = httpx.Client(base_url=CM, timeout=30)
    tag = uuid.uuid4().hex[:6]
    partner = ok(
        c.post(
            "/consent/v1/partners",
            json={
                "audience": f"trial-bank-{tag}",
                "controller_id": "farmer-registry",
                "name": "Trial Bank",
            },
        ),
        201,
    )
    ok(
        c.put(
            f"/consent/v1/partners/{partner['id']}/policy",
            json={
                "allowed_data_scopes": ["farmer.mobile", "farmer.firstname"],
                "allowed_purposes": ["loan_assessment"],
                "allowed_subject_id_types": ["FIN"],
                "lawful_basis": "consent",
                "required_auth_method": "otp",
            },
        ),
        200,
    )

    def new_request(subject: str) -> str:
        return ok(
            c.post(
                "/consent/v1/consent-requests",
                json={
                    "subject_id": {"type": "FIN", "value": subject},
                    "partner_id": partner["id"],
                    "purpose": {"code": "loan_assessment"},
                    "requested_scopes": ["farmer.mobile"],
                },
            ),
            201,
        )["id"]

    before = len(httpx.get(INBOX, params={"phone": PHONE}, timeout=5).json())
    rid = new_request("FIN-1001")
    issued = ok(c.post(f"/consent/v1/consent-requests/{rid}/otp"), 200)
    code, text = wait_code(before)
    print("OTP SMS at gateway:", text.replace(code, "######"))
    assert "Trial Bank" in text and "expires in 5 minutes" in text
    assert code not in str(issued), "API response must never contain the code"

    wrong = "000000" if code != "000000" else "111111"
    bad = c.post(f"/consent/v1/consent-requests/{rid}/verify-otp", json={"otp": wrong})
    print("wrong code ->", bad.status_code)
    assert bad.status_code >= 400

    ok(c.post(f"/consent/v1/consent-requests/{rid}/verify-otp", json={"otp": code}), 200)
    artefact = ok(
        c.post(
            f"/consent/v1/consent-requests/{rid}/approve",
            json={"granted_scopes": ["farmer.mobile"]},
        ),
        201,
    )
    print("approved after OTP; artefact", artefact["id"])

    # Re-issue: a NEW code, a NEW SMS (new key), the old code no longer works.
    rid2 = new_request("FIN-1001")
    count = len(httpx.get(INBOX, params={"phone": PHONE}, timeout=5).json())
    ok(c.post(f"/consent/v1/consent-requests/{rid2}/otp"), 200)
    first, _ = wait_code(count)
    ok(c.post(f"/consent/v1/consent-requests/{rid2}/otp"), 200)
    second, _ = wait_code(count + 1)
    stale = c.post(f"/consent/v1/consent-requests/{rid2}/verify-otp", json={"otp": first})
    print("re-issue: two SMS; stale code ->", stale.status_code if first != second else "same code")
    ok(c.post(f"/consent/v1/consent-requests/{rid2}/verify-otp", json={"otp": second}), 200)

    # Unknown subject phone: visible failure, nothing sent.
    total = len(httpx.get(INBOX, timeout=5).json())
    rid3 = new_request("FIN-9999")
    r = c.post(f"/consent/v1/consent-requests/{rid3}/otp")
    print(
        "unknown phone ->",
        r.status_code,
        r.json().get("reason") or r.json().get("detail") or r.text[:120],
    )
    assert r.status_code == 422
    time.sleep(3)
    assert len(httpx.get(INBOX, timeout=5).json()) == total

    logs = subprocess.run(  # noqa: S603 - fixed arguments
        ["docker", "compose", "logs", "cm-api"],  # noqa: S607 - local trial: docker from PATH
        cwd=HERE,
        capture_output=True,
        text=True,
    ).stdout
    for secret in (code, first, second):
        assert secret not in logs, "an OTP code appeared in the Consent Manager log"
    print("Consent Manager logs contain no OTP code")
    print("CONSENT E2E OK")


if __name__ == "__main__":
    main()
