"""The Owner surface contract: combinations that would contradict a status.

A forget preview is read by a phone that offers a delete button only when there
is a token. These invariants make the states a client could misread
unrepresentable at the one declaration both processes share.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eidolon_memory_contracts.owner import (
    MemoryBrowse,
    OwnerForgetEntry,
    OwnerForgetPreview,
)

ENTRY = OwnerForgetEntry(entry_id="drawer_1", preview="乌龙茶", score=1.0)


def test_a_preview_carries_entries_a_token_and_an_expiry() -> None:
    OwnerForgetPreview(
        status="preview",
        target="乌龙茶",
        entries=(ENTRY,),
        confirmation_token="t",
        expires_at=1,
    )
    for missing in (
        {"entries": ()},
        {"confirmation_token": None},
        {"expires_at": None},
    ):
        fields = {
            "status": "preview",
            "target": "乌龙茶",
            "entries": (ENTRY,),
            "confirmation_token": "t",
            "expires_at": 1,
            **missing,
        }
        with pytest.raises(ValidationError):
            OwnerForgetPreview(**fields)


@pytest.mark.parametrize("status", ["not_found", "too_broad"])
def test_nothing_offered_means_nothing_to_confirm(status: str) -> None:
    OwnerForgetPreview(status=status, target="x")
    with pytest.raises(ValidationError):
        OwnerForgetPreview(status=status, target="x", entries=(ENTRY,))
    with pytest.raises(ValidationError):
        OwnerForgetPreview(status=status, target="x", confirmation_token="t")


def test_there_is_no_archive_for_an_owner() -> None:
    """An Owner forget is a delete; the model has no field to say otherwise."""
    with pytest.raises(ValidationError):
        OwnerForgetPreview(status="not_found", target="x", action="archive")


def test_an_unknown_field_is_a_contract_change() -> None:
    with pytest.raises(ValidationError):
        MemoryBrowse.model_validate(
            {
                "memory_space_id": "r",
                "audience_scope": "owner",
                "entry_count": 0,
                "withheld_count": 0,
                "truncated": False,
                "materialization": {},
            }
        )
