"""The backup script, tested by looking inside the tarball.

It existed and it was wrong, for months, in a way reading it did not reveal. A
space used to be one directory; moving our seven ledgers out of MemPalace's
palace — so their ``repair``'s ``os.rename`` could not take them along — left the
snapshot archiving only the half that stayed. Every backup taken after that move
was missing the knowledge graph, the canonical facts and the commitments, and
said ``-> b64_alice_….tar.zst`` while doing it.

Nothing caught it because nothing ever opened one. So these tests open one.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "snapshot_palaces.sh"

LEDGERS = (
    "canonical_facts",
    "command_status",
    "commitments",
    "dlq",
    "extraction_decisions",
    "knowledge_graph",
    "sync_ledger",
)


def _space(root: Path, uid: str, *, with_ledgers: bool = True) -> None:
    """The on-disk shape of one space, as a running board actually has it."""

    palace = root / uid
    (palace / "9f3a-uuid").mkdir(parents=True)
    conn = sqlite3.connect(palace / "chroma.sqlite3")
    conn.execute("CREATE TABLE embeddings(id INTEGER)")
    conn.commit()
    conn.close()
    for name in ("data_level0.bin", "header.bin", "length.bin", "link_lists.bin"):
        (palace / "9f3a-uuid" / name).write_bytes(b"\0" * 64)
    (palace / "mempalace.yaml").write_text("backend: chroma\n", encoding="utf-8")

    if not with_ledgers:
        return
    ledgers = root / f"{uid}.ledgers"
    ledgers.mkdir()
    for name in LEDGERS:
        conn = sqlite3.connect(ledgers / f"{name}.sqlite3")
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()


def _run(palaces: Path, snaps: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(palaces.parent),
            "EIDOLON_MEMORY_PALACES_ROOT": str(palaces),
            "EIDOLON_MEMORY_SNAPSHOT_ROOT": str(snaps),
            "EIDOLON_MEMORY_SNAPSHOT_RETAIN": "2",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _members(archive: Path) -> set[str]:
    if archive.suffix == ".zst":
        raw = subprocess.run(["zstd", "-dc", str(archive)], capture_output=True, check=True).stdout
        decompressed = archive.with_suffix(".tar.decompressed")
        decompressed.write_bytes(raw)
        archive = decompressed
    with tarfile.open(archive) as tar:
        return {name.rstrip("/") for name in tar.getnames()}


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    palaces = tmp_path / "mempalaces"
    palaces.mkdir()
    return palaces, tmp_path / "snapshots"


def test_the_snapshot_holds_both_halves_of_a_space(tree: tuple[Path, Path]) -> None:
    """The regression. Seven of these were absent from every backup for months."""

    palaces, snaps = tree
    _space(palaces, "b64_alice")

    result = _run(palaces, snaps)
    assert result.returncode == 0, result.stderr

    archives = list(snaps.glob("b64_alice_*.tar.*"))
    assert len(archives) == 1, f"expected one archive, got {archives}"
    members = _members(archives[0])

    assert "b64_alice/chroma.sqlite3" in members
    assert "b64_alice/9f3a-uuid/data_level0.bin" in members, "the HNSW index is data too"
    for name in LEDGERS:
        assert f"b64_alice.ledgers/{name}.sqlite3" in members, (
            f"{name} is not in the backup — a restore would come back without it"
        )


def test_the_ledgers_directory_is_not_mistaken_for_a_palace(tree: tuple[Path, Path]) -> None:
    """It matches the same ``*/`` glob and has no chroma.sqlite3.

    The old script tested for one, found none, and reported "skipping" — which
    read as a tidy no-op rather than as the ledgers going unarchived.
    """

    palaces, snaps = tree
    _space(palaces, "b64_alice")

    result = _run(palaces, snaps)

    assert "b64_alice.ledgers: no chroma.sqlite3" not in result.stdout
    assert not list(snaps.glob("b64_alice.ledgers_*")), (
        "the ledgers directory was snapshotted as a space of its own"
    )


def test_checkpointing_never_creates_a_database_that_was_not_there(
    tree: tuple[Path, Path],
) -> None:
    """``sqlite3.connect`` creates. That is how an empty graph got into a backup.

    The old script checkpointed ``<palace>/knowledge_graph.sqlite3`` — a path
    that stopped existing when the ledgers moved — and so manufactured a
    zero-table database inside the palace on every run, then archived it. A
    restore would lay down a file that looks like a graph and holds nothing.
    """

    palaces, snaps = tree
    _space(palaces, "b64_alice")
    before = {p.name for p in (palaces / "b64_alice").iterdir()}

    _run(palaces, snaps)

    assert {p.name for p in (palaces / "b64_alice").iterdir()} == before
    assert not (palaces / "b64_alice" / "knowledge_graph.sqlite3").exists()


def test_a_palace_with_no_ledgers_yet_is_backed_up_and_says_so(
    tree: tuple[Path, Path],
) -> None:
    """A space initialised but never written to has no ledgers directory. That is
    not an error, but it is worth saying out loud — a silent one-directory
    snapshot is exactly what the regression looked like."""

    palaces, snaps = tree
    _space(palaces, "b64_new", with_ledgers=False)

    result = _run(palaces, snaps)

    assert result.returncode == 0, result.stderr
    assert "graph and ledgers not in this snapshot" in result.stdout
    assert _members(next(iter(snaps.glob("b64_new_*.tar.*")))) >= {"b64_new/chroma.sqlite3"}


def test_retention_prunes_instead_of_dying_on_an_unset_array(
    tree: tuple[Path, Path],
) -> None:
    """``files=(...zst ...gz)`` under ``set -u`` with no .gz ever written killed
    the script before it pruned. It had already produced the snapshot, so cron
    saw a file appear and a non-zero exit nobody read."""

    palaces, snaps = tree
    _space(palaces, "b64_alice")
    snaps.mkdir(parents=True, exist_ok=True)
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        shutil.copyfile(SCRIPT, snaps / f"b64_alice_{stamp}.tar.zst")

    result = _run(palaces, snaps)  # RETAIN=2

    assert result.returncode == 0, result.stderr
    kept = sorted(p.name for p in snaps.glob("b64_alice_*.tar.zst"))
    assert len(kept) == 2, f"retention kept {kept}"
    # The one just written is the newest, so it must be among the survivors.
    assert not kept[0].startswith("b64_alice_20260101")
