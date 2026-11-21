"""OAN Messaging on registry-test: COMMON cases and the 4 SMS/s load check. Test numbers only (fake 8028).

Runs in the cm-test-driver pod; the caller token is read from stdin and never printed:
  kubectl exec -i <driver> -- python3 -c "$(cat oan_load_common.py)" <phase> < token-file
Phases: common | load <count>
Prints statuses, counts and timings only.
"""

import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

API = "https://oan-messaging.test.oanstaging.com/api/v1/messages/sms"
TOKEN = sys.stdin.readline().strip()
H = {"Authorization": "Bearer " + TOKEN}
results = []


def check(case, ok, detail):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {case}: {detail}", flush=True)


EXPIRES = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()


def send(text, recipient, key=None, ref=None, purpose="transactional", client=None):
    body = {"recipient": recipient, "text": text, "purpose": purpose,
            "source_reference": ref or f"acceptance:{uuid.uuid4().hex[:12]}", "expires_at": EXPIRES}
    r = (client or httpx).post(API, json=body, headers={**H, "Idempotency-Key": key or str(uuid.uuid4())}, timeout=30)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def common():
    key = str(uuid.uuid4())
    s1, b1 = send("OAN acceptance: retry with the same key.", "+251900000801", key=key, ref="acceptance:retry-1")
    s2, b2 = send("OAN acceptance: retry with the same key.", "+251900000801", key=key, ref="acceptance:retry-1")
    check("COMMON retries reuse the key", s1 == 202 and s2 in (200, 202) and b1.get("id") == b2.get("id"),
          f"first {s1}, repeat {s2}, same message id: {b1.get('id') == b2.get('id')}")
    s3, b3 = send("OAN acceptance: changed body.", "+251900000801", key=key, ref="acceptance:retry-1")
    check("COMMON a key with a different body is refused", s3 == 409 or s3 == 422,
          f"{s3} {b3.get('code', '')}")
    s4, _ = send("ሀ" * 70, "+251900000802", purpose="otp")
    s5, b5 = send("ሀ" * 71, "+251900000802", purpose="otp")
    check("COMMON an Amharic OTP must fit one part (70)", s4 == 202 and s5 == 422,
          f"70 characters {s4}; 71 characters {s5} {b5.get('code', '')}")
    s7, _ = send("ሀ" * 201, "+251900000802")
    s8, b8 = send("ሀ" * 202, "+251900000802")
    check("COMMON Amharic notice up to 3 parts (201)", s7 == 202 and s8 == 422,
          f"201 characters {s7}; 202 characters {s8} {b8.get('code', '')}")
    s6, b6 = send("x", "+251900000803", purpose="broadcast")
    check("COMMON purpose outside the caller's grant is refused", s6 in (403, 422), f"{s6} {b6.get('code', '')}")
    print(json.dumps({"ids": [b1.get("id"), b2.get("id")]}))


def load(n):
    with httpx.Client(timeout=30, limits=httpx.Limits(max_connections=20)) as c:
        start = time.time()
        with ThreadPoolExecutor(20) as pool:
            out = list(pool.map(lambda i: send(f"OAN load test message {i}.", f"+2519009{i:05d}",
                                               ref=f"load:{i}", client=c), range(n)))
        took = time.time() - start
    ok = sum(1 for s, _ in out if s == 202)
    print(json.dumps({"submitted": n, "accepted": ok, "submit_seconds": round(took, 1),
                      "other_statuses": sorted({s for s, _ in out if s != 202})}))


def otps(n, delay=10.0):
    """OTPs sent one every 2 s while the load drains: they use the reserved slot."""
    time.sleep(delay)
    codes = []
    for i in range(n):
        s, _ = send(f"Your OAN test code is {100000 + i}. It expires in 5 minutes.", f"+2519008{i:05d}",
                    ref=f"loadotp:{i}", purpose="otp")
        codes.append(s)
        time.sleep(2)
    print(json.dumps({"otp_submitted": n, "otp_accepted": codes.count(202)}))


phase = sys.argv[1]
if phase == "common":
    common()
elif phase == "load":
    load(int(sys.argv[2]))
elif phase == "otp":
    otps(int(sys.argv[2]))
sys.exit(0 if all(results) else 1)
