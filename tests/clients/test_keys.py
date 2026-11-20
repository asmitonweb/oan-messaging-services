"""MSG-06 correction 2: delivery keys are an unambiguous function of the tuple
(service, event_id, role, channel). Pure functions; no network."""

from __future__ import annotations

import itertools
import json
import uuid

import pytest
from oan_client import KEY_ALGORITHM, delivery_key, key_material
from oan_client.keys import OAN_KEY_NAMESPACE

#: Pinned v2 output. If this changes, every stored-but-unfinished delivery would get a
#: different key on recompute, so the algorithm must not change silently.
KNOWN_ANSWER = ("grievance", "ge-1", "farmer", "sms", "2eb210bd-4b29-564d-afd2-eab8eb04d761")


def test_reported_collision_is_gone() -> None:
    assert delivery_key("auth", "a:b", "c", "sms") != delivery_key("auth", "a", "b:c", "sms")


@pytest.mark.parametrize(("left", "right"), [
    (("s:x", "e", "r", "sms"), ("s", "x:e", "r", "sms")),
    (("s", "e:x", "r", "sms"), ("s", "e", "x:r", "sms")),
    (("s", "e", "r:x", "sms"), ("s", "e", "r", "x:sms")),
    (("a:b:c", "d", "e", "f"), ("a", "b:c:d", "e", "f")),
    (("a", "b", "c:d:e", "f"), ("a", "b:c", "d:e", "f")),
    ((":", "::", ":", "sms"), ("::", ":", ":", "sms")),
])
def test_separator_containing_inputs_never_collide(
    left: tuple[str, str, str, str], right: tuple[str, str, str, str],
) -> None:
    assert ":".join(left) == ":".join(right)  # these collided under the old join
    assert key_material(*left) != key_material(*right)
    assert delivery_key(*left) != delivery_key(*right)


def test_exhaustive_small_alphabet_is_injective() -> None:
    parts = ["a", "b", ":", "a:", ":a", "a:b", "-", ".", "a.b"]
    tuples = list(itertools.product(["s", "s:a"], parts, parts, ["sms", "telegram"]))
    keys = {delivery_key(*t) for t in tuples}
    assert len(keys) == len(tuples)


def test_material_is_a_versioned_json_array() -> None:
    m = key_material("auth", "a:b", "c", "sms")
    assert json.loads(m) == [KEY_ALGORITHM, "auth", "a:b", "c", "sms"]
    assert KEY_ALGORITHM == "oan-delivery-key/v2"
    assert delivery_key("auth", "a:b", "c", "sms") == str(uuid.uuid5(OAN_KEY_NAMESPACE, m))


def test_keys_are_stable_across_retries_and_releases() -> None:
    *inputs, expected = KNOWN_ANSWER
    assert delivery_key(*inputs) == expected
    assert all(delivery_key(*inputs) == expected for _ in range(5))
    assert uuid.UUID(expected).version == 5


def test_v2_never_reproduces_the_unreleased_colon_joined_key() -> None:
    # Stored keys must be reused as stored, never recomputed: the algorithms differ.
    legacy = str(uuid.uuid5(OAN_KEY_NAMESPACE, "grievance:ge-1:farmer:sms"))
    assert delivery_key("grievance", "ge-1", "farmer", "sms") != legacy


def test_distinct_roles_events_and_channels_get_distinct_keys() -> None:
    base = delivery_key("grievance", "ge-1", "farmer", "sms")
    assert len({base,
                delivery_key("grievance", "ge-1", "da-agent", "sms"),
                delivery_key("grievance", "ge-2", "farmer", "sms"),
                delivery_key("grievance", "ge-1", "farmer", "telegram"),
                delivery_key("esignet", "ge-1", "farmer", "sms")}) == 5


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, '"quoted"', "a,b", "[x]", "ü"])
def test_inputs_outside_the_token_alphabet_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        delivery_key("svc", bad, "r", "sms")
