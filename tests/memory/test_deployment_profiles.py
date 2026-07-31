"""The local and cloud profiles must differ in values only.

The promise is that moving a deployment between one machine and many is a config
change. That is easy to claim and easy to break — a field that exists in only one
profile, or a shape only one of them can express, turns the move into a code
change. These tests pin it.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.registry import load_users_config
from eidolon.memory.infrastructure.mempalace_backend import (
    mempalace_backend_env,
    selected_mempalace_backend,
    vector_sqlite_integrity_targets,
)

_CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config"
_LOCAL = _CONFIG / "settings.example.yaml"
_CLOUD = _CONFIG / "settings.cloud.example.yaml"
_ROSTER = _CONFIG / "registry.example.yaml"


def _load(path: pathlib.Path) -> MemorySettings:
    return MemorySettings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def _keys(node, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    found: set[str] = set()
    for key, value in node.items():
        found |= _keys(value, f"{prefix}.{key}" if prefix else key)
    return found


@pytest.mark.parametrize("path", [_LOCAL, _CLOUD])
def test_both_profiles_are_valid_settings(path: pathlib.Path) -> None:
    _load(path)


def test_the_profiles_declare_the_same_fields() -> None:
    """A field present in one profile only means the move is not just values."""

    local = _keys(yaml.safe_load(_LOCAL.read_text(encoding="utf-8")))
    cloud = _keys(yaml.safe_load(_CLOUD.read_text(encoding="utf-8")))

    assert local == cloud, (
        "the profiles have diverged in shape; "
        f"only local: {sorted(local - cloud)}, only cloud: {sorted(cloud - local)}"
    )


def test_the_profiles_select_different_storage() -> None:
    """Sanity check that these are genuinely two deployment shapes."""

    assert selected_mempalace_backend(_load(_LOCAL)) == "chroma"
    assert selected_mempalace_backend(_load(_CLOUD)) == "milvus"
    assert _load(_LOCAL).kg.backend == "sqlite"
    assert _load(_CLOUD).kg.backend == "postgres"


def test_the_cloud_profile_confines_itself_to_one_milvus_database() -> None:
    """Shared instances are the norm; a stray deployment must not spill into one."""

    assert _load(_CLOUD).mempalace.milvus_db_name.strip()


def test_secrets_are_referenced_by_environment_variable_not_value() -> None:
    for path in (_LOCAL, _CLOUD):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        for section in raw.values():
            if not isinstance(section, dict):
                continue
            for key, value in section.items():
                if key.endswith("_env"):
                    assert value == "" or value.isupper(), (
                        f"{path.name}: {key} should name an environment variable, "
                        f"got {value!r}"
                    )


def test_local_integrity_checks_have_nothing_to_check_in_the_cloud(tmp_path) -> None:
    """A remote store's health is the server's to verify, not a startup gate."""

    local = vector_sqlite_integrity_targets(tmp_path, selected_mempalace_backend(_load(_LOCAL)))
    cloud = vector_sqlite_integrity_targets(tmp_path, selected_mempalace_backend(_load(_CLOUD)))

    assert local and not cloud


def test_the_cloud_profile_reaches_milvus_through_the_environment_bridge() -> None:
    """MemPalace is configured by environment, so the profile must translate."""

    env = mempalace_backend_env(_load(_CLOUD), base={})

    assert env["MEMPALACE_BACKEND"] == "milvus"
    assert env["MEMPALACE_MILVUS_URI"]
    assert env["MEMPALACE_MILVUS_DB_NAME"] == "eidolon"


def test_the_example_roster_serves_what_it_declares() -> None:
    settings = MemorySettings.model_validate(
        {"registry": {"source": "static", "static_path": str(_ROSTER)}}
    )

    roster = load_users_config(settings)

    assert [entry.id for entry in roster.users] == ["alice", "bob", "carol", "dave"]
    assert [entry.id for entry in roster.enabled_users()] == ["alice", "bob", "dave"]
    assert {entry.id for entry in roster.users if entry.consolidator_enabled()} == {"dave"}
