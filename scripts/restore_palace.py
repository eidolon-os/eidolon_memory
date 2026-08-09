#!/usr/bin/env python3
"""Put a snapshot back, or refuse and say why.

``snapshot_palaces.sh`` has existed for months and nothing has ever restored one
of its archives. A backup nobody has restored is a guess about a backup, and the
two ways this one could quietly fail are both invisible from the outside:

* **The wrong embedder.** A palace is only readable by the encoder that built it
  — Chroma persists the embedding function's name on the collection and refuses a
  differently-named one. Restoring a bge-base palace onto a host configured for
  bge-small does not corrupt anything; it produces a space that raises on first
  recall, long after the operator has moved on. The archive carries the palace's
  own ``mempalace_embedder.json``, so this is checkable before anything moves.
* **A live holder.** Swapping the directory under a running agent leaves it
  holding file descriptors into a directory that is no longer there. The palace
  lock is the existing proof that nobody holds it, and it is the same lock the
  router takes — so this cannot disagree with the router about what "in use"
  means.

Everything is staged and verified before the live directory is touched, and the
directory it replaces is moved aside rather than deleted. A restore that turns
out to be the wrong archive should cost a rename, not the data.

    uv run python scripts/restore_palace.py --archive snapshots/b64_alice_….tar.zst
    uv run python scripts/restore_palace.py --archive … --space default.alice.default
"""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eidolon_memory_contracts import memory_space_storage_name  # noqa: E402

from eidolon.memory.config.memory_settings import (  # noqa: E402
    get_memory_settings,
    resolve_run_dir,
)
from eidolon.memory.config.palace_directory import resolve_palaces_root  # noqa: E402
from eidolon.memory.infrastructure.nats.names import nats_safe_name  # noqa: E402


class RestoreRefused(Exception):
    """A refusal the operator can act on. Never a partial restore."""


# ── reading the archive ───────────────────────────────────────────────────────


def extract(archive: Path, into: Path) -> None:
    """Unpack, decompressing zstd out of band because tarfile cannot."""

    if archive.suffix == ".zst":
        raw = subprocess.run(["zstd", "-dc", str(archive)], capture_output=True, check=True).stdout
        plain = into.parent / "archive.tar"
        plain.write_bytes(raw)
        archive = plain
    with tarfile.open(archive) as tar:
        tar.extractall(into, filter="data")


def sole_palace_dir(staged: Path) -> Path:
    """The one ``<uid>/`` in the archive — not the ``<uid>.ledgers/`` beside it."""

    candidates = [d for d in staged.iterdir() if d.is_dir() and not d.name.endswith(".ledgers")]
    if len(candidates) != 1:
        raise RestoreRefused(
            f"expected exactly one palace directory in the archive, found "
            f"{sorted(d.name for d in candidates)}"
        )
    return candidates[0]


def archived_embedder(palace: Path) -> tuple[str, int]:
    """The name and width the archived collection was created with.

    MemPalace writes this beside the palace and it travels inside the archive,
    which is what makes the check possible without a separate manifest to keep
    in sync.
    """

    marker = palace / "mempalace_embedder.json"
    if not marker.is_file():
        raise RestoreRefused(
            f"{marker.name} is not in the archive, so there is no way to tell "
            f"which encoder built this palace. Restoring blind risks a space "
            f"that raises on first recall."
        )
    doc = json.loads(marker.read_text(encoding="utf-8"))
    names = {
        str(section.get("model_name") or "")
        for section in doc.values()
        if isinstance(section, dict)
    }
    dims = {
        int(section.get("dimension") or 0) for section in doc.values() if isinstance(section, dict)
    }
    if len(names) != 1:
        raise RestoreRefused(f"{marker.name} names {sorted(names)}; expected exactly one")
    return names.pop(), (dims.pop() if len(dims) == 1 else 0)


# ── the two refusals ──────────────────────────────────────────────────────────


def refuse_on_embedder_mismatch(archived: tuple[str, int], settings) -> None:
    identity = settings.embedding.declared_identity()
    if identity is None:
        raise RestoreRefused(
            "the configured embedder has no declared identity, so this host "
            "cannot say what it would read the restored palace with"
        )
    name, dimension = archived
    if name and name != identity.name:
        raise RestoreRefused(
            f"the archive was built with embedder {name!r} and this host is "
            f"configured for {identity.name!r}. Chroma persists the name on the "
            f"collection and refuses a different one, so this would restore a "
            f"space that raises on its first recall rather than one that is "
            f"merely worse. Align embedding.* with {name!r} and rerun."
        )
    if dimension and dimension != identity.dimension:
        raise RestoreRefused(
            f"the archive is {dimension}-dimensional and this host is configured "
            f"for {identity.dimension}. The collection's width is fixed at "
            f"creation and cannot be reinterpreted."
        )


def verify_databases(staged: Path) -> list[str]:
    """Every SQLite file in the archive must open and pass integrity_check.

    Cheap, and it is the difference between discovering a truncated archive now
    and discovering it the next time somebody asks the companion a question.
    """

    checked = []
    for path in sorted(staged.rglob("*.sqlite3")):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise RestoreRefused(f"{path.name} is not a readable database: {error}") from error
        finally:
            conn.close()
        if verdict != "ok":
            raise RestoreRefused(f"{path.name} fails integrity_check: {verdict}")
        checked.append(path.name)
    if not checked:
        raise RestoreRefused("the archive holds no databases at all")
    return checked


# ── moving it into place ──────────────────────────────────────────────────────


def swap_in(staged_dir: Path, live_dir: Path, stamp: str) -> Path | None:
    """Move ``live_dir`` aside and put ``staged_dir`` in its place.

    Returns where the old directory went, or None if there was nothing there.
    Renames rather than copies, so the window in which neither exists is one
    syscall wide, and the previous contents survive a wrong restore.
    """

    superseded = None
    if live_dir.exists():
        superseded = live_dir.with_name(f"{live_dir.name}.superseded-{stamp}")
        live_dir.rename(superseded)
    live_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged_dir), str(live_dir))
    return superseded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument(
        "--space",
        default="",
        help="cross-check: refuse if the archive is not this space's palace",
    )
    parser.add_argument("--palaces-root", type=Path, help="default: from settings")
    parser.add_argument("--run-dir", type=Path, help="where the palace locks live")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify and report, then change nothing",
    )
    args = parser.parse_args(argv)

    settings = get_memory_settings()
    palaces_root = (
        args.palaces_root.expanduser().resolve()
        if args.palaces_root
        else resolve_palaces_root(settings)
    )
    run_dir = (
        args.run_dir.expanduser().resolve() if args.run_dir else Path(resolve_run_dir(settings))
    )

    with tempfile.TemporaryDirectory(prefix="eidolon-restore-") as tmp:
        staged = Path(tmp) / "staged"
        staged.mkdir()
        try:
            extract(args.archive, staged)
            palace = sole_palace_dir(staged)
            uid = palace.name

            if args.space and memory_space_storage_name(args.space) != uid:
                raise RestoreRefused(
                    f"--space {args.space} encodes to {memory_space_storage_name(args.space)} "
                    f"but the archive holds {uid}. Restoring it would put one "
                    f"person's memories in another's space."
                )

            archived = archived_embedder(palace)
            refuse_on_embedder_mismatch(archived, settings)
            checked = verify_databases(staged)

            live_palace = palaces_root / uid
            live_ledgers = palaces_root / f"{uid}.ledgers"
            staged_ledgers = staged / f"{uid}.ledgers"

            print(f"archive   {args.archive}")
            print(f"space     {uid}")
            print(f"embedder  {archived[0]} ({archived[1]}d)")
            print(f"verified  {len(checked)} database(s): {', '.join(sorted(checked))}")
            print(f"target    {live_palace}")
            if not staged_ledgers.is_dir():
                print("[warn]    the archive has no .ledgers — graph and ledgers will be absent")

            if args.dry_run:
                print("\ndry run: nothing was moved")
                return 0

            # The same lock the router takes on the palace directory. Held for
            # the whole swap, so a supervisor cannot start an agent into a
            # half-replaced directory.
            run_dir.mkdir(parents=True, exist_ok=True)
            lock_path = run_dir / f"eidolon-memory-palace-{nats_safe_name(str(live_palace))}.lock"
            with lock_path.open("a+b") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RestoreRefused(
                        f"another process holds {live_palace}. Stop the agent for "
                        f"this space first — swapping the directory underneath it "
                        f"leaves it reading a directory that no longer exists."
                    ) from error
                try:
                    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                    old_palace = swap_in(palace, live_palace, stamp)
                    old_ledgers = (
                        swap_in(staged_ledgers, live_ledgers, stamp)
                        if staged_ledgers.is_dir()
                        else None
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

            print(f"\nrestored  {live_palace}")
            for previous in (old_palace, old_ledgers):
                if previous:
                    print(f"previous  {previous}  (delete once the restore is confirmed)")
            return 0

        except RestoreRefused as error:
            print(f"[refused] {error}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
