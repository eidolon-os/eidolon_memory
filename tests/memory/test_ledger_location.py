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

from pathlib import Path

import pytest

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


@pytest.mark.parametrize("filename", LEDGER_FILENAMES)
def test_every_ledger_is_named_in_one_place(filename: str, tmp_path: Path) -> None:
    """The reset inventory must cover every database opened by the router."""

    source = Path("eidolon/memory/adapters/local_palace_router.py").read_text(encoding="utf-8")

    assert f'"{filename}"' in source, (
        f"{filename} is in LEDGER_FILENAMES but the router does not open it there"
    )


def test_the_router_opens_nothing_of_ours_inside_the_palace() -> None:
    """The other direction: a ledger the router still opens at ``palace_path``."""

    source = Path("eidolon/memory/adapters/local_palace_router.py").read_text(encoding="utf-8")
    offenders = [name for name in LEDGER_FILENAMES if f'palace_path / "{name}"' in source]

    assert not offenders, f"still opened inside MemPalace's directory: {offenders}"


def test_the_inventory_counts_our_tables_and_not_mempalace_s() -> None:
    """A table name that does not exist is skipped, not reported.

    The inventory listed ``entities`` / ``triples`` / ``entity_mentions`` — MemPalace's
    names, left behind when we stopped borrowing their graph. Nothing raised; every
    graph's counts came back ``{}``, which reads as an empty graph rather than as a
    lookup against the wrong schema. Pinned against the DDL so a rename fails here.
    """

    from eidolon.memory.adapters.kg_sql import SCHEMA_STATEMENTS
    from eidolon.memory.infrastructure.palace_inventory import _SQLITE_COUNT_TABLES

    ddl = "\n".join(SCHEMA_STATEMENTS)
    for table in _SQLITE_COUNT_TABLES["knowledge_graph.sqlite3"]:
        assert f"CREATE TABLE IF NOT EXISTS {table} " in ddl, (
            f"the inventory counts {table!r}, which the graph's schema does not create"
        )
