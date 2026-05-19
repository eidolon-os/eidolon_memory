#!/usr/bin/env python3
"""Dual-process SQLite read/write stress test for chroma.sqlite3 (Phase 0)."""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sqlite3
import sys
import time
from pathlib import Path


def _reader(db_path: str, duration: float, ready: mp.Queue, errors: mp.Queue) -> None:
    ready.put("ok")
    end = time.monotonic() + duration
    n = 0
    while time.monotonic() < end:
        try:
            conn = sqlite3.connect(db_path, timeout=2.0)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            conn.close()
            n += 1
        except sqlite3.OperationalError as exc:
            errors.put(str(exc))
        except Exception as exc:
            errors.put(repr(exc))
        time.sleep(0.1)
    errors.put(f"reads={n}")


def _writer(db_path: str, duration: float, ready: mp.Queue, errors: mp.Queue) -> None:
    ready.put("ok")
    end = time.monotonic() + duration
    n = 0
    while time.monotonic() < end:
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("SELECT 1")
            conn.commit()
            conn.close()
            n += 1
        except sqlite3.OperationalError as exc:
            errors.put(str(exc))
        except Exception as exc:
            errors.put(repr(exc))
        time.sleep(1.0)
    errors.put(f"writes={n}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", required=True, help="Absolute palace path")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--out", default="reports/poc_sqlite_rw.json")
    args = parser.parse_args()

    from eidolon.memory.infrastructure.chroma_refresh import ensure_sqlite_wal

    palace = Path(args.palace).expanduser().resolve()
    db = palace / "chroma.sqlite3"
    if not db.is_file():
        print(f"missing {db}", file=sys.stderr)
        return 1

    wal = ensure_sqlite_wal(str(db))
    ctx = mp.get_context("spawn")
    ready_r: mp.Queue = ctx.Queue()
    ready_w: mp.Queue = ctx.Queue()
    errors: mp.Queue = ctx.Queue()

    pr = ctx.Process(target=_reader, args=(str(db), args.duration, ready_r, errors))
    pw = ctx.Process(target=_writer, args=(str(db), args.duration, ready_w, errors))
    pr.start()
    pw.start()
    ready_r.get(timeout=30)
    ready_w.get(timeout=30)
    pr.join()
    pw.join()

    locked = 0
    msgs: list[str] = []
    while not errors.empty():
        m = errors.get_nowait()
        msgs.append(m)
        if "locked" in m.lower():
            locked += 1

    report = {
        "palace": str(palace),
        "db": str(db),
        "duration_s": args.duration,
        "wal": wal,
        "database_locked_count": locked,
        "messages": msgs[-20:],
        "pass": locked == 0,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
