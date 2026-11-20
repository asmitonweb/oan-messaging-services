"""Proposed RapidPro bridge mapping (mock; no connector exists). Verified courier facts
are encoded as assertions so a future courier upgrade can be re-checked."""

from __future__ import annotations

import pytest
from conftest import T0, FakeClock
from oan_client import OanClient
from rapidpro_mapping import (
    UnsupportedMessage,
    courier_would_split,
    fits_one_sms,
    map_courier_request,
)

from oan_messaging.contracts import SmsSubmission

UUID = "7a3c1e2b-9f40-4d1a-8e2b-3c4d5e6f7a8b"


def payload(**kw: object) -> dict[str, object]:
    return {"uuid": UUID, "to": "+251900000008", "text": "Rain expected tomorrow.",
            "channel": "0f0e0d0c-0b0a-4908-8706-050403020100", **kw}


def test_mapping_uses_message_uuid_for_key_and_reference() -> None:
    d = map_courier_request(payload(), purpose="broadcast", first_submitted_at=T0)
    assert d.idempotency_key == f"rapidpro:{UUID}" == d.source_reference
    SmsSubmission.model_validate(d.body())


def test_courier_splits_amharic_by_bytes_long_before_the_sms_limit() -> None:
    amharic_60 = "ሀ" * 60
    assert fits_one_sms(amharic_60)  # OAN would accept it as one SMS ...
    assert courier_would_split(amharic_60, 160)  # ... but default max_length splits it
    assert not courier_would_split(amharic_60, 640)  # hence max_length=640 in rapidpro.md
    assert courier_would_split("ሀ" * 54, 160) and not courier_would_split("ሀ" * 53, 160)


@pytest.mark.parametrize("bad", [
    {"text": "ሀ" * 71}, {"text": "a" * 161}, {"text": "  "}, {"to": "0900000008"},
    {"to": "+15555550100"}, {"uuid": "not-a-uuid"},
])
def test_unsupported_messages_are_refused_before_calling_oan(bad: dict[str, object]) -> None:
    with pytest.raises(UnsupportedMessage):
        map_courier_request(payload(**bad), purpose="broadcast", first_submitted_at=T0)


def test_rapidpro_never_sends_otps() -> None:
    with pytest.raises(UnsupportedMessage):
        map_courier_request(payload(), purpose="otp", first_submitted_at=T0)


async def test_courier_retry_of_same_message_is_an_oan_replay(
    api_client: OanClient, clock: FakeClock,
) -> None:
    d = map_courier_request(payload(), purpose="broadcast", first_submitted_at=T0)
    first = await api_client.submit(d)
    retry = await api_client.submit(map_courier_request(payload(), purpose="broadcast",
                                                        first_submitted_at=T0))
    assert first.id == retry.id and retry.replayed  # no second SMS


# ---------------------------------------- MSG-06 correction 5: splitting is not solved


def test_max_length_640_does_not_disable_splitting_of_longer_input() -> None:
    assert not courier_would_split("a" * 640, 640)
    assert courier_would_split("a" * 641, 640)  # still split
    assert courier_would_split("ሀ" * 214, 640)  # 642 bytes of Amharic


def test_every_valid_pilot_message_survives_max_length_640_unsplit() -> None:
    from rapidpro_mapping import GSM7_BASIC

    widest_gsm = max(GSM7_BASIC, key=lambda c: len(c.encode("utf-8")))
    for text in (widest_gsm * 160, "ሀ" * 70, "😀" * 35, "€" * 80):
        assert fits_one_sms(text) and not courier_would_split(text, 640)


def test_mapping_cannot_detect_a_fragment_of_a_split_message() -> None:
    original = ("Rain expected tomorrow across the region. " * 20).strip()
    assert courier_would_split(original, 640) and not fits_one_sms(original)
    fragment = original[:150]  # what courier could send first, with the SAME uuid
    d = map_courier_request(payload(text=fragment), purpose="broadcast",
                            first_submitted_at=T0)
    assert d.text == fragment  # accepted as if complete: a truncated message


def test_non_splitting_needs_a_hard_input_bound_and_4_bytes_per_character() -> None:
    from rapidpro_mapping import is_non_splitting

    assert is_non_splitting(640, 160)
    assert not is_non_splitting(639, 160) and not is_non_splitting(640, 161)
    assert not is_non_splitting(160, 160)  # courier's default
    with pytest.raises(ValueError):
        is_non_splitting(640, 0)
