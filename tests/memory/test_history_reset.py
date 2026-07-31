from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from eidolon_memory_contracts import memory_space_storage_name

from eidolon.memory.infrastructure.history_reset import (
    HistoryResetSafetyError,
    active_realm_ids,
    clear_palace_contents,
    clear_repair_archives,
    realm_registry_digest,
    validate_reset_scope,
)


def _registry(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memory_realms(realm_id TEXT, status TEXT)")
        connection.executemany(
            "INSERT INTO memory_realms VALUES (?, ?)",
            [("realm-a", "active"), ("realm-b", "active"), ("old", "deleted")],
        )
        connection.commit()
    finally:
        connection.close()


def test_active_realms_and_scope_are_exact(tmp_path: Path) -> None:
    registry = tmp_path / "registry.sqlite3"
    _registry(registry)
    root = tmp_path / "palaces"
    root.mkdir()
    for realm_id in ("realm-a", "realm-b"):
        (root / memory_space_storage_name(realm_id)).mkdir()

    realm_ids = active_realm_ids(registry)
    mapping = validate_reset_scope(root, realm_ids)

    assert realm_ids == ["realm-a", "realm-b"]
    assert set(mapping) == set(realm_ids)


def test_registry_digest_changes_only_when_realm_rows_change(tmp_path: Path) -> None:
    registry = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(registry)
    try:
        connection.execute(
            "CREATE TABLE memory_realms("
            "realm_id TEXT, owner_id TEXT, companion_id TEXT, status TEXT)"
        )
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute(
            "INSERT INTO memory_realms VALUES ('realm-a', 'owner', 'companion', 'active')"
        )
        connection.commit()
    finally:
        connection.close()
    before = realm_registry_digest(registry)

    connection = sqlite3.connect(registry)
    try:
        connection.execute("INSERT INTO unrelated VALUES ('changed')")
        connection.commit()
    finally:
        connection.close()

    assert realm_registry_digest(registry) == before


def test_scope_rejects_unregistered_palace(tmp_path: Path) -> None:
    root = tmp_path / "palaces"
    root.mkdir()
    (root / memory_space_storage_name("realm-a")).mkdir()
    (root / "unexpected").mkdir()

    with pytest.raises(HistoryResetSafetyError, match="unexpected"):
        validate_reset_scope(root, ["realm-a"])


def test_scope_allows_and_clear_removes_only_matching_repair_archives(tmp_path: Path) -> None:
    root = tmp_path / "palaces"
    root.mkdir()
    active = root / memory_space_storage_name("realm-a")
    active.mkdir()
    archive = root / f"{active.name}.pre-rebuild-20260716-103800"
    archive.mkdir()
    (archive / "chroma.sqlite3").write_bytes(b"old")

    mapping = validate_reset_scope(root, ["realm-a"])
    removed = clear_repair_archives(root, list(mapping.values()))

    assert removed == [str(archive)]
    assert active.is_dir()
    assert not archive.exists()


def test_clear_preserves_palace_directory_but_removes_all_contents(tmp_path: Path) -> None:
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_bytes(b"db")
    (palace / ".marker").write_text("marker")
    segment = palace / "segment"
    segment.mkdir()
    (segment / "data.bin").write_bytes(b"data")

    removed = clear_palace_contents(palace)

    assert removed == 3
    assert palace.is_dir()
    assert list(palace.iterdir()) == []
