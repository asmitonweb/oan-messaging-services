"""Generate the OAN Messaging Postman collection and environment templates (contract 3.2.0).

    python docs/postman/build_collection.py

Writes oan-messaging.postman_collection.json and one environment per target. Tokens are
left blank on purpose: paste them into the Postman environment (type secret), never commit them.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

PRE = r"""// A fresh Idempotency-Key, source_reference and expires_at per request. Requests with the
// X-Keep-Key header reuse the previous request's values (replay and conflict checks).
if (!pm.request.headers.has('X-Keep-Key')) {
  pm.collectionVariables.set('idempotencyKey', 'pm-' + pm.variables.replaceIn('{{$guid}}'));
  pm.collectionVariables.set('sourceReference', 'postman:' + Date.now());
  // expires_at: 5 minutes ahead for OTPs, 30 minutes for everything else.
  const ttl = (pm.request.name || '').toLowerCase().includes('otp') ? 5 : 30;
  pm.collectionVariables.set('expiresAt', new Date(Date.now() + ttl * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z'));
}"""

STATUS_TEST = """pm.test('status is {code}', () => pm.response.to.have.status({code}));"""
PROBLEM_TEST = """pm.test('problem code is {code}', () => pm.expect(pm.response.json().code).to.eql('{code}'));"""
SAVE_ID = """if (pm.response.code === 202 || pm.response.code === 200) {
  const b = pm.response.json();
  pm.collectionVariables.set('messageId', b.id);
  pm.collectionVariables.set('lastKey', b.idempotency_key);
  pm.test('response never carries the text', () => pm.expect(b).to.not.have.property('text'));
  pm.test('status is accepted', () => pm.expect(b.status).to.eql('accepted'));
}"""


def tests(*lines):
    return [{"listen": "test", "script": {"type": "text/javascript", "exec": "\n".join(lines).split("\n")}}]


def headers(auth="{{callerToken}}", key=True, ctype="application/json", keep=False):
    h = []
    if auth is not None:
        h.append({"key": "Authorization", "value": f"Bearer {auth}"})
    if ctype:
        h.append({"key": "Content-Type", "value": ctype})
    if key:
        h.append({"key": "Idempotency-Key", "value": "{{idempotencyKey}}"})
    if keep:
        h.append({"key": "X-Keep-Key", "value": "1", "description": "Reuse the previous request's key"})
    return h


def post(name, path, body, desc, expect, problem=None, auth="{{callerToken}}", key=True, ctype="application/json",
         keep=False, save=False, raw=None):
    t = [STATUS_TEST.format(code=expect)]
    if problem:
        t.append(PROBLEM_TEST.format(code=problem))
    if save:
        t.append(SAVE_ID)
    return {"name": name, "event": tests(*t), "request": {
        "method": "POST", "header": headers(auth, key, ctype, keep), "description": desc,
        "body": {"mode": "raw", "raw": raw if raw is not None else json.dumps(body, indent=2, ensure_ascii=False),
                 "options": {"raw": {"language": "json"}}},
        "url": {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": path.strip("/").split("/")}}}


def get(name, path, desc, expect, auth="{{callerToken}}", query=None, extra_tests=()):
    url = {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": path.strip("/").split("/")}
    if query:
        url["query"] = [{"key": k, "value": v, **({"disabled": True} if off else {})} for k, v, off in query]
        url["raw"] += "?" + "&".join(f"{k}={v}" for k, v, off in query if not off)
    hdr = [{"key": "Authorization", "value": f"Bearer {auth}"}] if auth else []
    return {"name": name, "event": tests(STATUS_TEST.format(code=expect), *extra_tests),
            "request": {"method": "GET", "header": hdr, "description": desc, "url": url}}


def sms(purpose, text, recipient="{{recipient}}", expires=True):
    b = {"recipient": recipient, "text": text, "purpose": purpose, "source_reference": "{{sourceReference}}"}
    if expires:
        b["expires_at"] = "{{expiresAt}}"
    return b


SMS = "/api/v1/messages/sms"
STATUS_DESC = ("Status of a message this caller submitted. Never contains the text; the recipient is masked. "
               "Statuses: accepted, sending, sent, delivered (final), expired, failed, unknown (final). "
               "Never resubmit an `unknown` message under a new key: 8028 may have sent it.")
STATUS_TESTS = ("const s = pm.response.json();",
                "pm.test('recipient is masked', () => pm.expect(s.recipient_masked).to.include('*'));",
                "pm.test('no text in the status', () => pm.expect(s).to.not.have.property('text'));",
                "console.log('status', s.status, 'final', s.final, 'provider', s.provider_status || '-');")

collection = {
    "info": {
        "name": "OAN Messaging Service (contract 3.2.0)",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        "description": (
            "SMS (8028) and Telegram delivery for OAN services. Contract: docs/openapi.yaml (3.2.0).\n\n"
            "Setup: import an environment from docs/postman, paste the caller token into `callerToken` "
            "(and a statistics-reader token into `statsToken` for Charts). Tokens are issued by the OAN "
            "operator (`python -m oan_messaging.runtime issue-credential`); keep them as secret values, never "
            "commit or share them.\n\n"
            "Every POST needs an `Idempotency-Key` (8 to 128 of A-Z a-z 0-9 . _ : -). The collection makes a new "
            "one per request; a real caller uses one stable key per intended message and reuses it on every retry. "
            "202 = new, 200 = identical replay (same id), 409 = same key with a different body.\n\n"
            "Rules: one recipient per message (Ethiopian mobile: 09/07 series, 2519..., +2519...). An `otp` must "
            "carry `expires_at` and fit one SMS part (160 GSM-7 or 70 characters if any Amharic/Unicode). Other "
            "texts may use up to 3 parts (459 GSM-7 / 201 Unicode). 202 means accepted, not sent: poll the status.\n\n"
            "On the registry-test environment the gateway is a fake 8028: nothing reaches a real phone."),
    },
    "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": PRE.split("\n")}}],
    "variable": [{"key": k, "value": ""} for k in ("idempotencyKey", "sourceReference", "expiresAt", "messageId",
                                                     "lastKey")],
    "item": [
        {"name": "1. Health", "item": [
            get("Liveness", "/livez", "Process is up. No token.", 200, auth=None),
            get("Readiness", "/readyz", "Database reachable and the service ready for traffic. No token.", 200,
                auth=None)]},
        {"name": "2. Send SMS", "item": [
            post("Send OTP", SMS, sms("otp", "Your OAN code is 123456. It expires in 5 minutes. Do not share it."),
                 "An OTP: `expires_at` required (the code's own expiry), one SMS part, highest priority. "
                 "OAN never sends it after `expires_at`.", 202, save=True),
            get("Message status", "/api/v1/messages/{{messageId}}", STATUS_DESC, 200,
                extra_tests=STATUS_TESTS),
            post("Send transactional", SMS, sms("transactional", "Your grievance GRV-1024 has been resolved. "
                                                "Thank you for contacting OAN."),
                 "A notice in response to a user action. Up to 3 SMS parts. `expires_at` optional but recommended.",
                 202, save=True),
            post("Send transactional (Amharic)", SMS, sms("transactional", "የቅሬታዎ ቁጥር GRV-1024 ተፈትቷል። እናመሰግናለን።"),
                 "Any Amharic character makes the whole text Unicode: 70 characters per part, 201 for 3 parts.",
                 202, save=True),
            post("Replay with the same key", SMS,
                 sms("transactional", "Your grievance GRV-1024 has been resolved. Thank you for contacting OAN."),
                 "Run 'Send transactional' first, then this: it reuses that key with the same body. Expect 200 "
                 "with the same message id and no second SMS. (Bodies must be byte-identical, including "
                 "`source_reference` and `expires_at`, so run it straight after, before the variables change.)",
                 200, keep=True),
        ]},
        {"name": "3. Refusals (expected errors)", "item": [
            post("No token", SMS, sms("transactional", "Hello"), "401 `unauthenticated`.", 401, "unauthenticated",
                 auth=None),
            post("Missing Idempotency-Key", SMS, sms("transactional", "Hello"),
                 "422 `validation_failed`, field `header.Idempotency-Key` code `required`.", 422, "validation_failed",
                 key=False),
            post("Wrong content type", SMS, None, "415: the body must be `application/json`.", 415,
                 "unsupported_media_type", ctype="text/plain", raw="recipient=0900000000"),
            post("Invalid phone number", SMS, sms("transactional", "Hello", recipient="0123456789"),
                 "422: only Ethiopian mobile numbers (09/07 series).", 422, "validation_failed"),
            post("OTP without expires_at", SMS, sms("otp", "Your code is 123456.", expires=False),
                 "422: an OTP must carry its expiry.", 422, "validation_failed"),
            post("OTP longer than one part", SMS, sms("otp", "ሀ" * 71),
                 "422: an OTP must fit one SMS part (here 71 Unicode characters; 70 is the limit).", 422,
                 "validation_failed"),
            post("Text longer than three parts", SMS, sms("transactional", "ሀ" * 202),
                 "422: more than 201 Unicode characters is refused, never cut.", 422, "validation_failed"),
            post("Purpose not granted", SMS, sms("broadcast", "Weather advisory for your kebele."),
                 "403 `forbidden` (`purpose_not_permitted`) unless this caller was created with that purpose. "
                 "Broadcast is also off until the deployment enables it.", 403, "forbidden"),
            post("Same key, different body", SMS, sms("transactional", "A different text under a used key."),
                 "Run after any successful send: reuses its key with another body. 409 `idempotency_conflict`.",
                 409, "idempotency_conflict", keep=True),
            get("Status of an unknown id", "/api/v1/messages/00000000-0000-4000-8000-000000000000",
                "404: unknown id, or a message of another caller.", 404),
        ]},
        {"name": "4. Telegram", "item": [
            post("Send Telegram", "/api/v1/messages/telegram",
                 {"chat_id": "{{chatId}}", "text": "Your visit is confirmed for tomorrow.", "purpose": "transactional",
                  "source_reference": "{{sourceReference}}", "expires_at": "{{expiresAt}}"},
                 "Only for callers configured with a Telegram bot (else 403 `telegram_bot_not_configured`). "
                 "`chat_id` is an integer: the farmer must have started the bot. No delivery receipts on Telegram.",
                 202, save=True, raw=json.dumps(
                     {"chat_id": 0, "text": "Your visit is confirmed for tomorrow.", "purpose": "transactional",
                      "source_reference": "{{sourceReference}}", "expires_at": "{{expiresAt}}"},
                     indent=2).replace('"chat_id": 0', '"chat_id": {{chatId}}')),
        ]},
        {"name": "5. Charts (statistics reader)", "item": [
            get("KPIs", "/api/v1/charts/msgKpis", "Counts only. Needs a token of a caller created with "
                "`--stats-reader` (else 403 `stats_not_permitted`). Query: from/to (YYYY-MM-DD), granularity "
                "(day|week|month), caller, purpose, channel (comma lists).", 200, auth="{{statsToken}}",
                query=[("from", "2026-11-01", True), ("to", "2026-11-30", True), ("granularity", "day", True),
                       ("purpose", "otp,transactional", True), ("channel", "sms", True)]),
            get("Several charts", "/api/v1/charts", "Up to 20 chart ids in `charts`: msgKpis, msgTrend, "
                "msgByCaller, msgByPurpose, msgFailureReasons, msgOtpLatency, msgCampaigns, msgQueueNow, "
                "msgProviderHealth, msgFilterCallers.", 200, auth="{{statsToken}}",
                query=[("charts", "msgKpis,msgTrend,msgByPurpose,msgOtpLatency,msgQueueNow", False),
                       ("granularity", "day", False)]),
        ]},
    ],
}


def env(name, base, recipient, note):
    return {"name": name, "values": [
        {"key": "baseUrl", "value": base, "type": "default", "enabled": True},
        {"key": "callerToken", "value": "", "type": "secret", "enabled": True},
        {"key": "statsToken", "value": "", "type": "secret", "enabled": True},
        {"key": "recipient", "value": recipient, "type": "default", "enabled": True},
        {"key": "chatId", "value": "0", "type": "default", "enabled": True}],
        "_postman_variable_scope": "environment", "_note": note}


(HERE / "oan-messaging.postman_collection.json").write_text(
    json.dumps(collection, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
(HERE / "registry-test.postman_environment.json").write_text(json.dumps(env(
    "OAN Messaging - registry-test (fake 8028)", "https://oan-messaging.test.oanstaging.com", "0900000901",
    "Fake 8028: no real SMS. Paste the dpi-consent caller token into callerToken."), indent=2) + "\n",
    encoding="utf-8")
(HERE / "local-trial.postman_environment.json").write_text(json.dumps(env(
    "OAN Messaging - local trial", "http://localhost:8000", "0900000901",
    "trial/ stack on this machine (fake 8028)."), indent=2) + "\n", encoding="utf-8")
print("written:", ", ".join(sorted(p.name for p in HERE.glob("*.json"))))
