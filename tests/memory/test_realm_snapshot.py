"""A copy of one space, taken while it is live, and provably whole.

The Host backup tool lists memory as state it does not carry, with the reason
"palace, vector index and knowledge graph have no declared snapshot", and its
own comment says the fix is for the owning component to say how it is copied and
how the copy is checked. This is that, exercised against real SQLite files
rather than mocks — the properties being claimed are SQLite's, so a fake would
be claiming them on its behalf.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from eidolon_memory_contracts.snapshot import (
    LEDGERS_PREFIX,
    PALACE_PREFIX,
    REQUIRED_ENTRIES,
)

from eidolon.memory.config.palace_directory import LEDGER_FILENAMES
from eidolon.memory.infrastructure.realm_snapshot import (
    MANIFEST_NAME,
    RestoreError,
    SnapshotError,
    restore_realm_snapshot,
    verify_realm_snapshot,
    write_realm_snapshot,
)


def _database(path: Path, *, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS kept (id INTEGER, note TEXT)")
        connection.executemany(
            "INSERT INTO kept VALUES (?, ?)",
            [(index, f"note-{index}") for index in range(rows)],
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def space(tmp_path: Path) -> tuple[Path, Path]:
    """A palace and its sibling ledgers, shaped like one on disk."""
    palace = tmp_path / "b64_space"
    ledgers = tmp_path / "b64_space.ledgers"
    _database(palace / "chroma.sqlite3", rows=5)
    (palace / "mempalace.yaml").write_text("engine: chroma\n", encoding="utf-8")
    (palace / "mempalace_embedder.json").write_text("{}\n", encoding="utf-8")
    (palace / ".collection_type_fixed").write_text("", encoding="utf-8")
    for name in LEDGER_FILENAMES:
        _database(ledgers / name)
    return palace, ledgers


def _take(space: tuple[Path, Path], destination: Path, **overrides):
    palace, ledgers = space
    fields = {
        "palace_path": palace,
        "ledgers_path": ledgers,
        "destination": destination,
        "memory_space_id": "r_06607258a65055708c91880e8f2fb9a9",
        "owner_id": "o_1",
        "embedder_identity": "bge_base_zh_v15",
        "embedder_dimension": 768,
    }
    fields.update(overrides)
    return write_realm_snapshot(**fields)


def test_a_snapshot_carries_the_vectors_and_every_ledger(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    snapshot = _take(space, tmp_path / "copy")

    paths = {entry.path for entry in snapshot.entries}
    assert set(REQUIRED_ENTRIES) <= paths
    for name in LEDGER_FILENAMES:
        assert (tmp_path / "copy" / LEDGERS_PREFIX / name).is_file()
    assert (tmp_path / "copy" / PALACE_PREFIX / "chroma.sqlite3").is_file()


def test_the_copy_is_readable_sqlite_with_the_same_rows(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    """``VACUUM INTO`` is claimed to produce a usable database, so check that."""
    _take(space, tmp_path / "copy")
    copied = tmp_path / "copy" / PALACE_PREFIX / "chroma.sqlite3"
    connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM kept").fetchone()[0] == 5
    finally:
        connection.close()


def test_config_and_markers_travel_as_plain_copies(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    snapshot = _take(space, tmp_path / "copy")
    methods = {entry.path: entry.method for entry in snapshot.entries}
    assert methods[f"{PALACE_PREFIX}/mempalace.yaml"] == "file-copy"
    assert methods[f"{PALACE_PREFIX}/chroma.sqlite3"] == "sqlite-vacuum-into"
    # Absent markers are not defects: this space never wrote one.
    assert f"{PALACE_PREFIX}/.blob_seq_ids_migrated" not in methods


def test_the_manifest_records_the_embedder_it_was_taken_under(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    _take(space, tmp_path / "copy")
    written = json.loads((tmp_path / "copy" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert written["embedder_identity"] == "bge_base_zh_v15"
    assert written["embedder_dimension"] == 768
    assert written["operation"] == "memory.realm-snapshot"


def test_verify_accepts_the_copy_it_just_took(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    taken = _take(space, tmp_path / "copy")
    assert verify_realm_snapshot(tmp_path / "copy") == taken


def test_verify_notices_a_changed_file(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    """A digest nobody re-checks is a digest nobody is using."""
    _take(space, tmp_path / "copy")
    graph = tmp_path / "copy" / LEDGERS_PREFIX / "knowledge_graph.sqlite3"
    graph.write_bytes(graph.read_bytes() + b"tampered")
    # ``RestoreError``: the two moments that verify a copy ask the same question,
    # and the answer that matters is "do not rely on this", not which caller
    # asked.
    with pytest.raises(RestoreError, match="knowledge_graph"):
        verify_realm_snapshot(tmp_path / "copy")


def test_verify_notices_a_missing_file(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    _take(space, tmp_path / "copy")
    (tmp_path / "copy" / LEDGERS_PREFIX / "commitments.sqlite3").unlink()
    with pytest.raises(RestoreError, match="commitments"):
        verify_realm_snapshot(tmp_path / "copy")


def test_a_missing_ledger_refuses_the_snapshot_and_names_it(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    """Refused when taken, not when needed.

    A copy silently missing an append-only ledger restores into a space that
    answers questions wrongly — a corrected fact coming back, a commitment
    query missing its source — rather than one that answers less.
    """
    palace, ledgers = space
    (ledgers / "canonical_facts.sqlite3").unlink()
    with pytest.raises(SnapshotError, match="canonical_facts"):
        _take((palace, ledgers), tmp_path / "copy")


def test_a_missing_vector_store_refuses_the_snapshot(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    palace, ledgers = space
    (palace / "chroma.sqlite3").unlink()
    with pytest.raises(SnapshotError, match="vector store"):
        _take((palace, ledgers), tmp_path / "copy")


def test_it_refuses_to_write_over_an_existing_copy(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    """Otherwise the directory becomes half of each copy."""
    _take(space, tmp_path / "copy")
    with pytest.raises(SnapshotError, match="already exists"):
        _take(space, tmp_path / "copy")


def test_a_live_writer_does_not_block_the_snapshot(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    """The reason a snapshot does not stop the realm's runner.

    A connection is held open with an uncommitted write in flight while the copy
    is taken. The copy must succeed, and must not contain the uncommitted row —
    it is a consistent instant, not a smear.
    """
    palace, ledgers = space
    live = sqlite3.connect(palace / "chroma.sqlite3")
    try:
        live.execute("BEGIN")
        live.execute("INSERT INTO kept VALUES (99, 'uncommitted')")
        snapshot = _take((palace, ledgers), tmp_path / "copy")
    finally:
        live.rollback()
        live.close()

    assert snapshot.total_bytes > 0
    copied = tmp_path / "copy" / PALACE_PREFIX / "chroma.sqlite3"
    connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
    try:
        notes = {row[0] for row in connection.execute("SELECT note FROM kept")}
    finally:
        connection.close()
    assert "uncommitted" not in notes


def test_restore_refuses_to_cross_a_newer_hard_privacy_deletion(
    space: tuple[Path, Path], tmp_path: Path
) -> None:
    """An old backup cannot make a deleted fact recallable again."""

    palace, ledgers = space
    snapshot = _take(space, tmp_path / "copy")
    canonical = ledgers / "canonical_facts.sqlite3"
    with sqlite3.connect(canonical) as connection:
        connection.execute(
            """
            CREATE TABLE canonical_forgets (
                assertion_id TEXT PRIMARY KEY,
                hard INTEGER NOT NULL,
                forgotten_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO canonical_forgets VALUES (?, 1, ?)",
            ("fact:privacy-fence", "2099-01-01T00:00:00+00:00"),
        )
    _database(palace / "chroma.sqlite3", rows=2)

    with pytest.raises(RestoreError, match="resurrect deleted memory"):
        restore_realm_snapshot(
            source=tmp_path / "copy",
            palace_path=palace,
            ledgers_path=ledgers,
            snapshot=snapshot,
        )

    with sqlite3.connect(palace / "chroma.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM kept").fetchone()[0] == 7
