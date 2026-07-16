from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

from eidolon.memory.infrastructure.palace_inventory import (
    build_palaces_inventory,
    memory_space_id_from_storage_name,
)


def _storage_name(memory_space_id: str) -> str:
    token = base64.urlsafe_b64encode(memory_space_id.encode()).decode().rstrip("=")
    return f"b64_{token}"


def _create_sqlite(path: Path, ddl: str, rows: list[str]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(ddl)
        for sql in rows:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()


def test_storage_name_decoder_is_strict() -> None:
    storage_name = _storage_name("r_c_owner_example")

    assert memory_space_id_from_storage_name(storage_name) == "r_c_owner_example"
    assert memory_space_id_from_storage_name("raw-name") is None
    assert memory_space_id_from_storage_name("b64_not-valid-utf8_") is None


def test_deep_inventory_has_hashes_integrity_and_counts(tmp_path: Path) -> None:
    palace = tmp_path / _storage_name("realm-a")
    palace.mkdir()
    (palace / "note.txt").write_text("hello", encoding="utf-8")
    _create_sqlite(
        palace / "knowledge_graph.sqlite3",
        "CREATE TABLE entities(id TEXT PRIMARY KEY)",
        ["INSERT INTO entities VALUES ('self')"],
    )

    inventory = build_palaces_inventory(tmp_path, deep=True)

    assert inventory["palace_count"] == 1
    entry = inventory["palaces"][0]
    assert entry["memory_space_id"] == "realm-a"
    assert entry["sqlite"]["knowledge_graph.sqlite3"]["quick_check"] == "ok"
    assert entry["sqlite"]["knowledge_graph.sqlite3"]["counts"]["entities"] == 1
    note = next(item for item in entry["files"] if item["path"] == "note.txt")
    assert note["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert "error" not in entry["sqlite"]["knowledge_graph.sqlite3"]


def test_inventory_skips_hidden_runtime_directories(tmp_path: Path) -> None:
    (tmp_path / ".process-tmp").mkdir()
    (tmp_path / _storage_name("realm-a")).mkdir()

    inventory = build_palaces_inventory(tmp_path)

    assert inventory["palace_count"] == 1
