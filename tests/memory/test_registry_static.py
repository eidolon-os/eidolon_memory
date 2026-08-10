"""The static roster — what lets the service run without an admin service."""

from __future__ import annotations

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.registry import (
    RegistryPort,
    build_registry,
    load_users_config,
    resolve_static_registry_path,
)
from eidolon.memory.config.registry_static import StaticFileRegistry
from eidolon.memory.config.users import RegistrySourceUnavailable, SystemDataRegistry


def _settings(**registry) -> MemorySettings:
    return MemorySettings.model_validate({"registry": registry} if registry else {})


def _write(tmp_path, body: str):
    path = tmp_path / "roster.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_minimal_entry_needs_only_an_id(tmp_path) -> None:
    """Ports are derived, so an operator declares spaces without tracking them."""

    path = _write(tmp_path, "memory_spaces:\n  - id: alice\n")

    roster = StaticFileRegistry(_settings(), path=path).load()

    assert [entry.id for entry in roster.users] == ["alice"]
    assert roster.users[0].enabled
    assert roster.users[0].port > 0


def test_derived_ports_are_stable_across_loads(tmp_path) -> None:
    """A space keeps its port across restarts without recording the allocation."""

    path = _write(tmp_path, "memory_spaces:\n  - id: alice\n  - id: bob\n")
    registry = StaticFileRegistry(_settings(), path=path)

    first = {entry.id: entry.port for entry in registry.load().users}
    second = {entry.id: entry.port for entry in registry.load().users}

    assert first == second
    assert len(set(first.values())) == 2, "distinct spaces must not share a port"


def test_an_explicit_port_wins(tmp_path) -> None:
    path = _write(tmp_path, "memory_spaces:\n  - id: alice\n    port: 11111\n")

    roster = StaticFileRegistry(_settings(), path=path).load()

    assert roster.users[0].port == 11111


def test_owner_id_defaults_to_the_space_id(tmp_path) -> None:
    path = _write(tmp_path, "memory_spaces:\n  - id: alice\n")

    assert StaticFileRegistry(_settings(), path=path).load().users[0].owner_id == "alice"


def test_a_disabled_space_is_kept_but_not_served(tmp_path) -> None:
    path = _write(
        tmp_path,
        "memory_spaces:\n  - id: alice\n  - id: bob\n    enabled: false\n",
    )

    roster = StaticFileRegistry(_settings(), path=path).load()

    assert len(roster.users) == 2
    assert [entry.id for entry in roster.enabled_users()] == ["alice"]


def test_the_consolidator_stays_off_unless_asked_for(tmp_path) -> None:
    """It costs LLM calls, so declaring a space must not start one."""

    path = _write(
        tmp_path,
        "memory_spaces:\n"
        "  - id: alice\n"
        "  - id: bob\n"
        "    consolidator:\n"
        "      enabled: true\n",
    )

    roster = StaticFileRegistry(_settings(), path=path).load()
    by_id = {entry.id: entry for entry in roster.users}

    assert not by_id["alice"].consolidator_enabled()
    assert by_id["bob"].consolidator_enabled()


def test_a_missing_file_is_unavailable_not_empty(tmp_path) -> None:
    """An empty roster stops every worker; an unreadable source must not."""

    registry = StaticFileRegistry(_settings(), path=tmp_path / "absent.yaml")

    with pytest.raises(RegistrySourceUnavailable):
        registry.load()


@pytest.mark.parametrize(
    "body",
    [
        "",  # no mapping at all
        "memory_spaces: {}",  # wrong shape
        "other_key: []",  # missing the key entirely
        "memory_spaces:\n  - port: 10030\n",  # entry without an id
        "memory_spaces:\n  - just-a-string\n",  # entry that is not a mapping
    ],
)
def test_a_malformed_roster_is_rejected_rather_than_half_read(tmp_path, body: str) -> None:
    registry = StaticFileRegistry(_settings(), path=_write(tmp_path, body))

    with pytest.raises(RegistrySourceUnavailable):
        registry.load()


def test_an_empty_list_is_a_valid_empty_roster(tmp_path) -> None:
    """Distinct from malformed: the operator is saying "serve nothing"."""

    roster = StaticFileRegistry(_settings(), path=_write(tmp_path, "memory_spaces: []")).load()

    assert roster.users == []


def test_duplicate_ids_are_rejected(tmp_path) -> None:
    registry = StaticFileRegistry(
        _settings(), path=_write(tmp_path, "memory_spaces:\n  - id: alice\n  - id: alice\n")
    )

    with pytest.raises(ValueError):
        registry.load()


def test_two_enabled_spaces_may_not_share_an_explicit_port(tmp_path) -> None:
    registry = StaticFileRegistry(
        _settings(),
        path=_write(
            tmp_path,
            "memory_spaces:\n"
            "  - id: alice\n    port: 10030\n"
            "  - id: bob\n    port: 10030\n",
        ),
    )

    with pytest.raises(ValueError):
        registry.load()


def test_source_selection_follows_configuration(tmp_path) -> None:
    system_data = build_registry(_settings())
    static = build_registry(
        _settings(source="static", static_path=str(_write(tmp_path, "memory_spaces: []")))
    )

    assert isinstance(system_data, SystemDataRegistry)
    assert isinstance(static, StaticFileRegistry)
    assert isinstance(static, RegistryPort)


def test_static_source_without_a_path_says_so(tmp_path) -> None:
    with pytest.raises(RegistrySourceUnavailable, match="static_path"):
        resolve_static_registry_path(_settings(source="static"))


def test_load_users_config_honours_the_static_source(tmp_path) -> None:
    """The entrypoints call this; it must reach the file without an admin service."""

    settings = _settings(
        source="static",
        static_path=str(_write(tmp_path, "memory_spaces:\n  - id: alice\n")),
    )

    assert [entry.id for entry in load_users_config(settings).users] == ["alice"]
