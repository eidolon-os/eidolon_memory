"""Audience is the visibility axis between an owner's companions."""

import pytest

from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    audience_companion_id,
    companion_audience,
    is_companion_audience,
    readable_audiences,
    validate_audience,
)


def test_owner_layer_is_readable_by_every_companion() -> None:
    for companion in ("comp_a", "comp_b"):
        assert OWNER_AUDIENCE in readable_audiences(companion)


def test_companion_cannot_read_another_companions_layer() -> None:
    readable = readable_audiences("comp_a")

    assert companion_audience("comp_a") in readable
    assert companion_audience("comp_b") not in readable


def test_unidentified_caller_sees_owner_layer_only() -> None:
    for missing in (None, "", "   "):
        assert readable_audiences(missing) == (OWNER_AUDIENCE,)


def test_audience_round_trips_through_its_companion_id() -> None:
    assert audience_companion_id(companion_audience("comp_a")) == "comp_a"
    assert audience_companion_id(OWNER_AUDIENCE) is None


def test_owner_audience_is_not_a_companion_audience() -> None:
    assert not is_companion_audience(OWNER_AUDIENCE)
    assert is_companion_audience(companion_audience("comp_a"))


@pytest.mark.parametrize("bad", ["", "   ", "everyone", "companion:", "companion:  "])
def test_unknown_audience_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_audience(bad)


@pytest.mark.parametrize("unsafe", ["a b", "a'b", "a;drop", "a/b", "a,b"])
def test_companion_id_that_would_be_unsafe_in_a_filter_is_rejected(unsafe: str) -> None:
    """Audience tokens land in metadata keys and filter expressions."""

    with pytest.raises(ValueError):
        companion_audience(unsafe)


def test_audience_accepts_surrounding_whitespace() -> None:
    assert validate_audience(f"  {OWNER_AUDIENCE}  ") == OWNER_AUDIENCE
