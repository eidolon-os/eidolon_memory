"""Resolve per-memory-space MemPalace palace directories on disk (D1).

Each memory space gets ``<palaces_root>/<storage_name>/``; the agent_runner
process owns exclusive PersistentClient access to that directory.

Resolution order for :func:`resolve_palace_for_user`:

1. ``path_override`` (caller-supplied absolute path)
2. ``settings.runtime.palaces_root`` joined with encoded ``memory_space_id``

``palaces_root`` priority: ``EIDOLON_MEMORY_PALACES_ROOT`` env > config field
> ``~/eidolon/memory/mempalaces`` fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

# Single source of truth for the memory_space_id grammar lives in the SDK; the
# path-injection guarantee here relies on that same validation. Re-exported so
# existing callers keep importing it from this module.
from eidolon_memory_contracts import memory_space_storage_name, validate_memory_space_id

from eidolon.memory.config.memory_settings import MemorySettings

__all__ = [
    "validate_memory_space_id",
    "resolve_palaces_root",
    "resolve_palace_for_memory_space",
    "memory_space_storage_name",
]


def resolve_palaces_root(settings: MemorySettings) -> Path:
    """Env > config > ``~/eidolon/memory/mempalaces`` default."""
    env = os.environ.get("EIDOLON_MEMORY_PALACES_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.palaces_root or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "eidolon" / "memory" / "mempalaces").resolve()


def resolve_palace_for_memory_space(
    settings: MemorySettings,
    memory_space_id: str,
    *,
    path_override: str | Path | None = None,
) -> Path:
    """Return per-memory-space palace directory; does not create it.

    This directory is **MemPalace's**. It holds ``chroma.sqlite3``, their segment
    directories and their embedder marker, and they treat it as theirs to move:
    ``repair --archive-existing`` does ``os.rename`` on the whole thing. Our own
    state goes in the sibling returned by :func:`resolve_ledgers_for_memory_space`.
    """
    if path_override:
        return Path(path_override).expanduser().resolve()
    mid = validate_memory_space_id(memory_space_id)
    return resolve_palaces_root(settings) / memory_space_storage_name(mid)


#: Appended to the palace directory's name to get ours. A sibling rather than a
#: subdirectory, because the palace path is what MemPalace's CLI is handed and what
#: every existing deployment already has on disk — nesting it would mean migrating
#: their files too, for no additional guarantee.
LEDGERS_DIR_SUFFIX = ".ledgers"

#: Everything in a space that is ours rather than MemPalace's: the six append-only
#: ledgers and the knowledge graph.
LEDGER_FILENAMES = (
    "knowledge_graph.sqlite3",
    "command_status.sqlite3",
    "dlq.sqlite3",
    "extraction_decisions.sqlite3",
    "canonical_facts.sqlite3",
    "commitments.sqlite3",
    "sync_ledger.sqlite3",
)


def resolve_ledgers_for_memory_space(
    settings: MemorySettings,
    memory_space_id: str,
    *,
    path_override: str | Path | None = None,
) -> Path:
    """Where this space's Eidolon-owned databases live. Does not create it.

    A sibling of the palace, not a directory inside it, and that is the whole
    point. ``mempalace repair --mode from-sqlite --archive-existing`` — which our
    supervisor runs to change embedder — renames the palace directory aside and
    rebuilds a fresh one, then copies back exactly one filename:
    ``knowledge_graph.sqlite3`` and its ``-wal``/``-shm`` (their
    ``_preserve_knowledge_graph_sqlite``, added for their issue #1816).

    So with our files inside the palace, a repair silently dropped all six
    ledgers. Two of them are product behaviour rather than bookkeeping: canonical
    facts hold the invalidation chain that stops a corrected fact being recalled,
    and commitments are what commitment queries are answered from. The graph
    survived only by being named what their hardcoded string happens to expect —
    preserved by a coincidence with a third-party constant, not by a contract.

    The alternative was to teach our supervisor to copy the ledgers back. That
    patches one operation; this removes the whole class, because nothing MemPalace
    does to its own directory can reach a path it was never given.
    """

    palace = resolve_palace_for_memory_space(
        settings, memory_space_id, path_override=path_override
    )
    return palace.with_name(palace.name + LEDGERS_DIR_SUFFIX)
