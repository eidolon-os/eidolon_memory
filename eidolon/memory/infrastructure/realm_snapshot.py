"""Take a copy of one memory space that a restore can trust.

Every file in a space is SQLite except two config files and a couple of
markers, so this needs no mechanism the Host backup tool does not already use:
``VACUUM INTO`` produces a consistent copy of a database that is being written,
without holding the writer out of it. That is why a snapshot does not stop the
realm's runner.

What it must not do is guess. The copy is written with a manifest that names
every file, its digest, and which method produced it, and the manifest refuses
to exist if a load-bearing file is missing (see
``eidolon_memory_contracts.snapshot``). An incomplete copy that sits in a backup
directory looking valid is worse than a failure at the moment of taking it.

Restore is deliberately not here. It cannot be done under a live runner — one
process holds a palace, enforced by a lock — so it belongs with the component
that can stop one.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from eidolon_memory_contracts.snapshot import (
    LEDGERS_PREFIX,
    PALACE_PREFIX,
    RealmSnapshot,
    SnapshotEntry,
)

from eidolon.memory.config.palace_directory import LEDGER_FILENAMES
from eidolon.memory.infrastructure.palace_inventory import file_sha256

#: Files in the palace that are MemPalace's own and are not databases. They are
#: read back by MemPalace, so they travel with the copy; a plain read is safe
#: because nothing rewrites them while a space is serving.
PALACE_PLAIN_FILES = (
    "mempalace.yaml",
    "mempalace_embedder.json",
    ".collection_type_fixed",
    ".blob_seq_ids_migrated",
)

MANIFEST_NAME = "manifest.json"


class SnapshotError(RuntimeError):
    """The copy was not taken, and nothing incomplete was left behind."""


def _vacuum_into(source: Path, destination: Path) -> None:
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        # A consistent copy of a live database. The Host backup tool uses the
        # same call on the authority databases for the same reason.
        connection.execute("VACUUM INTO ?", (str(destination),))
    except sqlite3.Error as exc:
        raise SnapshotError(f"snapshot of {source.name} failed: {exc}") from exc
    finally:
        connection.close()


def write_realm_snapshot(
    *,
    palace_path: Path,
    ledgers_path: Path,
    destination: Path,
    memory_space_id: str,
    embedder_identity: str,
    embedder_dimension: int,
    owner_id: str | None = None,
) -> RealmSnapshot:
    """Copy one space into ``destination`` and return the manifest written there.

    ``destination`` must not already exist: overwriting a previous copy would
    leave a directory that is half of each.
    """

    palace_path = Path(palace_path)
    ledgers_path = Path(ledgers_path)
    destination = Path(destination)
    if destination.exists():
        raise SnapshotError(f"snapshot destination already exists: {destination}")
    if not palace_path.is_dir():
        raise SnapshotError(f"palace directory is missing: {palace_path}")

    palace_out = destination / PALACE_PREFIX
    ledgers_out = destination / LEDGERS_PREFIX
    palace_out.mkdir(mode=0o700, parents=True)
    ledgers_out.mkdir(mode=0o700, parents=True)

    entries: list[SnapshotEntry] = []

    def record(relative: str, path: Path, method: str) -> None:
        entries.append(
            SnapshotEntry(
                path=relative,
                method=method,  # type: ignore[arg-type]
                sha256=file_sha256(path),
                bytes=path.stat().st_size,
            )
        )

    chroma = palace_path / "chroma.sqlite3"
    if not chroma.is_file():
        raise SnapshotError(f"vector store is missing: {chroma}")
    _vacuum_into(chroma, palace_out / "chroma.sqlite3")
    record(
        f"{PALACE_PREFIX}/chroma.sqlite3",
        palace_out / "chroma.sqlite3",
        "sqlite-vacuum-into",
    )

    for name in PALACE_PLAIN_FILES:
        source = palace_path / name
        if not source.is_file():
            continue
        shutil.copy2(source, palace_out / name)
        record(f"{PALACE_PREFIX}/{name}", palace_out / name, "file-copy")

    for name in LEDGER_FILENAMES:
        source = ledgers_path / name
        if not source.is_file():
            # Named rather than skipped: the manifest is what decides whether
            # this copy is usable, and it cannot decide that about a file it was
            # never told about.
            raise SnapshotError(f"ledger is missing: {source}")
        _vacuum_into(source, ledgers_out / name)
        record(f"{LEDGERS_PREFIX}/{name}", ledgers_out / name, "sqlite-vacuum-into")

    snapshot = RealmSnapshot(
        memory_space_id=memory_space_id,
        owner_id=owner_id,
        taken_at=datetime.now(UTC).isoformat(),
        embedder_identity=embedder_identity,
        embedder_dimension=embedder_dimension,
        entries=tuple(entries),
    )
    (destination / MANIFEST_NAME).write_text(
        snapshot.model_dump_json(indent=2), encoding="utf-8"
    )
    return snapshot


def verify_realm_snapshot(destination: Path) -> RealmSnapshot:
    """Read a copy's manifest and check the files still match it.

    Separate from taking the copy, because the question "is this backup still
    good" is asked long after, by whoever is about to rely on it — and because a
    digest recorded but never re-checked is a digest nobody is using.
    """

    destination = Path(destination)
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SnapshotError(f"snapshot manifest is missing: {manifest_path}")
    snapshot = RealmSnapshot.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    for entry in snapshot.entries:
        path = destination / entry.path
        if not path.is_file():
            raise SnapshotError(f"snapshot file is missing: {entry.path}")
        if path.stat().st_size != entry.bytes:
            raise SnapshotError(f"snapshot file changed size: {entry.path}")
        if file_sha256(path) != entry.sha256:
            raise SnapshotError(f"snapshot file does not match its digest: {entry.path}")
    return snapshot
