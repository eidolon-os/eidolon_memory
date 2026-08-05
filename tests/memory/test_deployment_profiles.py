"""The shipped configuration template has to be a working deployment.

An example settings file is documentation that runs, so the ways it can rot are
the ways documentation rots — a field that no longer validates, a secret written
in place of the variable that holds it, an embedder left blank. None of those are
visible by reading it.

This suite once compared a local profile against a cloud one, asserting they
differed in values only. There is one profile now.
"""

from __future__ import annotations

import pathlib

import yaml

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.registry import load_users_config
from eidolon.memory.infrastructure.mempalace_backend import (
    selected_mempalace_backend,
    vector_sqlite_integrity_targets,
)

_CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config"
_LOCAL = _CONFIG / "settings.example.yaml"
_ROSTER = _CONFIG / "registry.example.yaml"


def _load(path: pathlib.Path) -> MemorySettings:
    return MemorySettings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def test_the_example_is_valid_settings() -> None:
    _load(_LOCAL)


def test_secrets_are_referenced_by_environment_variable_not_value() -> None:
    """A template is committed, so a value here would be a committed secret."""

    raw = yaml.safe_load(_LOCAL.read_text(encoding="utf-8"))
    for section in raw.values():
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            if key.endswith("_env"):
                assert value == "" or value.isupper(), (
                    f"{_LOCAL.name}: {key} should name an environment variable, "
                    f"got {value!r}"
                )


def test_the_configured_store_has_something_to_check_at_startup() -> None:
    """Embedded storage is verifiable before serving, and that is the point of it
    being embedded: a corrupt file is found at startup rather than on a read."""

    targets = vector_sqlite_integrity_targets(
        pathlib.Path("/nonexistent"), selected_mempalace_backend(_load(_LOCAL))
    )

    assert targets


def test_the_example_roster_serves_what_it_declares() -> None:
    settings = MemorySettings.model_validate(
        {"registry": {"source": "static", "static_path": str(_ROSTER)}}
    )

    roster = load_users_config(settings)

    assert [entry.id for entry in roster.users] == ["alice", "bob", "carol", "dave"]
    assert [entry.id for entry in roster.enabled_users()] == ["alice", "bob", "dave"]
    assert {entry.id for entry in roster.users if entry.consolidator_enabled()} == {"dave"}


def test_the_embedder_is_named_rather_than_defaulted() -> None:
    """Blank means we pass no model and MemPalace picks minilm, its own default.

    A new palace would then silently be built with the English-only encoder,
    which measures 5/43 top-1 on our Chinese corpus. Naming it is the difference
    between a deliberate choice and an accident — and this exact accident is what
    every quality figure before 2026-08-03 was measured under.
    """

    assert _load(_LOCAL).mempalace.embedding_model.strip()
