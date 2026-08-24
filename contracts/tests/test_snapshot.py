"""A snapshot manifest is refused while it is being written, not at restore.

The operator tool that takes Host backups names what it cannot copy rather than
including something that restores into a subtly wrong state. Memory was on that
list. Taking it off means declaring two things a reader cannot infer: whether
the copy is complete, and which embedder the vectors in it were produced under.

Both are checked here at construction, because a manifest that only fails at
restore time is a backup that looks valid for as long as nobody needs it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eidolon_memory_contracts import (
    LEDGERS_PREFIX,
    PALACE_PREFIX,
    REQUIRED_ENTRIES,
    RealmSnapshot,
    SnapshotEntry,
)


def _entry(path: str, *, size: int = 1) -> SnapshotEntry:
    return SnapshotEntry(
        path=path,
        method="sqlite-vacuum-into",
        sha256="a" * 64,
        bytes=size,
    )


def _snapshot(paths: tuple[str, ...], **overrides) -> RealmSnapshot:
    fields = {
        "memory_space_id": "r_06607258a65055708c91880e8f2fb9a9",
        "taken_at": "2026-08-24T04:00:00Z",
        "embedder_identity": "bge_base_zh_v15",
        "embedder_dimension": 768,
        "entries": tuple(_entry(path) for path in paths),
    }
    fields.update(overrides)
    return RealmSnapshot(**fields)


def test_a_complete_snapshot_is_accepted() -> None:
    snapshot = _snapshot(REQUIRED_ENTRIES)
    assert snapshot.contract_version == "1"
    assert snapshot.operation == "memory.realm-snapshot"
    assert snapshot.total_bytes == len(REQUIRED_ENTRIES)


def test_the_required_set_is_the_vectors_and_every_ledger() -> None:
    """Stated as a test so the set cannot shrink by accident.

    Each of these answers questions on its own: the vectors are recall, the
    graph is the entity view, canonical facts hold the invalidation chain that
    stops a corrected fact coming back, commitments are what commitment queries
    read. A copy missing one restores into a space that answers wrongly rather
    than one that answers less.
    """
    assert f"{PALACE_PREFIX}/chroma.sqlite3" in REQUIRED_ENTRIES
    ledgers = {path for path in REQUIRED_ENTRIES if path.startswith(f"{LEDGERS_PREFIX}/")}
    assert ledgers == {
        f"{LEDGERS_PREFIX}/knowledge_graph.sqlite3",
        f"{LEDGERS_PREFIX}/canonical_facts.sqlite3",
        f"{LEDGERS_PREFIX}/commitments.sqlite3",
        f"{LEDGERS_PREFIX}/command_status.sqlite3",
        f"{LEDGERS_PREFIX}/dlq.sqlite3",
        f"{LEDGERS_PREFIX}/extraction_decisions.sqlite3",
        f"{LEDGERS_PREFIX}/sync_ledger.sqlite3",
    }


@pytest.mark.parametrize("dropped", REQUIRED_ENTRIES)
def test_dropping_any_required_file_is_refused_and_named(dropped: str) -> None:
    remaining = tuple(path for path in REQUIRED_ENTRIES if path != dropped)
    with pytest.raises(ValidationError) as caught:
        _snapshot(remaining)
    assert dropped in str(caught.value)


def test_optional_files_may_be_present_without_being_required() -> None:
    """Markers and config travel with the copy; their absence is not a defect.

    ``mempalace.yaml`` and the embedder marker are written by MemPalace and read
    back by it. They are copied when present, but a space that has not written
    one yet is not an incomplete space.
    """
    extras = (
        f"{PALACE_PREFIX}/mempalace.yaml",
        f"{PALACE_PREFIX}/mempalace_embedder.json",
    )
    snapshot = _snapshot(REQUIRED_ENTRIES + extras)
    assert len(snapshot.entries) == len(REQUIRED_ENTRIES) + 2


def test_a_repeated_path_is_refused() -> None:
    with pytest.raises(ValidationError, match="same path twice"):
        _snapshot(REQUIRED_ENTRIES + (REQUIRED_ENTRIES[0],))


def test_the_embedder_it_was_taken_under_is_not_optional() -> None:
    """Vectors mean nothing without it, and the vector store already enforces it.

    A collection is stamped with an embedder *name* and refuses to open under a
    different one, so a snapshot that did not record it could only be restored by
    guessing. The width is different: real MemPalace markers record
    ``dimension: 0``, meaning unset, so requiring a width would make an actual
    palace unsnapshottable. It is recorded when known and absent when not —
    absent is checkable, and zero is a lie that validates.
    """
    for omitted in ("embedder_identity",):
        fields = {
            "memory_space_id": "r_a",
            "taken_at": "2026-08-24T04:00:00Z",
            "embedder_identity": "bge_base_zh_v15",
            "embedder_dimension": 768,
            "entries": tuple(_entry(path) for path in REQUIRED_ENTRIES),
        }
        del fields[omitted]
        with pytest.raises(ValidationError):
            RealmSnapshot(**fields)


def test_a_plain_copy_is_distinguishable_from_a_consistent_one() -> None:
    """The reader must not have to assume how a file was copied.

    A live SQLite database read with a plain file copy can be torn; the same
    file taken with ``VACUUM INTO`` cannot. Recording which was used keeps that
    difference checkable instead of conventional.
    """
    entries = [_entry(path) for path in REQUIRED_ENTRIES]
    marker = SnapshotEntry(
        path=f"{PALACE_PREFIX}/.collection_type_fixed",
        method="file-copy",
        sha256="b" * 64,
        bytes=0,
    )
    snapshot = _snapshot(REQUIRED_ENTRIES, entries=tuple(entries) + (marker,))
    methods = {entry.method for entry in snapshot.entries}
    assert methods == {"sqlite-vacuum-into", "file-copy"}


def test_an_unrecorded_width_is_absent_rather_than_zero() -> None:
    """The case a real palace produces.

    MemPalace writes ``dimension: 0`` when it has not recorded one. Storing that
    verbatim would let a restore compare against a width nothing has; refusing
    the snapshot over it would leave memory on the operator tool's uncovered
    list, which is what this contract exists to fix.
    """
    snapshot = RealmSnapshot(
        memory_space_id="r_a",
        taken_at="2026-08-24T04:00:00Z",
        embedder_identity="embeddinggemma",
        entries=tuple(_entry(path) for path in REQUIRED_ENTRIES),
    )

    assert snapshot.embedder_dimension is None

    with pytest.raises(ValidationError):
        RealmSnapshot(
            memory_space_id="r_a",
            taken_at="2026-08-24T04:00:00Z",
            embedder_identity="embeddinggemma",
            embedder_dimension=0,
            entries=tuple(_entry(path) for path in REQUIRED_ENTRIES),
        )
