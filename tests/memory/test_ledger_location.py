"""Our databases do not live in a directory MemPalace renames.

``mempalace repair --mode from-sqlite --archive-existing`` — which our supervisor
runs to change the embedder — does ``os.rename`` on the whole palace directory
(``repair.py``, ``rebuild_from_sqlite``) and rebuilds a fresh one in its place. It
then copies back exactly one filename: ``knowledge_graph.sqlite3`` and its
``-wal``/``-shm``, in ``_preserve_knowledge_graph_sqlite``, which they added for
their issue #1816.

With our seven databases inside that directory, a repair silently dropped six of
them. Two are product behaviour rather than bookkeeping: canonical facts hold the
invalidation chain that stops a corrected fact being recalled, and commitments are
what commitment queries are answered from. The graph survived only because it is
named what their hardcoded string expects — preserved by a coincidence with a
third-party constant rather than by any contract, and one they could rename.

The supervisor reports ``kg_preserved`` as ``repair_returncode == 0``, which is not
a check of anything. Rather than teach it to restore what was lost — patching one
operation, leaving the next one to find — our state moved to a sibling directory
the rename cannot reach. The supervisor is off limits and needs no change: what it
reports becomes true because the files are somewhere else.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eidolon.memory.adapters.local_palace_router import _adopt_ledgers_beside_the_palace
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.palace_directory import (
    LEDGER_FILENAMES,
    resolve_ledgers_for_memory_space,
    resolve_palace_for_memory_space,
)

SPACE = "default.alice.default"


def _settings(root: Path) -> MemorySettings:
    return MemorySettings.model_validate({"runtime": {"palaces_root": str(root)}})


def test_our_directory_is_not_inside_theirs(tmp_path: Path) -> None:
    """The whole guarantee, in one assertion.

    A subdirectory of the palace would be renamed along with it; a sibling is not
    reachable from the path MemPalace was given.
    """

    settings = _settings(tmp_path)
    palace = resolve_palace_for_memory_space(settings, SPACE)
    ledgers = resolve_ledgers_for_memory_space(settings, SPACE)

    assert palace.parent == ledgers.parent
    assert not ledgers.is_relative_to(palace)
    assert ledgers.name.startswith(palace.name)


def test_a_palace_rename_leaves_our_databases_untouched(tmp_path: Path) -> None:
    """What ``repair --archive-existing`` actually does, done to a real layout.

    ``os.rename`` on the palace, then a fresh directory in its place — the same two
    steps, without needing MemPalace installed to run them.
    """

    settings = _settings(tmp_path)
    palace = resolve_palace_for_memory_space(settings, SPACE)
    ledgers = resolve_ledgers_for_memory_space(settings, SPACE)
    palace.mkdir(parents=True)
    ledgers.mkdir(parents=True)
    (palace / "chroma.sqlite3").write_text("theirs", encoding="utf-8")
    for filename in LEDGER_FILENAMES:
        (ledgers / filename).write_text(filename, encoding="utf-8")

    # The repair: archive the palace aside, rebuild an empty one.
    palace.rename(palace.with_name(palace.name + ".pre-rebuild-20260806"))
    palace.mkdir()
    (palace / "chroma.sqlite3").write_text("rebuilt", encoding="utf-8")

    survivors = sorted(p.name for p in ledgers.iterdir())

    assert survivors == sorted(LEDGER_FILENAMES)
    for filename in LEDGER_FILENAMES:
        assert (ledgers / filename).read_text(encoding="utf-8") == filename


def test_an_existing_palace_migrates_on_open(tmp_path: Path) -> None:
    """Deployments already have these files in the old place.

    Done on resolve rather than as a step someone has to run, because a migration
    that has to be remembered is one that gets skipped on the machine that matters.
    """

    palace = tmp_path / "b64_space"
    ledgers = tmp_path / "b64_space.ledgers"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_text("theirs", encoding="utf-8")
    for filename in LEDGER_FILENAMES:
        (palace / filename).write_text(filename, encoding="utf-8")
    # A live write-ahead log alongside its database.
    (palace / "knowledge_graph.sqlite3-wal").write_text("wal", encoding="utf-8")

    _adopt_ledgers_beside_the_palace(palace, ledgers)

    assert sorted(p.name for p in palace.iterdir()) == ["chroma.sqlite3"]
    assert (ledgers / "commitments.sqlite3").read_text(encoding="utf-8") == (
        "commitments.sqlite3"
    )
    # The sidecar moved with its database. Left behind, it would silently discard
    # everything committed to the log but not yet checkpointed — for the graph,
    # the most recent turns.
    assert (ledgers / "knowledge_graph.sqlite3-wal").is_file()


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    """It runs on every open, so it has to be a no-op after the first."""

    palace = tmp_path / "b64_space"
    ledgers = tmp_path / "b64_space.ledgers"
    palace.mkdir()
    for filename in LEDGER_FILENAMES:
        (palace / filename).write_text("original", encoding="utf-8")

    _adopt_ledgers_beside_the_palace(palace, ledgers)
    _adopt_ledgers_beside_the_palace(palace, ledgers)

    assert sorted(p.name for p in ledgers.iterdir()) == sorted(LEDGER_FILENAMES)


def test_a_stale_copy_left_behind_never_overwrites_the_live_one(tmp_path: Path) -> None:
    """The case that would lose data if this used ``os.replace``.

    Two files with the same name means the destination is the live one — the space
    has been served from the new layout already — and the copy in the palace is
    what a MemPalace rebuild restored from an archive. Overwriting would discard
    everything written since.
    """

    palace = tmp_path / "b64_space"
    ledgers = tmp_path / "b64_space.ledgers"
    palace.mkdir()
    ledgers.mkdir()
    (palace / "commitments.sqlite3").write_text("stale, restored from an archive", "utf-8")
    (ledgers / "commitments.sqlite3").write_text("live", encoding="utf-8")

    _adopt_ledgers_beside_the_palace(palace, ledgers)

    assert (ledgers / "commitments.sqlite3").read_text(encoding="utf-8") == "live"
    assert (palace / "commitments.sqlite3").is_file(), "the stale copy is left for an operator"


@pytest.mark.parametrize("filename", LEDGER_FILENAMES)
def test_every_ledger_is_named_in_one_place(filename: str, tmp_path: Path) -> None:
    """The migration works off ``LEDGER_FILENAMES``. A seventh ledger added to the
    router and not to that tuple would keep being written inside the palace, and be
    lost by the next repair — silently, since nothing else reads the list."""

    source = Path("eidolon/memory/adapters/local_palace_router.py").read_text(encoding="utf-8")

    assert f'"{filename}"' in source, (
        f"{filename} is in LEDGER_FILENAMES but the router does not open it there"
    )


def test_the_router_opens_nothing_of_ours_inside_the_palace() -> None:
    """The other direction: a ledger the router still opens at ``palace_path``."""

    source = Path("eidolon/memory/adapters/local_palace_router.py").read_text(encoding="utf-8")
    offenders = [
        name for name in LEDGER_FILENAMES if f'palace_path / "{name}"' in source
    ]

    assert not offenders, f"still opened inside MemPalace's directory: {offenders}"


def test_a_real_sqlite_file_survives_the_move(tmp_path: Path) -> None:
    """Not just the bytes — the database is still openable afterwards."""

    palace = tmp_path / "b64_space"
    ledgers = tmp_path / "b64_space.ledgers"
    palace.mkdir()
    connection = sqlite3.connect(palace / "commitments.sqlite3")
    connection.execute("CREATE TABLE t (x TEXT)")
    connection.execute("INSERT INTO t VALUES ('kept')")
    connection.commit()
    connection.close()

    _adopt_ledgers_beside_the_palace(palace, ledgers)

    moved = sqlite3.connect(ledgers / "commitments.sqlite3")
    try:
        assert moved.execute("SELECT x FROM t").fetchone()[0] == "kept"
    finally:
        moved.close()
