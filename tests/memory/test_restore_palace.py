"""The other half of a backup: putting one back.

``snapshot_palaces.sh`` was written months ago and nothing had ever restored one
of its archives, which makes every claim about it a guess. The round trip is the
only test that settles it, so that is the first one here — snapshot a palace,
destroy it, restore it, and read the rows back out.

The refusals matter as much as the happy path, because both failures they cover
are silent. Restoring a palace onto a host configured for a different embedder
produces a space that raises on its first recall, hours later and far from the
restore. Swapping the directory under a live agent leaves it holding descriptors
into a directory that no longer exists.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.restore_palace import main as restore_main

SNAPSHOT = Path(__file__).resolve().parents[2] / "scripts" / "snapshot_palaces.sh"

LEDGERS = ("canonical_facts", "commitments", "knowledge_graph", "sync_ledger")


def _configured_identity() -> tuple[str, int]:
    """What this host would read a restored palace with.

    Read from settings rather than hardcoded: the check under test is "archive
    matches host", so a fixture that pins a model name tests the repo's current
    config instead of the restore. It caught me once — the fixture said
    bge_base_zh_v15 while config/settings.yaml had moved to bge-large.
    """

    from eidolon.memory.config.memory_settings import get_memory_settings

    identity = get_memory_settings().embedding.declared_identity()
    assert identity is not None, "the configured embedder has no declared identity"
    return identity.name, identity.dimension


def _palace(root: Path, uid: str, *, model: str = "", dimension: int = 0) -> None:
    """A palace with the marker MemPalace writes and the rows we will look for."""

    if not model:
        model, dimension = _configured_identity()

    palace = root / uid
    (palace / "seg-uuid").mkdir(parents=True)
    conn = sqlite3.connect(palace / "chroma.sqlite3")
    conn.execute("CREATE TABLE drawers(id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany(
        "INSERT INTO drawers(body) VALUES (?)",
        [("用户对花生过敏",), ("用户的妈妈叫张丽",), ("用户养了一只叫铁锤的鸟",)],
    )
    conn.commit()
    conn.close()
    (palace / "seg-uuid" / "data_level0.bin").write_bytes(b"\1" * 128)
    (palace / "mempalace.yaml").write_text("backend: chroma\n", encoding="utf-8")
    (palace / "mempalace_embedder.json").write_text(
        json.dumps({"default": {"model_name": model, "dimension": dimension}}),
        encoding="utf-8",
    )

    ledgers = root / f"{uid}.ledgers"
    ledgers.mkdir()
    for name in LEDGERS:
        conn = sqlite3.connect(ledgers / f"{name}.sqlite3")
        conn.execute("CREATE TABLE t(x TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (name,))
        conn.commit()
        conn.close()


def _snapshot(palaces: Path, snaps: Path) -> Path:
    result = subprocess.run(
        ["bash", str(SNAPSHOT)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(palaces.parent),
            "EIDOLON_MEMORY_PALACES_ROOT": str(palaces),
            "EIDOLON_MEMORY_SNAPSHOT_ROOT": str(snaps),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    archives = list(snaps.glob("*.tar.*"))
    assert len(archives) == 1, f"expected one archive, got {archives}"
    return archives[0]


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """A palaces root, a snapshot root, and a run dir, wired into settings."""

    palaces = tmp_path / "mempalaces"
    palaces.mkdir()
    monkeypatch.setenv("EIDOLON_MEMORY_PALACES_ROOT", str(palaces))
    monkeypatch.setenv("EIDOLON_MEMORY_RUN_DIR", str(tmp_path / "run"))
    return palaces, tmp_path / "snapshots", tmp_path / "run"


def _restore(archive: Path, palaces: Path, run: Path, *extra: str) -> int:
    return restore_main(
        [
            "--archive",
            str(archive),
            "--palaces-root",
            str(palaces),
            "--run-dir",
            str(run),
            *extra,
        ]
    )


def _bodies(palaces: Path, uid: str) -> list[str]:
    conn = sqlite3.connect(f"file:{palaces / uid / 'chroma.sqlite3'}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute("SELECT body FROM drawers ORDER BY id")]
    finally:
        conn.close()


# ── the round trip ────────────────────────────────────────────────────────────


def test_a_destroyed_palace_comes_back_with_its_memories(
    board: tuple[Path, Path, Path],
) -> None:
    """The claim the backup has been making without evidence since it was written."""

    palaces, snaps, run = board
    _palace(palaces, "b64_alice")
    archive = _snapshot(palaces, snaps)

    import shutil

    shutil.rmtree(palaces / "b64_alice")
    shutil.rmtree(palaces / "b64_alice.ledgers")

    assert _restore(archive, palaces, run) == 0

    assert _bodies(palaces, "b64_alice") == [
        "用户对花生过敏",
        "用户的妈妈叫张丽",
        "用户养了一只叫铁锤的鸟",
    ]
    for name in LEDGERS:
        path = palaces / "b64_alice.ledgers" / f"{name}.sqlite3"
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT x FROM t").fetchone()[0] == name
        finally:
            conn.close()
    assert (palaces / "b64_alice" / "seg-uuid" / "data_level0.bin").is_file()


def test_the_directory_it_replaces_is_moved_aside_not_deleted(
    board: tuple[Path, Path, Path],
) -> None:
    """A restore of the wrong archive should cost a rename, not the data."""

    palaces, snaps, run = board
    _palace(palaces, "b64_alice")
    archive = _snapshot(palaces, snaps)

    conn = sqlite3.connect(palaces / "b64_alice" / "chroma.sqlite3")
    conn.execute("INSERT INTO drawers(body) VALUES ('写在快照之后的一条')")
    conn.commit()
    conn.close()

    assert _restore(archive, palaces, run) == 0

    assert "写在快照之后的一条" not in _bodies(palaces, "b64_alice")
    superseded = list(palaces.glob("b64_alice.superseded-*"))
    assert len(superseded) == 1, "the replaced palace was not kept"
    conn = sqlite3.connect(f"file:{superseded[0] / 'chroma.sqlite3'}?mode=ro", uri=True)
    try:
        kept = [row[0] for row in conn.execute("SELECT body FROM drawers")]
    finally:
        conn.close()
    assert "写在快照之后的一条" in kept, "the write made after the snapshot is unrecoverable"


# ── the refusals ──────────────────────────────────────────────────────────────


def test_a_palace_built_by_another_embedder_is_refused(
    board: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Chroma persists the embedder's name on the collection and refuses a
    different one. Restoring anyway produces a space that raises on its first
    recall — hours later, and nowhere near the restore."""

    palaces, snaps, run = board
    _palace(palaces, "b64_alice", model="some_other_encoder", dimension=384)
    archive = _snapshot(palaces, snaps)

    import shutil

    shutil.rmtree(palaces / "b64_alice")

    assert _restore(archive, palaces, run) == 2
    assert "some_other_encoder" in capsys.readouterr().err
    assert not (palaces / "b64_alice").exists(), "a refused restore still moved files"


def test_a_live_holder_blocks_the_swap(board: tuple[Path, Path, Path]) -> None:
    """The same lock the router takes, so this cannot disagree with it about
    what "in use" means."""

    import fcntl

    from eidolon.memory.infrastructure.nats.names import nats_safe_name

    palaces, snaps, run = board
    _palace(palaces, "b64_alice")
    archive = _snapshot(palaces, snaps)

    run.mkdir(parents=True, exist_ok=True)
    lock = run / f"eidolon-memory-palace-{nats_safe_name(str(palaces / 'b64_alice'))}.lock"
    with lock.open("a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert _restore(archive, palaces, run) == 2
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    assert not list(palaces.glob("b64_alice.superseded-*")), "the swap started anyway"


def test_an_archive_for_another_space_is_refused_when_the_space_is_named(
    board: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the cross-check this puts one person's memories in another's space."""

    palaces, snaps, run = board
    _palace(palaces, "b64_alice")
    archive = _snapshot(palaces, snaps)

    assert _restore(archive, palaces, run, "--space", "default.bob.default") == 2
    assert "b64_alice" in capsys.readouterr().err


def test_a_corrupt_database_in_the_archive_is_caught_before_anything_moves(
    board: tuple[Path, Path, Path],
) -> None:
    """Otherwise a truncated archive is discovered the next time somebody asks
    the companion a question."""

    palaces, snaps, run = board
    _palace(palaces, "b64_alice")
    archive = _snapshot(palaces, snaps)

    # Rewrite the archive with a chroma.sqlite3 that is not a database.
    import shutil
    import tarfile
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "s"
        staged.mkdir()
        raw = subprocess.run(["zstd", "-dc", str(archive)], capture_output=True, check=True).stdout
        plain = Path(tmp) / "a.tar"
        plain.write_bytes(raw)
        with tarfile.open(plain) as tar:
            tar.extractall(staged, filter="data")
        (staged / "b64_alice" / "chroma.sqlite3").write_bytes(b"not a database at all")
        broken = Path(tmp) / "broken.tar"
        with tarfile.open(broken, "w") as tar:
            for member in sorted(staged.iterdir()):
                tar.add(member, arcname=member.name)
        target = snaps / "b64_alice_broken.tar"
        shutil.copyfile(broken, target)

        assert _restore(target, palaces, run) == 2

    assert not list(palaces.glob("b64_alice.superseded-*"))
    assert _bodies(palaces, "b64_alice") == [
        "用户对花生过敏",
        "用户的妈妈叫张丽",
        "用户养了一只叫铁锤的鸟",
    ], "the live palace was touched by a refused restore"


def test_dry_run_verifies_and_changes_nothing(board: tuple[Path, Path, Path]) -> None:
    palaces, snaps, run = board
    _palace(palaces, "b64_alice")
    archive = _snapshot(palaces, snaps)
    before = sorted(p.name for p in palaces.iterdir())

    assert _restore(archive, palaces, run, "--dry-run") == 0

    assert sorted(p.name for p in palaces.iterdir()) == before


if sys.platform == "win32":  # pragma: no cover - the deployment target is a Pi
    pytest.skip("fcntl locking is POSIX", allow_module_level=True)
