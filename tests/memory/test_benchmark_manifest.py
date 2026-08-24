"""A measurement without its provenance is not comparable to anything.

These tests pin the fields that decide what a number means, and the one mistake
that has already happened once: reporting a configured embedder as though it were
the one the index was built with.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from scripts.benchmark import manifest as m

jsonschema = pytest.importorskip("jsonschema")

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCHEMA = json.loads((_REPO / "benchmarks" / "manifest.schema.json").read_text())


def _settings(**mempalace) -> MemorySettings:
    return MemorySettings.model_validate({"mempalace": mempalace} if mempalace else {})


def test_a_manifest_validates_against_the_published_schema() -> None:
    built = m.build_manifest(
        suite="latency_chat",
        repo_root=_REPO,
        settings=_settings(),
        command="pytest",
    )

    jsonschema.validate(built, _SCHEMA)


def test_the_embedder_comes_from_the_palace_when_one_exists(tmp_path) -> None:
    """The palace records what its index was built with; config records intent.

    This is the distinction that made an earlier quality benchmark measure minilm
    while production ran embeddinggemma.
    """

    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "mempalace_embedder.json").write_text(
        json.dumps({"mempalace_drawers": {"model_name": "embeddinggemma"}})
    )

    facts = m.storage_facts(_settings(embedding_model="minilm"), palace_path=palace)

    assert facts["embedder"] == "embeddinggemma", "the palace must win over config"
    assert facts["embedder_source"] == "palace"


def test_a_configured_embedder_is_marked_as_weaker_evidence(tmp_path) -> None:
    """Before a palace is built there is nothing authoritative to read.

    Recording that fact is the point: a reader can then tell a measured embedder
    from an assumed one.
    """

    facts = m.storage_facts(
        _settings(embedding_model="embeddinggemma"), palace_path=tmp_path / "absent"
    )

    assert facts["embedder"] == "embeddinggemma"
    assert facts["embedder_source"] == "configured"


def test_an_unreadable_palace_marker_does_not_produce_a_confident_answer(
    tmp_path,
) -> None:
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "mempalace_embedder.json").write_text("not json")

    recorded = m.palace_embedder(palace)

    assert (recorded.name, recorded.source) == ("", "unknown")
    assert recorded.dimension is None


def test_a_dirty_tree_is_recorded_as_such() -> None:
    """A dirty run did not come from the recorded sha, so it cannot be a baseline."""

    provenance = m.code_provenance(_REPO)

    assert isinstance(provenance["dirty"], bool)
    assert provenance["git_sha"]


def test_the_machine_is_recorded_because_latency_is_a_property_of_it() -> None:
    facts = m.machine_facts()

    assert facts["platform"]
    assert facts["cpu_count"] > 0


def test_quality_runs_must_say_which_metric_they_report() -> None:
    """Retrieval recall and end-to-end accuracy are different measurements.

    Putting one beside the other unlabelled is how benchmark tables mislead, so
    the schema will not accept a dataset block without saying which it is.
    """

    built = m.build_manifest(
        suite="quality_longmemeval",
        repo_root=_REPO,
        settings=_settings(),
        command="pytest",
        dataset={
            "name": "longmemeval",
            "split": "held_out_450",
            "item_count": 450,
            "metric": "retrieval_recall",
        },
    )

    jsonschema.validate(built, _SCHEMA)

    built["dataset"]["metric"] = "made_up"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(built, _SCHEMA)


def test_the_schema_rejects_fields_it_does_not_know() -> None:
    """So a typo becomes a failure rather than a silently ignored field."""

    built = m.build_manifest(
        suite="latency_chat",
        repo_root=_REPO,
        settings=_settings(),
        command="pytest",
    )
    built["machien"] = {"platform": "typo"}

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(built, _SCHEMA)


# ── regression comparison ────────────────────────────────────────────────────


def test_a_large_latency_regression_is_reported() -> None:
    problems = m.compare_to_baseline(
        {"recall_chat": {"p95": 0.30}}, {"recall_chat": {"p95": 0.20}}
    )

    assert problems and "p95" in problems[0]


def test_ordinary_variation_between_runs_is_not() -> None:
    """A gate that fires on noise gets ignored, and then catches nothing."""

    assert (
        m.compare_to_baseline(
            {"recall_chat": {"p95": 0.21}}, {"recall_chat": {"p95": 0.20}}
        )
        == []
    )


def test_a_recall_drop_beyond_a_point_is_reported() -> None:
    problems = m.compare_to_baseline(
        {"quality": {"recall_at_5": 0.955}}, {"quality": {"recall_at_5": 0.980}}
    )

    assert problems and "R@5" in problems[0]


def test_an_improvement_is_never_a_regression() -> None:
    assert (
        m.compare_to_baseline(
            {"recall_chat": {"p95": 0.10}, "quality": {"recall_at_5": 0.99}},
            {"recall_chat": {"p95": 0.20}, "quality": {"recall_at_5": 0.98}},
        )
        == []
    )


def test_a_section_missing_from_the_baseline_is_not_a_regression() -> None:
    """A newly added measurement has nothing to compare against yet."""

    assert m.compare_to_baseline({"brand_new": {"p95": 5.0}}, {}) == []


def test_a_manifest_round_trips_through_disk(tmp_path) -> None:
    built = m.build_manifest(
        suite="latency_voice",
        repo_root=_REPO,
        settings=_settings(),
        command="pytest",
        scale={"drawers": 1000},
    )

    path = m.write_manifest(tmp_path / "run", built)
    reloaded = json.loads(path.read_text())

    jsonschema.validate(reloaded, _SCHEMA)
    assert reloaded["scale"]["drawers"] == 1000
