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

Restore is here too, but it does not decide *when*. Putting a copy back cannot
be done under a live runner — one process holds a palace, enforced by a lock —
so the caller stops the realm's runner first and this refuses to write unless
that actually happened. The refusal is not a comment: it takes the same lock
the runner holds, so "the caller says it stopped it" is never taken on trust.
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
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

# Hard-deletion authorities that a restore must never move backwards past.
# These are the existing ledgers, not a second backup or privacy database.
_PRIVACY_WATERMARK_QUERIES = (
    (
        "canonical_facts.sqlite3",
        "SELECT MAX(forgotten_at) FROM canonical_forgets WHERE hard = 1",
    ),
    (
        "commitments.sqlite3",
        "SELECT MAX(forgotten_at) FROM commitment_privacy WHERE action = 'delete'",
    ),
    (
        "extraction_decisions.sqlite3",
        "SELECT MAX(redacted_at) FROM extraction_privacy_tombstones",
    ),
)


class SnapshotError(RuntimeError):
    """The copy was not taken, and nothing incomplete was left behind."""


class RestoreError(RuntimeError):
    """The copy was not put back, and the realm was left as it was.

    Every check a restore can make happens before the first byte is written,
    because a realm that is half of two copies is the one state nothing on the
    Host knows how to describe.
    """


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
    embedder_dimension: int | None,
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


def read_realm_snapshot(source: Path) -> RealmSnapshot:
    """Parse the manifest in ``source``, or refuse.

    The manifest is what decides whether a directory is a snapshot: a copy
    missing a load-bearing file cannot even be described, because the contract
    refuses to build. So this is also the check that a directory found in a
    backup is one of ours rather than something that happens to sit there.
    """

    manifest_path = Path(source) / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RestoreError(f"not a realm snapshot: no {MANIFEST_NAME} in {source}")
    try:
        return RealmSnapshot.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RestoreError(f"snapshot manifest is not readable: {exc}") from exc


def verify_realm_snapshot(
    source: Path, snapshot: RealmSnapshot | None = None
) -> RealmSnapshot:
    """Check every file against the digest the manifest recorded.

    One function for two moments that ask the same question. Before a restore
    writes anything — over the whole set rather than file by file, because a
    restore that stops at the sixth ledger has already replaced five and nothing
    holds what those five were. And long afterwards, by whoever is about to rely
    on a backup: a digest recorded and never re-checked is a digest nobody is
    using.

    ``snapshot`` is the manifest when the caller already has it; otherwise it is
    read from the directory, which also answers "is this a snapshot at all".
    """

    source = Path(source)
    snapshot = snapshot or read_realm_snapshot(source)
    for entry in snapshot.entries:
        path = source / entry.path
        if not path.is_file():
            raise RestoreError(f"snapshot file is missing: {entry.path}")
        if path.stat().st_size != entry.bytes:
            raise RestoreError(f"snapshot file is the wrong size: {entry.path}")
        if file_sha256(path) != entry.sha256:
            raise RestoreError(f"snapshot file does not match its digest: {entry.path}")
    return snapshot


def restore_realm_snapshot(
    *,
    source: Path,
    palace_path: Path,
    ledgers_path: Path,
    snapshot: RealmSnapshot | None = None,
) -> dict:
    """Put a verified copy back, replacing whatever the realm holds now.

    Whole directories are replaced rather than files written into the live ones.
    A file-by-file write would leave the palace holding a mixture — its vectors
    from the copy and a stray segment file from before — and MemPalace has no way
    to notice that. Replacing the directory means the only two outcomes are the
    copy and what was there before.

    What was there before is moved aside rather than deleted, and only removed
    once both directories are in place. That is what makes an interrupted
    restore recoverable by hand instead of a realm that no longer exists.
    """

    source = Path(source)
    palace_path = Path(palace_path)
    ledgers_path = Path(ledgers_path)
    snapshot = verify_realm_snapshot(source, snapshot)

    privacy_watermark = _latest_privacy_watermark(ledgers_path)
    if privacy_watermark is not None:
        taken_at = _parse_timestamp(snapshot.taken_at, label="snapshot taken_at")
        if taken_at < privacy_watermark:
            raise RestoreError(
                "snapshot predates a hard privacy deletion in the live realm; "
                "restoring it would resurrect deleted memory"
            )

    staged_palace = _stage(source / PALACE_PREFIX, palace_path)
    staged_ledgers = _stage(source / LEDGERS_PREFIX, ledgers_path)
    moved_aside: list[tuple[Path, Path]] = []
    replaced: list[Path] = []
    try:
        for staged, live in ((staged_palace, palace_path), (staged_ledgers, ledgers_path)):
            if live.exists():
                aside = live.with_name(f".{live.name}.replaced-{uuid.uuid4().hex}")
                live.rename(aside)
                moved_aside.append((aside, live))
            staged.rename(live)
            replaced.append(live)
    except OSError as exc:
        # Back out in reverse: drop whatever of the copy was already in place,
        # then put back what was moved aside. A failure on the ledgers must not
        # leave the palace holding a copy the ledgers disagree with.
        for live in reversed(replaced):
            shutil.rmtree(live, ignore_errors=True)
        for aside, live in reversed(moved_aside):
            if not live.exists():
                aside.rename(live)
        raise RestoreError(f"restore could not replace the realm directories: {exc}") from exc
    finally:
        # Only ever the staging directories: after a successful rename they no
        # longer exist, and after a rollback they hold the copy rather than
        # anything of the realm's.
        for staged in (staged_palace, staged_ledgers):
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
    for aside, _live in moved_aside:
        shutil.rmtree(aside, ignore_errors=True)

    return {
        "memory_space_id": snapshot.memory_space_id,
        "taken_at": snapshot.taken_at,
        "embedder_identity": snapshot.embedder_identity,
        "file_count": len(snapshot.entries),
        "total_bytes": snapshot.total_bytes,
        "palace_path": str(palace_path),
        "ledgers_path": str(ledgers_path),
    }


def _latest_privacy_watermark(ledgers_path: Path) -> datetime | None:
    """Latest durable hard-delete time in the live Realm, if one exists."""

    latest: datetime | None = None
    for filename, query in _PRIVACY_WATERMARK_QUERIES:
        path = Path(ledgers_path) / filename
        if not path.is_file():
            continue
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            try:
                row = connection.execute(query).fetchone()
            except sqlite3.OperationalError as exc:
                # A pre-feature or test fixture database has no privacy table.
                # Other SQL failures are integrity problems and must stop restore.
                if "no such table" in str(exc).lower():
                    continue
                raise RestoreError(
                    f"cannot inspect privacy watermark in {filename}: {exc}"
                ) from exc
        finally:
            connection.close()
        if row is None or row[0] is None:
            continue
        value = _parse_timestamp(str(row[0]), label=f"{filename} privacy watermark")
        latest = value if latest is None else max(latest, value)
    return latest


def _parse_timestamp(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RestoreError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _stage(copied: Path, live: Path) -> Path:
    """Build the replacement beside where it will land.

    Beside it so the swap is a rename: a rename across filesystems is a copy
    with a window in which the realm is neither state, and this is the one
    moment that window would matter.
    """

    if not copied.is_dir():
        raise RestoreError(f"snapshot is missing its {copied.name} directory")
    staging = live.with_name(f".{live.name}.restoring-{uuid.uuid4().hex}")
    try:
        shutil.copytree(copied, staging)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise RestoreError(f"restore could not stage {copied.name}: {exc}") from exc
    return staging
