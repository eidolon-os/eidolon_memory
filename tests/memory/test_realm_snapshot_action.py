"""Taking a copy of one realm, and the copies it refuses to take.

The operator backup tool lists what it cannot snapshot rather than including a
copy that restores into something subtly wrong — memory was the first entry on
that list. This action is the declaration that takes it off, so the thing worth
testing is not that files appear: it is that a copy is either trustworthy or
refused, and that taking one does not cost the person their Eidolon's attention.

Three properties, each with a way it could quietly go wrong:

- **The realm keeps serving.** Every file is SQLite and ``VACUUM INTO`` copies a
  live database consistently, so nothing reconciles, stops a child, or takes the
  lock. A backup that needed downtime is a backup nobody takes.
- **The embedder identity is the palace's, not the configuration's.** Vectors
  mean nothing under a different embedder; configuration records intent that may
  have moved on, and stamping the intent as fact makes a restore that succeeds
  and then stops finding things.
- **A snapshot that could not be restored is not written.** No marker means no
  identity means no safe restore, and a refusal beats a directory that looks
  like a backup.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eidolon.memory.application.user_admin import (
    SnapshotNotPossible,
    UserAdmin,
    UserNotFound,
)
from eidolon.memory.config.palace_directory import LEDGER_FILENAMES

REALM = "r_owner_one"


class _Supervisor:
    """Only what the action uses: where this realm's palace is."""

    def __init__(self, palace: Path) -> None:
        self.palace = palace
        self.reconciles = 0
        self.stops: list[str] = []

    def palace_path_for(self, user) -> Path:
        return self.palace

    async def reconcile_now(self) -> None:
        self.reconciles += 1

    def request_reload(self) -> None:  # pragma: no cover - guarded against below
        self.stops.append("reload")


def _sqlite(path: Path, *, rows: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        db.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])


def _palace(root: Path, *, embedder: str | None = "embeddinggemma") -> Path:
    palace = root / REALM
    palace.mkdir(parents=True)
    _sqlite(palace / "chroma.sqlite3", rows=3)
    (palace / "mempalace.yaml").write_text("backend: python\n", encoding="utf-8")
    (palace / ".collection_type_fixed").write_text("", encoding="utf-8")
    if embedder is not None:
        (palace / "mempalace_embedder.json").write_text(
            json.dumps({"mempalace_drawers": {"model_name": embedder, "dimension": 768}}),
            encoding="utf-8",
        )
    ledgers = palace.with_name(palace.name + ".ledgers")
    for name in LEDGER_FILENAMES:
        _sqlite(ledgers / name)
    return palace


def _admin(monkeypatch, palace: Path, *, registered: bool = True) -> tuple[UserAdmin, _Supervisor]:
    from eidolon.memory.application import user_admin as module

    class _Entry:
        id = REALM
        port = 10031
        enabled = True

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


@pytest.mark.asyncio
async def test_a_snapshot_holds_every_file_of_the_space(tmp_path, monkeypatch) -> None:
    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace)

    result = await admin.snapshot_realm(REALM, destination=tmp_path / "out")

    manifest = result["manifest"]
    paths = {entry["path"] for entry in manifest["entries"]}
    assert "palace/chroma.sqlite3" in paths
    assert {f"ledgers/{name}" for name in LEDGER_FILENAMES} <= paths
    # Config and markers travel too: a restore without them is a palace whose
    # own tooling cannot open it.
    assert "palace/mempalace.yaml" in paths
    for entry in manifest["entries"]:
        assert Path(result["destination"], entry["path"]).is_file()


@pytest.mark.asyncio
async def test_taking_a_copy_does_not_interrupt_the_realm(tmp_path, monkeypatch) -> None:
    """No reconcile, no stop, no lock.

    ``VACUUM INTO`` copies a live SQLite database consistently. A backup that
    required downtime would be one nobody takes, and this is the property that
    makes it free.
    """
    palace = _palace(tmp_path / "palaces")
    admin, supervisor = _admin(monkeypatch, palace)

    await admin.snapshot_realm(REALM, destination=tmp_path / "out")

    assert supervisor.reconciles == 0
    assert supervisor.stops == []


@pytest.mark.asyncio
async def test_the_manifest_records_the_embedder_the_palace_was_built_with(
    tmp_path, monkeypatch
) -> None:
    """Not the configured one.

    A restore under a different embedder succeeds and then quietly stops
    finding things, which is why the vector store refuses it — and it can only
    refuse if the snapshot said what it was taken under.
    """
    palace = _palace(tmp_path / "palaces", embedder="embeddinggemma")
    admin, _sup = _admin(monkeypatch, palace)

    result = await admin.snapshot_realm(REALM, destination=tmp_path / "out")

    assert result["manifest"]["embedder_identity"] == "embeddinggemma"


@pytest.mark.asyncio
async def test_a_palace_with_no_recorded_identity_is_refused(
    tmp_path, monkeypatch
) -> None:
    """And nothing is written.

    A directory that looks like a backup but cannot be restored is worse than
    the operator tool's current honesty about not covering memory at all.
    """
    palace = _palace(tmp_path / "palaces", embedder=None)
    admin, _sup = _admin(monkeypatch, palace)

    with pytest.raises(SnapshotNotPossible):
        await admin.snapshot_realm(REALM, destination=tmp_path / "out")

    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_an_unknown_realm_is_not_snapshotted_into_existence(
    tmp_path, monkeypatch
) -> None:
    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace, registered=False)

    with pytest.raises(UserNotFound):
        await admin.snapshot_realm(REALM, destination=tmp_path / "out")


@pytest.mark.asyncio
async def test_an_existing_destination_is_refused_rather_than_merged(
    tmp_path, monkeypatch
) -> None:
    """Overwriting would leave a directory that is half of each copy."""
    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace)
    (tmp_path / "out").mkdir()

    with pytest.raises(SnapshotNotPossible):
        await admin.snapshot_realm(REALM, destination=tmp_path / "out")


@pytest.mark.asyncio
async def test_the_copy_is_readable_as_sqlite_rather_than_a_byte_copy(
    tmp_path, monkeypatch
) -> None:
    """A live database copied byte-wise can be mid-transaction.

    Opening the copy and reading it is the cheap proof that ``VACUUM INTO`` did
    what a plain ``cp`` cannot promise.
    """
    palace = _palace(tmp_path / "palaces")
    admin, _sup = _admin(monkeypatch, palace)

    result = await admin.snapshot_realm(REALM, destination=tmp_path / "out")

    copied = Path(result["destination"]) / "palace/chroma.sqlite3"
    with sqlite3.connect(copied) as db:
        assert db.execute("SELECT count(*) FROM t").fetchone()[0] == 3
