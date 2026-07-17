"""Contracts for the operation-level memory quality scorer."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eidolon_sdk.memory import (
    conversation_turn_subject,
    unwrap_memory_payload,
)

from scripts.benchmark import bench_memory_retrieve_quality as quality_bench
from scripts.benchmark.bench_memory_retrieve_quality import (
    _aggregate,
    _publish_turn,
    _run_query,
    _score_query,
    _spawn_agent,
    _validate_queries,
    _wait_mcp_ready,
)


def _answerable_query() -> dict:
    return {
        "id": "q-1",
        "category": "information_extraction",
        "query": "what is known about the subject?",
        "expected_entities": ["subject:alpha"],
        "expected_vector_contains": ["marker-vector"],
    }


def test_answerable_case_requires_every_labelled_evidence_group() -> None:
    result = _score_query(
        _answerable_query(),
        {
            "kg_triples": [
                {"subject": "subject:alpha", "predicate": "related_to", "object": "x"}
            ],
            "records": [{"value": "unrelated record"}],
        },
        12.5,
    )

    assert result.kg_hit is True
    assert result.vector_hit is False
    assert result.evidence_groups_hit == 1
    assert result.evidence_groups_total == 2
    assert result.omission_count == 1
    assert result.evidence_recall == 0.5
    assert result.correct is False


def test_vector_text_cannot_mask_a_missing_kg_entity() -> None:
    result = _score_query(
        _answerable_query(),
        {
            "kg_triples": [],
            "records": [{"value": "subject:alpha marker-vector"}],
        },
        1.0,
    )

    assert result.kg_hit is False
    assert result.vector_hit is True
    assert result.correct is False


def test_abstention_requires_an_empty_retrieval_boundary() -> None:
    query = {
        "id": "q-abstain",
        "category": "abstention",
        "query": "unknown premise",
        "expect_abstention": True,
    }

    clean = _score_query(query, {}, 1.0)
    contaminated = _score_query(
        query,
        {"records": [{"value": "nearest but unsupported memory"}]},
        1.0,
    )

    assert clean.abstention_correct is True
    assert clean.correct is True
    assert contaminated.abstention_correct is False
    assert contaminated.returned_evidence_count == 1
    assert contaminated.correct is False


def test_query_label_validation_rejects_vacuous_and_conflicting_cases() -> None:
    with pytest.raises(ValueError, match="at least one evidence group"):
        _validate_queries(
            [{"id": "empty", "category": "recall", "query": "anything"}]
        )

    with pytest.raises(ValueError, match="cannot also require positive evidence"):
        _validate_queries(
            [
                {
                    "id": "conflict",
                    "category": "abstention",
                    "query": "anything",
                    "expect_abstention": True,
                    "expected_entities": ["subject:alpha"],
                }
            ]
        )


def test_quality_fixture_has_no_vacuous_cases() -> None:
    fixture = (
        Path(__file__).parent
        / "e2e"
        / "fixtures"
        / "quality_queries.jsonl"
    )
    queries = [json.loads(line) for line in fixture.read_text().splitlines() if line]

    _validate_queries(queries)


def test_aggregate_reports_omission_and_abstention_separately() -> None:
    partial = _score_query(
        _answerable_query(),
        {
            "kg_triples": [
                {"subject": "subject:alpha", "predicate": "related_to", "object": "x"}
            ]
        },
        10.0,
    )
    abstained = _score_query(
        {
            "id": "q-abstain",
            "category": "abstention",
            "query": "unknown premise",
            "expect_abstention": True,
        },
        {},
        20.0,
    )

    overall = _aggregate([partial, abstained])["overall"]

    assert overall["correct"] == 1
    assert overall["evidence_recall"] == 0.5
    assert overall["omissions"] == 1
    assert overall["abstention_correct"] == 1
    assert overall["abstention_total"] == 1


@pytest.mark.asyncio
async def test_publish_turn_uses_current_envelope_and_encoded_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _JetStream:
        async def publish(self, subject, body):
            captured.update(subject=subject, body=body)

    class _Nats:
        def jetstream(self):
            return _JetStream()

        async def close(self):
            return None

    async def _connect(_url):
        return _Nats()

    monkeypatch.setattr(quality_bench.nats, "connect", _connect)

    await _publish_turn(
        "nats://test",
        user_id="r:quality:bench",
        turn={"turn_id": "t-1", "user_text": "u", "assistant_text": "a"},
    )

    assert captured["subject"] == conversation_turn_subject("r:quality:bench")
    payload = unwrap_memory_payload(json.loads(captured["body"]))
    assert payload["context"]["memory_realm_id"] == "r:quality:bench"
    assert payload["context"]["memory_space_id"] == "r:quality:bench"
    assert payload["turn_id"] == "t-1"


@pytest.mark.asyncio
async def test_query_passes_actor_context_to_current_mcp_contract() -> None:
    class _Session:
        args = None

        async def call_tool(self, name, args):
            self.args = (name, args)
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps({"records": []}))]
            )

    session = _Session()
    context = {
        "owner_id": "quality_bench",
        "memory_realm_id": "r:quality:bench",
        "memory_space_id": "r:quality:bench",
    }
    await _run_query(
        session,
        {
            "id": "q",
            "category": "abstention",
            "query": "unknown",
            "expect_abstention": True,
        },
        context=context,
    )

    assert session.args == (
        "eidolon_memory_recall_context",
        {"query": "unknown", "context": context, "top_k": 5, "voice": False},
    )


def test_spawn_uses_current_memory_space_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    class _Proc:
        pid = 123

        def terminate(self):
            return None

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        kwargs["stdout"].close()
        return _Proc()

    monkeypatch.setattr(quality_bench.subprocess, "Popen", _popen)
    monkeypatch.setattr(quality_bench, "_wait_mcp_ready", lambda _port: True)

    _spawn_agent(
        user_id="r:quality:bench",
        port=19200,
        palace_root=tmp_path / "palaces",
        settings_path=tmp_path / "settings.yaml",
        log_path=tmp_path / "agent.log",
        steward_mode="rules",
    )

    assert captured["argv"][1:3] == ["--memory-space-id", "r:quality:bench"]
    settings = quality_bench.yaml.safe_load(
        (tmp_path / "settings.yaml").read_text()
    )
    assert settings["steward"]["mode"] == "rules"


def test_readiness_probe_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(status_code=400)

    monkeypatch.setattr(quality_bench.httpx, "get", _get)

    assert _wait_mcp_ready(19200, timeout_s=0.1) is True
    assert captured["trust_env"] is False
