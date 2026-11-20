"""Credit (A2C) and DPI Consent Manager examples against contract 3.0.0 and the REAL
FastAPI app (in-memory store, no network, no database). Same checks as the Grievance
example, plus the template, expiry and OTP rules specific to these two callers."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from a2c_notifications import (
    A2C_EVENTS,
    FIELD_LIMITS,
    TEMPLATES,
    A2CNotification,
    TemplateTooLong,
    event_for_transition,
    render,
)
from a2c_notifications import to_delivery as a2c_delivery
from conftest import T0, ApiStore
from dpi_consent_notifications import FIELD_LIMITS as DPI_LIMITS
from dpi_consent_notifications import (
    NOTICE_TEMPLATES,
    ConsentNotice,
    OanOtpSender,
    OtpMessage,
    notice_delivery,
    otp_delivery,
    short_ref,
)
from dpi_consent_notifications import TemplateTooLong as DpiTooLong
from oan_client import OanClient, SmsDelivery
from rapidpro_mapping import fits_one_sms
from test_examples_api import openapi_validate

from oan_messaging.contracts import SmsSubmission

PHONE = "0900000021"
CONSENT_ID = "6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b"
TX_ID = "tx-0c9d8e7f"


def worst(name: str, limits: dict[str, int]) -> str:
    return "W" * limits[name]  # widest GSM-7 glyph; length is what counts


def a2c_note(event: str, **kw: Any) -> A2CNotification:
    base: dict[str, Any] = {
        "delivery_id": str(uuid.uuid4()),
        "event": event,
        "record_id": "VSCH-2026-00042" if event.startswith("visit_") else "APP-2026-00017",
        "farmer_phone": PHONE,
        "message_text": render(
            event,
            bank="Awash Bank",
            app="APP-2026-00017",
            stage="Verified",
            visit="VSCH-2026-00042",
            date="3 Sep",
            time="09:00",
            place="Kebele 04 office",
        ),
    }
    if event.startswith("visit_"):
        base["visit_starts_at"] = T0 + timedelta(days=2)
    base.update(kw)
    return A2CNotification(**base)


def otp(**kw: Any) -> OtpMessage:
    base: dict[str, Any] = {
        "kind": "consent",
        "transaction_id": TX_ID,
        "issue_no": 1,
        "code": "482913",
        "partner": "Awash Bank",
        "expires_at": T0 + timedelta(seconds=300),
        "phone": PHONE,
    }
    base.update(kw)
    return OtpMessage(**base)


def all_deliveries() -> list[SmsDelivery]:
    out = [a2c_delivery(a2c_note(e), T0) for e in A2C_EVENTS]
    out += [otp_delivery(otp(kind=k), T0) for k in ("consent", "data_release")]
    out += [
        notice_delivery(
            ConsentNotice(e, CONSENT_ID, "Awash Bank", PHONE, until="3 Sep 2027", who="you"), T0
        )
        for e in NOTICE_TEMPLATES
    ]
    return out


# ------------------------------------------------------------------ templates


@pytest.mark.parametrize("event", A2C_EVENTS)
def test_every_a2c_template_fits_one_sms_at_maximum_field_lengths(event: str) -> None:
    fields = {k: worst(k, FIELD_LIMITS) for k in FIELD_LIMITS}
    assert fits_one_sms(render(event, **fields))


@pytest.mark.parametrize("event", NOTICE_TEMPLATES)
def test_every_dpi_notice_fits_one_sms_at_maximum_field_lengths(event: str) -> None:
    text = NOTICE_TEMPLATES[event].format(**{k: worst(k, DPI_LIMITS) for k in DPI_LIMITS})
    assert fits_one_sms(text)


@pytest.mark.parametrize("kind", ["consent", "data_release"])
def test_dpi_otp_fits_with_longest_partner_and_code(kind: str) -> None:
    d = otp_delivery(otp(kind=kind, code="12345678", partner="W" * 30), T0)
    assert fits_one_sms(d.text)


def test_a2c_render_refuses_overlong_values_and_untranslated_fallback_is_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TemplateTooLong):
        render("stage_changed", bank="B" * 31, app="APP-2026-00017", stage="x")
    assert render("consent_failed", "om", bank="Awash Bank").startswith("OAN Credit:")
    # A translation that does not fit one Amharic (UCS-2, 70 character) part is refused.
    monkeypatch.setitem(TEMPLATES["consent_failed"], "am", "ፈቃድ " * 30 + "{bank}")
    with pytest.raises(TemplateTooLong):
        render("consent_failed", "am", bank="Awash Bank")


# ------------------------------------------------------------ contract + API


@pytest.mark.parametrize("delivery", all_deliveries(), ids=lambda d: d.source_reference)
def test_bodies_match_the_frozen_contract_and_openapi(delivery: SmsDelivery) -> None:
    body = delivery.body()
    SmsSubmission.model_validate(body)
    openapi_validate("SmsSubmission", body)
    openapi_validate("IdempotencyKeyValue", delivery.idempotency_key)


async def test_every_delivery_is_accepted_then_replayed_by_the_real_api(
    api_client: OanClient, api_store: ApiStore
) -> None:
    deliveries = all_deliveries()
    for d in deliveries:
        first = await api_client.submit(d)
        again = await api_client.submit(d)
        assert (first.replayed, again.replayed, first.id) == (False, True, again.id)
    assert len(api_store.rows) == len(deliveries)


# --------------------------------------------------------------------- A2C


@pytest.mark.parametrize(
    ("status", "stage", "event"),
    [
        ("Rejected", "Rejected", "application_rejected"),
        ("Completed", "Disbursed", "loan_disbursed"),
        ("In Transition", "Approved", "application_approved"),
        ("In Transition", " approved ", "application_approved"),
        ("In Transition", "Verified", "stage_changed"),
        ("In Transition", None, None),
        ("Active", None, None),
        ("Cancelled", None, None),
    ],
)
def test_transition_mapping(status: str, stage: str | None, event: str | None) -> None:
    assert event_for_transition(status, stage) == event


def test_a2c_key_is_the_stored_uuid4_and_reference_names_record_and_event() -> None:
    note = a2c_note("application_submitted")
    d = a2c_delivery(note, T0)
    assert d.idempotency_key == note.delivery_id
    assert d.source_reference == "a2c:APP-2026-00017:application_submitted"
    assert (d.purpose, d.expires_at) == ("transactional", None)
    for bad in ("APP-2026-00017", str(uuid.uuid1())):
        with pytest.raises(ValueError):
            a2c_delivery(a2c_note("application_submitted", delivery_id=bad), T0)


def test_visit_messages_expire_at_the_visit_capped_below_the_horizon() -> None:
    soon = a2c_delivery(a2c_note("visit_scheduled"), T0)
    assert soon.expires_at == T0 + timedelta(days=2)
    far = a2c_delivery(a2c_note("visit_scheduled", visit_starts_at=T0 + timedelta(days=20)), T0)
    assert far.expires_at == T0 + timedelta(days=7) - timedelta(minutes=5)
    with pytest.raises(ValueError):
        a2c_delivery(a2c_note("visit_cancelled", visit_starts_at=T0), T0)
    with pytest.raises(ValueError):
        a2c_delivery(a2c_note("visit_scheduled", visit_starts_at=None), T0)


def test_a2c_repr_hides_phone_and_text() -> None:
    text = repr(a2c_note("loan_disbursed"))
    assert PHONE not in text and "Awash" not in text and "loan_disbursed" in text


# --------------------------------------------------------------------- DPI


def test_otp_is_purpose_otp_with_its_own_deadline_and_stable_key() -> None:
    first = otp_delivery(otp(), T0)
    assert (first.purpose, first.expires_at) == ("otp", T0 + timedelta(seconds=300))
    assert "expires in 5 minutes" in first.text
    assert otp_delivery(otp(), T0).idempotency_key == first.idempotency_key  # retry
    resend = otp_delivery(otp(issue_no=2, code="771204"), T0)
    assert resend.idempotency_key != first.idempotency_key  # new code = new delivery
    assert "482913" not in first.source_reference and PHONE not in first.source_reference


def test_otp_repr_hides_code_and_phone() -> None:
    text = repr(otp())
    assert "482913" not in text and PHONE not in text


@pytest.mark.parametrize(
    "kw",
    [{"code": "12ab56"}, {"code": "123"}, {"issue_no": 0}, {"kind": "login"}, {"expires_at": T0}],
)
def test_invalid_otp_messages_are_refused(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        otp_delivery(otp(**kw), T0)


def test_overlong_partner_is_refused() -> None:
    with pytest.raises(DpiTooLong):
        otp_delivery(otp(partner="P" * 31), T0)


def test_notices_have_distinct_keys_and_short_refs() -> None:
    keys = {d.idempotency_key for d in all_deliveries()}
    assert len(keys) == len(all_deliveries())
    assert short_ref(CONSENT_ID) == "9C0D1E2F3A4B"[-8:]
    granted = notice_delivery(
        ConsentNotice("consent_granted", CONSENT_ID, "Awash Bank", PHONE, until="3 Sep 2027"),
        T0,
    )
    assert "until 3 Sep 2027" in granted.text and "Ref 1E2F3A4B" in granted.text


class Phones:
    def __init__(self, known: dict[str, str]) -> None:
        self.known = known
        self.asked: list[tuple[str, str]] = []

    async def phone_for(self, subject_id_type: str, subject_id_value: str) -> str | None:
        self.asked.append((subject_id_type, subject_id_value))
        return self.known.get(subject_id_value)


async def test_otp_sender_delivers_through_the_real_api_and_skips_unknown_phones(
    api_client: OanClient, api_store: ApiStore
) -> None:
    sender = OanOtpSender(api_client, Phones({"FIN-1234": PHONE}))
    kw: dict[str, Any] = {
        "kind": "data_release",
        "transaction_id": TX_ID,
        "issue_no": 1,
        "code": "482913",
        "partner": "Awash Bank",
        "subject_id_type": "FIN",
        "issued_at": T0,
        "expires_at": T0 + timedelta(seconds=300),
    }
    sub = await sender.send(subject_id_value="FIN-1234", **kw)
    assert sub is not None and sub.status == "accepted"
    assert sub.expires_at == "2026-09-01T05:17:13Z"  # T0 + 300 s, unchanged
    assert await sender.send(subject_id_value="FIN-9999", **kw) is None
    assert len(api_store.rows) == 1
