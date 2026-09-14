"""Lossless storage-to-wire mapping, including provenance and visibility."""

from eidolon.memory.adapters.mempalace_results import storage_record


def test_storage_record_keeps_id_metadata_and_does_not_mutate_input():
    metadata = {
        "wing": "Wing_Life",
        "room": "tea",
        "audience": "owner",
        "source_device_id": "phone",
        "visibility": "current_device",
        "source_turn_id": "turn-1",
        "source_file": "note.md",
        "occurred_at": "2026-05-18T20:00:00Z",
        "created_at": "2026-05-19T10:01:00Z",
    }
    before = dict(metadata)
    record = storage_record("drawer_real", '{"tea": "oolong"}', metadata, memory_space_id="realm")
    assert record.key == "tea"
    assert record.value == {"tea": "oolong"}
    assert record.metadata["_storage_id"] == "drawer_real"
    assert all(record.metadata[k] == v for k, v in metadata.items())
    assert record.memory_time.isoformat() == "2026-05-18T20:00:00+00:00"
    assert record.memory_time_source == "occurred_at"
    assert record.created_at is not None
    assert metadata == before


def test_get_preserves_raw_document_and_storage_key():
    record = storage_record("drawer_real", '{"tea": "oolong"}', {}, search=False)
    assert record.key == "drawer_real"
    assert record.value == '{"tea": "oolong"}'


def test_plain_text_and_missing_dates_remain_valid():
    record = storage_record(
        "drawer_plain", "hello", {"memory_space_id": "other"}, memory_space_id="realm"
    )
    assert record.memory_space_id == "other"
    assert record.value == "hello"
    assert record.created_at is None
