"""Consent Manager OTP-by-SMS acceptance (CM-01 to CM-08) against the cm-test install. Test numbers only.

Runs in the cm-test-driver pod: ``python3 - <phase> < acceptance.py``. Phases:
  setup | cm01-05 | raise <subject> | inbox-count <phone>
Prints only statuses, error codes and PASS/FAIL lines; never a code, token or phone.
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt

CM = "http://cm-api.consent-sms"
PORTAL = CM + "/consent/v1/partner-portal"
INBOX = "http://fake-8028.oan-messaging:8080/inbox"
STATE = Path("/tmp/cm-acceptance.json")
KEY = Path("/partner/private.pem").read_text()
FR = "farmer-registry"
SUBJECTS = {"a": "100000000001", "b": "100000000002", "c": "100000000003", "nophone": "100000000009"}
PHONES = {"100000000001": "0900000701", "100000000002": "0900000702", "100000000003": "0900000703"}
results = []


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def check(case, ok, detail):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {case}: {detail}", flush=True)


def token(partner):
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"iss": os.environ["PARTNER_ISSUER"], "sub": str(uuid.uuid4()), "iat": now, "exp": now + 600,
              "preferred_username": "acceptance", "realm_access": {"roles": ["PARTNER_OPERATOR"]}, "partner_id": partner}
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "cm-test-1"})


def pc(method, path, body=None):
    r = httpx.request(method, PORTAL + path, json=body, timeout=30,
                      headers={"Authorization": "Bearer " + token(state()["partner"])})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:200]}


def otp_body(subject):
    return {"subject_id": {"type": "FAYDA_FAN", "value": subject}, "purpose": {"code": "credit-assessment"},
            "grants": [{"data_controller": FR, "data_scopes": ["farmer_personal_details"]}],
            "use_case": "loan-profile@1", "method": "otp"}


def inbox(phone):
    rows = httpx.get(INBOX, timeout=10).json()
    tail = phone[-8:]
    return [r for r in rows if str(r.get("phone", "")).endswith(tail)]


def code_from(row):
    words = str(row.get("message", row.get("text", ""))).replace(".", " ").replace(":", " ").split()
    return next((w for w in words if w.isdigit() and len(w) == 6), None)


def wait_sms(phone, count, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        rows = inbox(phone)
        if len(rows) >= count:
            return rows
        time.sleep(2)
    return inbox(phone)


def setup():
    partner = f"bank-acc-{uuid.uuid4().hex[:8]}"
    r = httpx.post(CM + "/consent/v1/partners", json={"audience": partner, "controller_id": FR,
                                                       "partner_mgmt_id": "PM_" + partner, "name": partner}, timeout=30)
    assert r.status_code == 201, (r.status_code, r.text[:300])
    r2 = httpx.put(CM + f"/consent/v1/partners/{r.json()['id']}/policy", timeout=30, json={
        "allowed_data_scopes": ["farmer_personal_details"], "allowed_purposes": ["credit-assessment"],
        "allowed_subject_id_types": ["FAYDA_FAN"], "allowed_signing_algs": ["EdDSA"], "max_validity_duration": "P1Y"})
    assert r2.status_code in (200, 201), (r2.status_code, r2.text[:300])
    STATE.write_text(json.dumps({"partner": partner}))
    print("setup: partner binding and policy created")


def cm01_05():
    httpx.delete(INBOX, timeout=10)
    a, phone = SUBJECTS["a"], PHONES[SUBJECTS["a"]]
    status, req = pc("POST", "/consent-requests", otp_body(a))
    rows = wait_sms(phone, 1)
    code = code_from(rows[0]) if rows else None
    check("CM-01", status == 201 and len(rows) == 1 and code is not None and code not in json.dumps(req),
          f"raise {status}; SMS on the fake 8028 inbox: {len(rows)}; code absent from the response: "
          f"{code is not None and code not in json.dumps(req)}")
    st = state()
    st.update(cm01_request=req.get("id"), cm01_code=code)
    STATE.write_text(json.dumps(st))
    wrong = "000000" if code != "000000" else "111111"
    status, body = pc("POST", f"/consent-requests/{req['id']}/verify-otp", {"otp": wrong})
    check("CM-02", status == 401, f"wrong code {status} {body.get('error', '')}")
    status, body = pc("POST", f"/consent-requests/{req['id']}/verify-otp", {"otp": code})
    check("CM-03", status == 201 and body.get("status") == "active", f"right code {status}; consent {body.get('status')}")

    b, phone_b = SUBJECTS["b"], PHONES[SUBJECTS["b"]]
    status, req = pc("POST", "/consent-requests", otp_body(b))
    first = wait_sms(phone_b, 1)
    old = code_from(first[0]) if first else None
    print("CM-04: waiting 62 s for the resend cooldown", flush=True)
    time.sleep(62)
    rs, _ = pc("POST", f"/consent-requests/{req['id']}/resend-otp")
    rows = wait_sms(phone_b, 2)
    new = code_from(rows[-1]) if len(rows) >= 2 else None
    vo, _ = pc("POST", f"/consent-requests/{req['id']}/verify-otp", {"otp": old}) if old and old != new else (None, None)
    vn, body = pc("POST", f"/consent-requests/{req['id']}/verify-otp", {"otp": new}) if new else (None, {})
    check("CM-04", rs == 200 and len(rows) == 2 and new and new != old and vo == 401 and vn == 201,
          f"resend {rs}; SMS {len(rows)}; new code differs: {bool(new and new != old)}; old code {vo}; new code {vn}")

    before = len(httpx.get(INBOX, timeout=10).json())
    status, body = pc("POST", "/consent-requests", otp_body(SUBJECTS["nophone"]))
    time.sleep(5)
    after = len(httpx.get(INBOX, timeout=10).json())
    check("CM-05", status == 422 and body.get("error") == "otp_destination_unknown" and after == before,
          f"{status} {body.get('error')}; SMS sent: {after - before}")


def raise_one(subject):
    status, body = pc("POST", "/consent-requests", otp_body(SUBJECTS[subject]))
    print(json.dumps({"status": status, "error": body.get("error"), "id": body.get("id"),
                      "request_status": body.get("status")}))


phase = sys.argv[1]
if phase == "setup":
    setup()
elif phase == "cm01-05":
    cm01_05()
elif phase == "raise":
    raise_one(sys.argv[2])
elif phase == "inbox-count":
    print(len(inbox(PHONES[SUBJECTS[sys.argv[2]]])))
sys.exit(0 if all(results) else 1)
