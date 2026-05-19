"""SQLite integrity checks + deployment location guard (D1 守门 D2 / D4)."""

from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


class IntegrityCheckFailed(RuntimeError):
    """Raised when ``PRAGMA integrity_check`` returns non-ok rows."""


class PalaceLocationError(RuntimeError):
    """Raised when the palace directory is on an unsupported / unsafe filesystem."""


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    detail: str  # first non-ok line, or "ok"
    pragma: str  # "integrity_check" | "quick_check"


def run_integrity_check(
    chroma_sqlite: str,
    *,
    quick: bool = False,
    timeout_seconds: float = 30.0,
) -> IntegrityReport:
    """Run ``PRAGMA integrity_check`` (or ``quick_check``).

    Returns ``IntegrityReport(ok=True, detail="ok")`` on success.
    Empty / missing file → ok=False with detail="missing".
    """
    pragma_name = "quick_check" if quick else "integrity_check"
    p = Path(chroma_sqlite)
    if not p.is_file():
        return IntegrityReport(ok=False, detail="missing", pragma=pragma_name)

    conn = sqlite3.connect(str(p), timeout=timeout_seconds)
    try:
        rows = conn.execute(f"PRAGMA {pragma_name}").fetchall()
    finally:
        conn.close()

    if not rows:
        return IntegrityReport(ok=False, detail="empty-result", pragma=pragma_name)

    first = str(rows[0][0]) if rows[0] else ""
    if first.strip().lower() == "ok":
        return IntegrityReport(ok=True, detail="ok", pragma=pragma_name)

    # Pack a short summary of the worst offenders
    detail = " | ".join(str(r[0]) for r in rows[:5])
    return IntegrityReport(ok=False, detail=detail, pragma=pragma_name)


# -------------------- D4 deployment-location guard --------------------

_FORBIDDEN_PATH_SEGMENTS = (
    "Mobile Documents",   # iCloud Drive containers
    "iCloud Drive",
    "Dropbox",
    "OneDrive",
    "Google Drive",
    "GoogleDrive",
    "/Volumes/",          # macOS network / DMG mounts
)

# Filesystem types known to break SQLite multi-process locking
_UNSAFE_FSTYPES = ("nfs", "smbfs", "smb", "cifs", "afpfs", "fuse.osxfuse")


def assert_palace_location_safe(palace_path: str | Path) -> None:
    """Raise :class:`PalaceLocationError` if palace is on a banned location.

    Banned: iCloud / Dropbox / OneDrive / Google Drive / Time Machine, plus any
    NFS / SMB / AFP / FUSE mount. The check is best-effort — we don't fail open.
    """
    p = Path(palace_path).expanduser().resolve()
    abs_str = str(p)

    for marker in _FORBIDDEN_PATH_SEGMENTS:
        if marker.lower() in abs_str.lower():
            msg = (
                f"palace path {abs_str!r} crosses '{marker}' which is unsafe "
                "(cloud sync / external mount can corrupt chroma SQLite)"
            )
            raise PalaceLocationError(msg)

    # macOS: check for .icloud sentinel files in parent (iCloud-evicted)
    if platform.system() == "Darwin":
        try:
            parent = p.parent if p.exists() else p
            for entry in parent.iterdir():
                if entry.name.endswith(".icloud"):
                    msg = (
                        f"palace path {abs_str!r}: parent {parent} contains "
                        ".icloud placeholders — iCloud is managing this directory"
                    )
                    raise PalaceLocationError(msg)
        except (PermissionError, FileNotFoundError):
            pass

    # Posix: filesystem type via `mount` (best-effort)
    _check_fstype_via_mount(abs_str)


def _check_fstype_via_mount(abs_str: str) -> None:
    """Best-effort: parse ``mount`` output to flag NFS/SMB/AFP/FUSE mounts."""
    try:
        out = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return

    # Each line typically: "/dev/disk1s2 on /Users (apfs, local, journaled, …)"
    best_match: tuple[int, str, str] | None = None  # (mount_depth, mount_point, fstype)
    for line in out.splitlines():
        if " on " not in line:
            continue
        try:
            _src, rest = line.split(" on ", 1)
            mount_point, paren = rest.split(" ", 1)
        except ValueError:
            continue
        if not abs_str.startswith(mount_point.rstrip("/") + ("/" if mount_point != "/" else "")):
            if abs_str != mount_point:
                continue
        # parse fstype between ( and ,)
        paren = paren.strip().lstrip("(").rstrip(")")
        fstype = paren.split(",", 1)[0].strip()
        depth = len(mount_point)
        if best_match is None or depth > best_match[0]:
            best_match = (depth, mount_point, fstype)

    if best_match is None:
        return
    _depth, mp, fstype = best_match
    if fstype.lower() in _UNSAFE_FSTYPES:
        msg = (
            f"palace path {abs_str!r} is on mount {mp!r} with filesystem "
            f"{fstype!r}, which breaks SQLite locking"
        )
        raise PalaceLocationError(msg)


# -------------------- D3 fsync helper --------------------


def fsync_directory(path: str | Path) -> None:
    """``os.fsync`` a directory file descriptor (no-op on Windows).

    macOS APFS does not fsync directory entries when their contents fsync; calling
    this after ``palace_generation``-style metadata writes ensures rename / new
    files survive a sudden poweroff.
    """
    p = Path(path)
    try:
        fd = os.open(str(p), os.O_RDONLY)
    except (OSError, NotImplementedError):
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
