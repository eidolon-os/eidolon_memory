"""Memory's operations contract against Memory's own configuration.

Memory is the component whose deployment can be wrong while everything reports
healthy. A palace opened with the wrong encoder still answers; it just answers
badly, and a query that should take milliseconds takes a minute. Two of the
tests here exist because that has already happened once.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from eidolon.memory.config.memory_settings import (
    _ENVIRONMENT_OVERRIDES,
    MemorySettings,
)

_REPOSITORY = Path(__file__).resolve().parents[1]
_CONTRACT = _REPOSITORY / "ops/component.toml"
_STATE_ROOT = "/var/lib/eidolon"


@pytest.fixture(scope="module")
def contract() -> dict:
    return tomllib.loads(_CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def settings() -> dict:
    return yaml.safe_load(
        (_REPOSITORY / "config/settings.yaml").read_text(encoding="utf-8")
    )


def test_the_declared_admin_port_is_the_one_the_supervisor_binds(
    contract: dict,
) -> None:
    declared = contract["ports"]["memory_admin"]["default"]
    default = MemorySettings.model_fields["supervisor"].annotation.model_fields[
        "admin_http_port"
    ].default

    assert declared == default


def test_the_declared_palace_root_is_under_the_declared_authority(
    contract: dict, settings: dict
) -> None:
    palaces = Path(
        settings["runtime"]["palaces_root"].replace("$EIDOLON_STATE_ROOT", _STATE_ROOT)
    )
    authority = [Path(entry["path"]) for entry in contract["state"]["authority"]]

    assert any(palaces.is_relative_to(path) for path in authority)


def test_everything_an_eidolon_remembers_is_declared_as_not_carried(
    contract: dict,
) -> None:
    entry = next(
        item
        for item in contract["state"]["authority"]
        if item["path"] == f"{_STATE_ROOT}/memory"
    )

    assert entry["backup"] == "none"
    # The reason has to name the thing that makes this hard rather than just
    # saying it is hard: Chroma stores the embedder on the collection, so a
    # restore is only meaningful alongside the model identity.
    assert "encoder" in entry["uncovered_reason"]


def test_the_encoder_a_host_needs_is_declared_as_an_artifact(contract: dict) -> None:
    artifact = contract["artifacts"][0]

    # Weights are not in git, so they are not in a release. This is the only
    # place that says so. Without it, a fresh Host either downloads on first
    # query or silently runs without an encoder — and the second is what
    # produced a memory query that took 71 seconds.
    assert artifact["kind"] == "model"
    assert artifact["files"], "an artifact with no files pins nothing"
    for entry in artifact["files"]:
        assert len(entry["sha256"]) == 64


def test_the_artifact_id_is_a_value_the_encoder_setting_accepts(
    contract: dict, settings: dict
) -> None:
    artifact_id = contract["artifacts"][0]["id"]
    configured = settings["embedding"]["model"]

    # A Host overrides the model with EIDOLON_MEMORY_EMBEDDING_MODEL; this file
    # carries the workstation's choice. They are deliberately different — a
    # laptop runs bge-large-zh, a board runs bge-base-zh — so what is checked
    # is that they are the same family, not the same value. A pin that no
    # setting could ever name would install weights nothing loads.
    assert artifact_id.startswith("bge-")
    assert configured.startswith("bge-")


def test_the_model_directory_can_be_pointed_at_the_installed_artifact(
    contract: dict,
) -> None:
    overrides = _ENVIRONMENT_OVERRIDES

    # Installing weights is only half of it: the service has to be told where
    # they are. This override is what carries the artifact's install_root into
    # the process, and its absence is what made a Host with the model on disk
    # still take a minute to answer.
    assert overrides["EIDOLON_MEMORY_EMBEDDING_MODEL_DIR"] == ("embedding", "model_dir")
    assert contract["artifacts"][0]["install_root"].startswith("/var/lib/eidolon")


def test_a_factory_reset_removes_everything_memory_holds(contract: dict) -> None:
    removed = [Path(item) for item in contract["reset"]["factory"]]

    for entry in contract["state"]["authority"]:
        assert any(Path(entry["path"]).is_relative_to(root) for root in removed)
