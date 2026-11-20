"""Examples against the REAL accepted FastAPI app (oan_messaging.api) via ASGITransport,
with the in-memory ``ApiStore`` mock. Validates request compatibility with contract 3.0.0
(Pydantic models + OpenAPI schema) and replay semantics. No network, no database."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
import yaml
from auth_password_reset import ResetOtpIssued
from auth_password_reset import to_delivery as auth_delivery
from conftest import FAKE_TOKEN, ROOT, T0, ApiStore, FakeClock
from da_visits import VisitEvent, VisitRecipient, to_deliveries
from esignet_otp import OtpNotification
from esignet_otp import to_delivery as otp_delivery
from grievance_notifications import GrievanceEvent
from grievance_notifications import to_delivery as grievance_delivery
from oan_client import OanClient, OanConflict, OanRejected, SmsDelivery, delivery_key
from telegram_notify import TelegramNotice
from telegram_notify import to_delivery as tg_delivery

from oan_messaging.contracts import (
    FailureCode,
    MessageStatus,
    SmsSubmission,
    TelegramSubmission,
)

SPEC = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]


def _rewrite(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: (v.replace("#/components/schemas/", "#/$defs/") if k == "$ref"
                    else _rewrite(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite(v) for v in node]
    return node


def openapi_validate(schema_name: str, value: Any) -> None:
    jsonschema.validate(value, {"$defs": _rewrite(SCHEMAS),
                                "$ref": f"#/$defs/{schema_name}"},
                        format_checker=jsonschema.FormatChecker())


AUTH_DELIVERY_ID = "3d6f0a8e-2b1c-4e5f-9a7b-1c2d3e4f5a6b"
GRIEVANCE_IDS = ("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")
#: Auth's own template at 34c7306 (rendered by Auth, never by OAN or this example).
AUTH_TEXT = "482913 is your password reset code. It expires in 10 minutes."


def auth_event(**kw: Any) -> ResetOtpIssued:
    base: dict[str, Any] = {
        "delivery_id": AUTH_DELIVERY_ID, "reset_request_id": "pr-1001",
        "mobile_no": "0900000002", "message_text": AUTH_TEXT, "issued_at": T0,
        "otp_expires_at": T0 + timedelta(seconds=600)}
    base.update(kw)
    return ResetOtpIssued(**base)


def all_example_deliveries() -> list[Any]:
    exp = T0 + timedelta(minutes=5)
    return [
        otp_delivery(OtpNotification("tx-7f3c9a", 1, "+251900000001",
                                     "Your OAN code is 482913. It expires in 5 minutes.",
                                     exp), T0),
        auth_delivery(auth_event()),
        grievance_delivery(GrievanceEvent(GRIEVANCE_IDS[0], "acknowledged", "G-1042",
                                          "0900000003", "ቅሬታዎ G-1042 ተቀብለናል። እናመሰግናለን።"), T0),
        grievance_delivery(GrievanceEvent(GRIEVANCE_IDS[1], "deadline_extended", "G-1042",
                                          "0900000003", "Grievance G-1042: officer visit booked."),
                           T0),
        *to_deliveries(VisitEvent("ve-1", "V-77", "planned", (
            VisitRecipient("farmer", "0900000004", "DA visit planned: 3 Sep 09:00."),
            VisitRecipient("da-agent", "0700000005", "Visit V-77 with farmer, 3 Sep 09:00."),
        ), T0 + timedelta(days=2)), T0),
        tg_delivery(TelegramNotice("grievance", "ge-3", 555000111,
                                   "Your grievance G-1042 was updated."), T0),
    ]


@pytest.mark.parametrize("delivery", all_example_deliveries(),
                         ids=lambda d: d.source_reference)
def test_example_bodies_match_the_frozen_contract_and_openapi(delivery: Any) -> None:
    body = delivery.body()
    if isinstance(delivery, SmsDelivery):
        SmsSubmission.model_validate(body)
        openapi_validate("SmsSubmission", body)
    else:
        TelegramSubmission.model_validate(body)
        openapi_validate("TelegramSubmission", body)
    openapi_validate("IdempotencyKeyValue", delivery.idempotency_key)


async def test_every_example_is_accepted_then_replayed_by_the_real_api(
    api_client: OanClient, api_store: ApiStore,
) -> None:
    deliveries = all_example_deliveries()
    ids = []
    for d in deliveries:
        first = await api_client.submit(d)
        again = await api_client.submit(d)  # e.g. after a lost response
        assert (first.replayed, again.replayed) == (False, True)
        assert first.id == again.id and first.idempotency_key == d.idempotency_key
        ids.append(first.id)
    assert len(set(ids)) == len(deliveries)  # one message per intended delivery
    assert len(api_store.rows) == len(deliveries)


async def test_esignet_expiry_is_passed_through_unchanged(
    api_client: OanClient, api_store: ApiStore,
) -> None:
    d = all_example_deliveries()[0]
    sub = await api_client.submit(d)
    assert api_store.by_id[uuid.UUID(sub.id)].expires_at == d.expires_at


async def test_changed_content_under_same_key_is_a_conflict(api_client: OanClient) -> None:
    d = all_example_deliveries()[1]
    await api_client.submit(d)
    changed = SmsDelivery(**{**d.__dict__, "text": "Different text"})
    with pytest.raises(OanConflict):
        await api_client.submit(changed)


async def test_amharic_part_limit_is_three_parts_of_67(api_client: OanClient) -> None:
    def text_of(text: str, key: str, purpose: str = "transactional") -> SmsDelivery:
        return SmsDelivery(idempotency_key=key, recipient="0900000006", text=text,
                           source_reference="grievance:G-1:e", purpose=purpose,
                           expires_at=None, first_submitted_at=T0)

    # One part is 70 Amharic characters; three concatenated parts are 3 x 67 = 201.
    assert (await api_client.submit(text_of("ሀ" * 70, "k-amharic-70"))).status == "accepted"
    assert (await api_client.submit(text_of("ሀ" * 201, "k-amharic-201"))).status == "accepted"
    with pytest.raises(OanRejected) as info:
        await api_client.submit(text_of("ሀ" * 202, "k-amharic-202"))
    assert info.value.status == 422 and ("text", "too_long_for_sms") in info.value.errors
    # One Amharic character in English text makes the whole message UCS-2.
    with pytest.raises(OanRejected):
        await api_client.submit(text_of("a" * 201 + "ሀ", "k-mixed-202"))
    english = text_of("a" * 459, "k-english-459", "alert")
    assert (await api_client.submit(english)).status == "accepted"


async def test_unknown_outcome_is_explained_and_never_resubmitted(
    api_client: OanClient, api_store: ApiStore,
) -> None:
    sub = await api_client.submit(all_example_deliveries()[3])
    mid = uuid.UUID(sub.id)
    api_store.set_status(mid, status=MessageStatus.SENDING, attempts=1, last_attempt_at=T0)
    api_store.set_status(mid, status=MessageStatus.UNKNOWN, failure_code=FailureCode.TIMEOUT,
                         provider_status="timeout", content_purged_at=T0)
    st = await api_client.get_status(sub.id)
    assert st.status == "unknown" and st.may_resubmit is False
    assert len(api_store.rows) == 1


async def test_telegram_status_never_claims_delivery(
    api_client: OanClient, api_store: ApiStore,
) -> None:
    sub = await api_client.submit(all_example_deliveries()[-1])
    st = await api_client.get_status(sub.id)
    assert st.channel == "telegram" and st.delivery_receipts_supported is False


async def test_otp_without_expiry_is_impossible_to_build() -> None:
    with pytest.raises(ValueError):
        SmsDelivery(idempotency_key="k-otp-noexp", recipient="0900000001", text="1",
                    source_reference="esignet:otp:x", purpose="otp", expires_at=None,
                    first_submitted_at=T0)


def test_delivery_keys_are_stable_opaque_and_distinct() -> None:
    a1 = delivery_key("grievance", "ge-1", "farmer", "sms")
    assert a1 == delivery_key("grievance", "ge-1", "farmer", "sms")  # stable across retries
    assert a1 != delivery_key("grievance", "ge-1", "da-agent", "sms")
    assert a1 != delivery_key("grievance", "ge-2", "farmer", "sms")
    assert a1 != delivery_key("grievance", "ge-1", "farmer", "telegram")
    uuid.UUID(a1)
    for pii in ("0912345678", "251912345678", "evt-0712345678"):
        with pytest.raises(ValueError):
            delivery_key("svc", pii, "farmer", "sms")
    # UUID-shaped event ids with all-digit groups are not mistaken for phones.
    assert delivery_key("svc", "123e4567-e89b-12d3-a456-426614174000", "u", "sms")


async def test_da_visit_beyond_the_horizon_expires_inside_it_and_is_accepted(
    api_client: OanClient, api_store: ApiStore,
) -> None:
    """OI-30: a visit 20 days ahead is notified now, with an expiry capped 5 minutes inside
    the 7-day horizon (never the visit time), so the real API accepts it. A retry built
    from the same first_submitted_at gives the same key and the same deadline."""
    event = VisitEvent("ve-far", "V-90", "planned", (
        VisitRecipient("farmer", "0900000004", "DA visit planned: 21 Sep 09:00."),
    ), T0 + timedelta(days=20))
    [d] = to_deliveries(event, T0)
    assert d.expires_at == T0 + timedelta(days=7) - timedelta(minutes=5)
    sub = await api_client.submit(d)
    assert sub.status == "accepted"
    assert api_store.by_id[uuid.UUID(sub.id)].expires_at == d.expires_at
    [again] = to_deliveries(event, T0)
    assert (again.idempotency_key, again.expires_at) == (d.idempotency_key, d.expires_at)
    # A visit already started sends nothing.
    with pytest.raises(ValueError):
        to_deliveries(VisitEvent("ve-past", "V-91", "planned", event.recipients, T0), T0)


def test_da_events_need_unique_roles() -> None:
    with pytest.raises(ValueError):
        to_deliveries(VisitEvent("ve-2", "V-1", "rescheduled", (
            VisitRecipient("farmer", "0900000004", "x"),
            VisitRecipient("farmer", "0900000007", "y")), T0 + timedelta(days=1)), T0)


# -------------------------------------------- handoff-pack JSON examples (docs/integrations)


EXAMPLE_FILES = sorted((ROOT / "docs" / "integrations" / "examples").glob("*.json"))


def test_handoff_examples_exist() -> None:
    assert len(EXAMPLE_FILES) >= 6


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.name)
def test_handoff_json_examples_match_the_contract(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    schema = doc["schema"]
    openapi_validate(schema, doc["body"])
    models: dict[str, type[SmsSubmission] | type[TelegramSubmission]] = {
        "SmsSubmission": SmsSubmission, "TelegramSubmission": TelegramSubmission}
    model = models.get(schema)
    if model is not None:
        model.model_validate(doc["body"])
        openapi_validate("IdempotencyKeyValue", doc["headers"]["Idempotency-Key"])


async def test_handoff_request_examples_round_trip_through_the_real_api(
    api_app: Any, clock: FakeClock,
) -> None:
    clock.now = T0
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://oan.test") as http:
        for path in EXAMPLE_FILES:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if "request" not in doc:
                continue
            req = doc["request"]
            headers = {**doc["headers"], "Authorization": f"Bearer {FAKE_TOKEN}"}
            r = await http.request(req["method"], req["path"], headers=headers,
                                   content=json.dumps(doc["body"]).encode())
            assert r.status_code == doc["expect_status"], (path.name, r.text)


# ------------------------------------------------ MSG-06 correction 4: Auth / Grievance


async def test_auth_reset_is_an_otp_with_auths_own_deadline(
    api_client: OanClient, api_store: ApiStore,
) -> None:
    d = auth_delivery(auth_event())
    assert (d.purpose, d.idempotency_key) == ("otp", AUTH_DELIVERY_ID)
    assert d.expires_at == T0 + timedelta(seconds=600) and d.first_submitted_at == T0
    assert d.text == AUTH_TEXT  # Auth's template, byte for byte
    assert d.source_reference == "auth:password-reset:pr-1001"
    sub = await api_client.submit(d)
    row = api_store.by_id[uuid.UUID(sub.id)]
    assert row.purpose.value == "otp" and row.expires_at == T0 + timedelta(seconds=600)
    assert sub.status == "accepted"  # accepted by OAN, not sent


def test_auth_example_never_extends_or_invents_a_deadline() -> None:
    for bad in ({"otp_expires_at": T0 + timedelta(minutes=31)},
                {"otp_expires_at": T0}, {"otp_expires_at": T0 - timedelta(seconds=1)}):
        with pytest.raises(ValueError):
            auth_delivery(auth_event(**bad))
    ok = auth_delivery(auth_event(otp_expires_at=T0 + timedelta(minutes=30)))
    assert ok.expires_at == T0 + timedelta(minutes=30)


@pytest.mark.parametrize("bad_id", ["pr-1001", "GR-NOTIF-000001",
                                    "6f1d2c3e-8a4b-1c5d-9e6f-7a8b9c0d1e2f"])
def test_auth_and_grievance_keys_must_be_stored_uuid4s(bad_id: str) -> None:
    with pytest.raises(ValueError):
        auth_delivery(auth_event(delivery_id=bad_id))
    with pytest.raises(ValueError):
        grievance_delivery(GrievanceEvent(bad_id, "acknowledged", "G-1", "0900000003", "x"), T0)


def test_auth_resend_is_a_new_delivery_with_a_new_key() -> None:
    from auth_password_reset import new_delivery_id

    first, second = new_delivery_id(), new_delivery_id()
    assert first != second and uuid.UUID(first).version == 4
    a = auth_delivery(auth_event(delivery_id=first))
    b = auth_delivery(auth_event(delivery_id=second, message_text="105822 is your password "
                                 "reset code. It expires in 10 minutes."))
    assert a.idempotency_key != b.idempotency_key


def test_auth_event_repr_hides_otp_and_phone() -> None:
    shown = repr(auth_event()) + repr(auth_delivery(auth_event()))
    assert "482913" not in shown and "0900000002" not in shown and AUTH_DELIVERY_ID not in shown


def test_grievance_event_names_fit_the_source_reference_pattern() -> None:
    d = grievance_delivery(GrievanceEvent(GRIEVANCE_IDS[1], "deadline_extended", "G-1042",
                                          "0900000003", "x"), T0)
    assert d.source_reference == "grievance:G-1042:deadline_extended"
    assert d.idempotency_key == GRIEVANCE_IDS[1]
