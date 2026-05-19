#!/usr/bin/env python3
"""Repair MemPalace chroma.sqlite3 after disk I/O or malformed database errors.

Stop MCP, Worker, and LiveKit before running.

  uv run python scripts/repair_chroma_sqlite.py
  uv run python scripts/repair_chroma_sqlite.py --palace ~/eidolon/memory/mempalace
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _backup_db(palace: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = palace / "backups" / f"chroma_pre_repair_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm"):
        src = palace / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    return dest


def _integrity(db: Path) -> str:
    conn = sqlite3.connect(str(db), timeout=10.0)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"
    finally:
        conn.close()


def _checkpoint(db: Path) -> None:
    conn = sqlite3.connect(str(db), timeout=10.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.commit()
    finally:
        conn.close()


def _vacuum(db: Path) -> None:
    conn = sqlite3.connect(str(db), timeout=60.0)
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


def _recover(db: Path) -> bool:
    """Build a fresh DB via sqlite .recover when integrity_check fails."""
    recovered = db.with_suffix(".sqlite3.recovered")
    if recovered.is_file():
        recovered.unlink()
    conn = sqlite3.connect(str(db), timeout=10.0)
    try:
        with open(recovered, "w", encoding="utf-8") as out:
            for line in conn.iterdump():
                out.write(f"{line}\n")
    except Exception as exc:
        print(f"[WARN] iterdump failed: {exc}")
        return False
    finally:
        conn.close()

    if not recovered.is_file() or recovered.stat().st_size == 0:
        return False

    new_db = db.with_suffix(".sqlite3.new")
    if new_db.is_file():
        new_db.unlink()
    rconn = sqlite3.connect(str(new_db), timeout=60.0)
    try:
        with recovered.open(encoding="utf-8") as f:
            rconn.executescript(f.read())
        rconn.commit()
    finally:
        rconn.close()

    if _integrity(new_db) != "ok":
        new_db.unlink(missing_ok=True)
        return False

    db.rename(db.with_suffix(".sqlite3.bak"))
    new_db.rename(db)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.is_file():
            side.unlink()
    recovered.unlink(missing_ok=True)
    return True


def _chroma_smoke(palace: str) -> None:
    from mempalace.palace import get_collection

    col = get_collection(palace, create=False)
    col.get(limit=5, include=["documents"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", help="Absolute palace directory (overrides --user-id).")
    parser.add_argument("--user-id", help="Resolve palace via memory settings.")
    parser.add_argument("--skip-backup", action="store_true")
    args = parser.parse_args()
    if not args.palace and not args.user_id:
        parser.error("either --palace <abs> or --user-id <id> is required")

    if args.palace:
        palace = Path(args.palace).expanduser().resolve()
    else:
        from eidolon.memory.config.memory_settings import get_memory_settings
        from eidolon.memory.config.palace_directory import resolve_palace_for_user

        palace = resolve_palace_for_user(get_memory_settings(), args.user_id)

    db = palace / "chroma.sqlite3"
    if not db.is_file():
        print(f"[ERROR] missing {db}")
        return 1

    print(f"[INFO] palace={palace}")
    if not args.skip_backup:
        backup = _backup_db(palace)
        print(f"[INFO] backup={backup}")

    print(f"[INFO] integrity (before)={_integrity(db)}")
    try:
        _checkpoint(db)
    except Exception as exc:
        print(f"[WARN] checkpoint: {exc}")

    check = _integrity(db)
    if check != "ok":
        print(f"[WARN] integrity_check={check!r}, attempting dump recover…")
        if not _recover(db):
            print("[ERROR] automatic recover failed; restore from backup/ or re-init palace")
            return 1

    try:
        _vacuum(db)
    except Exception as exc:
        print(f"[WARN] vacuum: {exc}")

    _checkpoint(db)
    print(f"[INFO] integrity (after)={_integrity(db)}")

    # D1: no global Chroma cache to evict — each agent_runner process owns its
    # PersistentClient. Ensure no agent_runner is touching this palace before
    # running smoke check.
    try:
        _chroma_smoke(str(palace))
        print("[INFO] Chroma smoke get OK")
    except Exception as exc:
        print(f"[ERROR] Chroma still failing: {exc}")
        print("  Stop the agent_runner using this palace, rerun, or restore from snapshot.")
        return 1

    print("[INFO] repair complete — restart agent_runner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
