"""Live check of the Milvus vector path.

Skipped unless ``EIDOLON_MEMORY_MILVUS_TEST_URI`` names a reachable server, so an
ordinary test run never touches the network.

Everything here is confined to one database, named by
``EIDOLON_MEMORY_MILVUS_TEST_DB``. A Milvus instance is usually shared, and the
one we test against holds unrelated systems' databases. Collections are created
under a test-only namespace prefix and dropped by name afterwards — never by
scanning and clearing the database, which would delete data this test did not
create.

Run with::

    EIDOLON_MEMORY_MILVUS_TEST_URI=http://host:19530 \
    EIDOLON_MEMORY_MILVUS_TEST_DB=eidolon \
    uv run --extra milvus pytest tests/memory/test_live_milvus.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.mempalace_backend import mempalace_backend_env

_URI = os.environ.get("EIDOLON_MEMORY_MILVUS_TEST_URI", "").strip()
_DB = os.environ.get("EIDOLON_MEMORY_MILVUS_TEST_DB", "").strip()

# Distinct from any deployment namespace, so a stray collection is obviously ours.
_NAMESPACE = "eidolonpytest"

pytestmark = [
    pytest.mark.live_realm,
    pytest.mark.skipif(
        not (_URI and _DB),
        reason="set EIDOLON_MEMORY_MILVUS_TEST_URI and _TEST_DB to run",
    ),
]


def _settings() -> MemorySettings:
    return MemorySettings.model_validate(
        {
            "mempalace": {
                "backend": "milvus",
                "milvus_uri": _URI,
                "milvus_db_name": _DB,
                "milvus_namespace": _NAMESPACE,
                # Small English model; this checks the storage path, not recall
                # quality, and it keeps the download modest.
                "embedding_model": "minilm",
            }
        }
    )


def _client():
    from pymilvus import MilvusClient

    return MilvusClient(uri=_URI, db_name=_DB)


def _ours(client) -> list[str]:
    return [name for name in client.list_collections() if _NAMESPACE in name]


@pytest.fixture
def milvus_palace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A palace bound to the test database, cleaned up by name afterwards."""

    for key, value in mempalace_backend_env(_settings(), base={}).items():
        monkeypatch.setenv(key, value)

    client = _client()
    for stale in _ours(client):  # a previous interrupted run
        client.drop_collection(stale)

    palace = tmp_path / "palace"
    palace.mkdir(parents=True)
    try:
        yield palace
    finally:
        for created in _ours(_client()):
            _client().drop_collection(created)


def test_the_settings_reach_mempalace_as_its_environment_contract() -> None:
    env = mempalace_backend_env(_settings(), base={})

    assert env["MEMPALACE_BACKEND"] == "milvus"
    assert env["MEMPALACE_MILVUS_URI"] == _URI
    assert env["MEMPALACE_MILVUS_DB_NAME"] == _DB


def test_a_written_memory_is_readable_and_semantically_findable(milvus_palace: Path) -> None:
    from mempalace.palace import get_collection

    collection = get_collection(str(milvus_palace), create=True, backend="milvus")
    collection.upsert(
        ids=["live-probe"],
        documents=["the owner likes the colour green"],
        metadatas=[{"wing": "Wing_Life", "room": "colour", "source_file": "eidolon"}],
    )

    fetched = collection.get(ids=["live-probe"], include=["documents"])
    assert "the owner likes the colour green" in list(fetched["documents"])

    # Different words, same meaning: proves the embedder ran server-side rather
    # than this being an id lookup.
    found = collection.query(
        query_texts=["what colour do they like"],
        n_results=1,
        include=["documents"],
    )
    assert list(found["documents"][0]) == ["the owner likes the colour green"]

    collection.delete(ids=["live-probe"])
    assert collection.count() == 0


def test_collections_land_only_in_the_configured_database(milvus_palace: Path) -> None:
    """The instance is shared; a deployment must stay inside its own database."""

    from mempalace.palace import get_collection

    before = {db for db in _client().list_databases()}

    collection = get_collection(str(milvus_palace), create=True, backend="milvus")
    collection.upsert(
        ids=["scope-probe"],
        documents=["scope probe"],
        metadatas=[{"wing": "Wing_Work", "room": "init", "source_file": "eidolon"}],
    )

    assert _ours(_client()), "expected a namespaced collection in the test database"
    assert {db for db in _client().list_databases()} == before, (
        "creating a collection must not add or remove databases"
    )


def test_the_namespace_prefixes_every_collection_we_create(milvus_palace: Path) -> None:
    """Cleanup relies on this: without the prefix we could not tell ours apart."""

    from mempalace.palace import get_collection

    collection = get_collection(str(milvus_palace), create=True, backend="milvus")
    collection.upsert(
        ids=["prefix-probe"],
        documents=["prefix probe"],
        metadatas=[{"wing": "Wing_Work", "room": "init", "source_file": "eidolon"}],
    )

    created = _ours(_client())
    assert created
    for name in created:
        assert name.startswith(f"mempalace_{_NAMESPACE}_")
