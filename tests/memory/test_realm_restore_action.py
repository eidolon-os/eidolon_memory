"""Putting a copy back, and every way it refuses to.

A snapshot nobody can restore is not a backup, which is why §9.5 of the
isolation decision sets the standard at "can be restored and was checked"
rather than "can be exported". These tests are the checking half.

The property that matters most is not that files land: it is that a refusal
leaves the realm untouched. An operator reaching for a restore is already
having a bad day, and a half-restored realm — a palace from the copy with the
ledgers it had before — is a state nothing on the Host knows how to describe.
So every check happens before the first byte is written, and the tests assert
what the realm still holds after each refusal.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from eidolon.memory.application.user_admin import (
    RestoreNotPossible,
    UserAdmin,
    UserNotFound,
    WorkerNotTerminated,
)
from eidolon.memory.config.palace_directory import LEDGER_FILENAMES

REALM = "r_owner_one"
OTHER = "r_owner_two"


class _Supervisor:
    """Only the slice a restore uses, plus a record of the pause."""

    def __init__(self, palace: Path) -> None:
        self.palace = palace
        self.paused = 0
        self.reconciles = 0
        self.worker_alive = True
        self.alive_during_pause: bool | None = None

    def palace_path_for(self, user) -> Path:
        return self.palace

    def is_worker_alive(self, user_id: str) -> bool:
        return self.worker_alive

    @asynccontextmanager
    async def realm_paused(self, user):
        self.paused += 1
        self.worker_alive = False
        try:
            yield
        finally:
            self.reconciles += 1
            self.worker_alive = True

    async def reconcile_now(self) -> None:  # pragma: no cover - unused here
        self.reconciles += 1


def _sqlite(path: Path, *, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        db.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])


def _rows(path: Path) -> int:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return int(db.execute("SELECT count(*) FROM t").fetchone()[0])


def _palace(root: Path, *, rows: int = 3, embedder: str | None = "embeddinggemma") -> Path:
    palace = root / REALM
    palace.mkdir(parents=True, exist_ok=True)
    _sqlite(palace / "chroma.sqlite3", rows=rows)
    (palace / "mempalace.yaml").write_text("backend: python\n", encoding="utf-8")
    if embedder is not None:
        (palace / "mempalace_embedder.json").write_text(
            json.dumps({"mempalace_drawers": {"model_name": embedder, "dimension": 0}}),
            encoding="utf-8",
        )
    ledgers = palace.with_name(palace.name + ".ledgers")
    for name in LEDGER_FILENAMES:
        _sqlite(ledgers / name, rows=rows)
    return palace


def _admin(monkeypatch, palace: Path, *, registered: bool = True, enabled: bool = True):
    from eidolon.memory.application import user_admin as module

    class _Entry:
        id = REALM
        port = 10031

    _Entry.enabled = enabled

    class _Config:
        def find(self, user_id: str):
            return _Entry() if registered and user_id == REALM else None

    monkeypatch.setattr(module, "load_users_config", lambda: _Config())
    supervisor = _Supervisor(palace)
    return (
        UserAdmin(
            supervisor,  # type: ignore[arg-type]
            maintenance_log_root=palace.parent / "logs",
            user_log_root=palace.parent / "logs",
        ),
        supervisor,
    )


@pytest.fixture
def run_dir(tmp_path, monkeypatch) -> Path:
    """The lock directory the realm's runner would take its lock in."""

    directory = tmp_path / "run"
    directory.mkdir()
    monkeypatch.setenv("EIDOLON_MEMORY_RUN_DIR", str(directory))
    return directory


async def _snapshot(admin, tmp_path: Path, *, name: str = "copy") -> Path:
    result = await admin.snapshot_realm(REALM, destination=tmp_path / name)
    return Path(result["destination"])


@pytest.mark.asyncio
async def test_a_realm_becomes_the_copy_it_was_given(tmp_path, monkeypatch, run_dir) -> None:
    """The round trip §9.5 asks for: copy, change, restore, and it is back."""

    palace = _palace(tmp_path / "palaces", rows=3)
    admin, supervisor = _admin(monkeypatch, palace)
    copy = await _snapshot(admin, tmp_path)

    # Life goes on in the realm, and then something goes wrong with it.
    _sqlite(palace / "chroma.sqlite3", rows=4)
    assert _rows(palace / "chroma.sqlite3") == 7

    result = await admin.restore_realm(REALM, source=copy)

    assert _rows(palace / "chroma.sqlite3") == 3
    assert _rows(palace.with_name(palace.name + ".ledgers") / "knowledge_graph.sqlite3") == 3
    # The seven ledgers plus the palace's three: vectors, config, marker.
    assert result["restored"]["file_count"] == len(LEDGER_FILENAMES) + 3
    assert supervisor.paused == 1


@pytest.mark.asyncio
async def test_the_runner_is_off_the_palace_while_it_is_replaced(
    tmp_path, monkeypatch, run_dir
) -> None:
    """Not asserted by comment: the restore takes the runner's own lock.

    One process holds a palace. A restore that wrote under a live runner would
    leave two views of the same realm, and the one that is wrong is the one
    still in memory.
    """
    import fcntl

    from eidolon.memory.infrastructure.nats.names import nats_safe_name

    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace)
    copy = await _snapshot(admin, tmp_path)

    lock_path = run_dir / f"eidolon-memory-agent-{nats_safe_name(REALM)}.lock"
    with lock_path.open("a+b") as still_running:
        fcntl.flock(still_running.fileno(), fcntl.LOCK_EX)
        with pytest.raises(WorkerNotTerminated):
            await admin.restore_realm(REALM, source=copy)


@pytest.mark.asyncio
async def test_a_snapshot_of_another_realm_is_refused(tmp_path, monkeypatch, run_dir) -> None:
    """The mistake that would write one person's memory into another's."""

    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace)
    copy = await _snapshot(admin, tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text())
    manifest["memory_space_id"] = OTHER
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RestoreNotPossible):
        await admin.restore_realm(REALM, source=copy)


@pytest.mark.asyncio
async def test_a_copy_taken_under_another_embedder_is_refused(
    tmp_path, monkeypatch, run_dir
) -> None:
    """It would restore, start, and then find nothing.

    Compared marker to marker, so the refusal does not depend on a mapping from
    a settings key being right.
    """
    palace = _palace(tmp_path / "palaces", embedder="embeddinggemma")
    admin, _sup = _admin(monkeypatch, palace)
    copy = await _snapshot(admin, tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text())
    manifest["embedder_identity"] = "bge_large_zh_v15"
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RestoreNotPossible):
        await admin.restore_realm(REALM, source=copy)

    assert _rows(palace / "chroma.sqlite3") == 3


@pytest.mark.asyncio
async def test_a_file_that_does_not_match_its_digest_stops_the_whole_restore(
    tmp_path, monkeypatch, run_dir
) -> None:
    """Checked over the whole set before writing, not as files are copied.

    A restore that stops at the sixth ledger has already replaced five, and
    nothing holds a copy of what those five were.
    """
    palace = _palace(tmp_path / "palaces", rows=3)
    admin, _sup = _admin(monkeypatch, palace)
    copy = await _snapshot(admin, tmp_path)
    _sqlite(copy / "ledgers" / "dlq.sqlite3", rows=9)

    with pytest.raises(RestoreNotPossible):
        await admin.restore_realm(REALM, source=copy)

    assert _rows(palace / "chroma.sqlite3") == 3
    assert _rows(palace.with_name(palace.name + ".ledgers") / "dlq.sqlite3") == 3


@pytest.mark.asyncio
async def test_a_directory_that_is_not_a_snapshot_is_not_treated_as_one(
    tmp_path, monkeypatch, run_dir
) -> None:
    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace)
    stray = tmp_path / "not-a-backup"
    stray.mkdir()

    with pytest.raises(RestoreNotPossible):
        await admin.restore_realm(REALM, source=stray)


@pytest.mark.asyncio
async def test_an_unknown_realm_is_not_restored_into_existence(
    tmp_path, monkeypatch, run_dir
) -> None:
    """A restore is not a way to create a realm the roster does not have."""

    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace)
    copy = await _snapshot(admin, tmp_path)
    admin_without, _ = _admin(monkeypatch, palace, registered=False)

    with pytest.raises(UserNotFound):
        await admin_without.restore_realm(REALM, source=copy)


@pytest.mark.asyncio
async def test_the_result_says_whether_the_realm_came_back(
    tmp_path, monkeypatch, run_dir
) -> None:
    """§9.4 wants the reconvergence asserted rather than assumed.

    The roster is desired state and the supervisor reconciles to it; a report of
    "restored" with a runner that never returned is the one an operator would
    act on wrongly.
    """
    palace = _palace(tmp_path / "palaces")
    admin, supervisor = _admin(monkeypatch, palace)
    copy = await _snapshot(admin, tmp_path)

    result = await admin.restore_realm(REALM, source=copy)
    assert result["worker_running"] is True

    supervisor.worker_alive = False

    @asynccontextmanager
    async def _pause_without_return(user):
        supervisor.paused += 1
        yield
        supervisor.reconciles += 1

    supervisor.realm_paused = _pause_without_return  # type: ignore[method-assign]
    copy_two = await _snapshot(admin, tmp_path, name="copy-two")

    result = await admin.restore_realm(
        REALM, source=copy_two, worker_return_timeout_s=0.2
    )
    assert result["worker_running"] is False


@pytest.mark.asyncio
async def test_a_disabled_realm_is_not_waited_on(tmp_path, monkeypatch, run_dir) -> None:
    """It has no runner to come back, and a timeout would report that as a fault."""

    palace = _palace(tmp_path / "palaces")
    admin, supervisor = _admin(monkeypatch, palace, enabled=False)
    copy = await _snapshot(admin, tmp_path)
    supervisor.worker_alive = False

    @asynccontextmanager
    async def _pause(user):
        supervisor.paused += 1
        yield

    supervisor.realm_paused = _pause  # type: ignore[method-assign]

    result = await admin.restore_realm(REALM, source=copy, worker_return_timeout_s=30.0)

    assert result["roster_enabled"] is False
    assert result["worker_running"] is False
