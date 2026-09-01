"""One server, several spaces.

Every operator tool used to close over the handles the process opened at
startup, which made the server *be* a space rather than serve them: a second
space meant a second process, a second resident embedding model and a second
port. The tools now resolve handles per request, so the same server answers
about any space its router will open.

These tests pin what that has to mean — the named space is the one answered
about, the default still works unchanged, and a space this deployment does not
serve is refused rather than quietly answered with the default's contents.
"""

from __future__ import annotations

import tempfile

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRuntime,
    SpaceLedgers,
    UnknownMemorySpace,
)
from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp

SPACE_A = "default.alice.default"
SPACE_B = "default.bob.default"


class _Router:
    """Serves the spaces it was given, and refuses every other."""

    def __init__(self, *runtimes: MemorySpaceRuntime) -> None:
        self._runtimes = {runtime.space_id: runtime for runtime in runtimes}

    def serves(self, space_id: str) -> bool:
        return space_id in self._runtimes

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        runtime = self._runtimes.get(space_id)
        if runtime is None:
            raise UnknownMemorySpace(f"this deployment does not serve {space_id!r}")
        return runtime

    def held_spaces(self) -> list[str]:
        return list(self._runtimes)

    async def aclose(self) -> None:
        return None


def _runtime(space_id: str, *, ledgers: SpaceLedgers | None = None) -> MemorySpaceRuntime:
    return MemorySpaceRuntime(
        space_id=space_id,
        backend=FakeMemoryBackend(),
        palace_path=tempfile.mkdtemp(),
        kg=None,
        ledgers=ledgers or SpaceLedgers(),
    )


def _server(*runtimes: MemorySpaceRuntime):
    settings = load_memory_settings()
    service = MemoryService(_Router(*runtimes), settings)
    return build_control_plane_mcp(
        runtimes[0].backend,
        settings,
        service=service,
        memory_space_id=runtimes[0].space_id,
        palace_path=runtimes[0].palace_path,
        host="127.0.0.1",
        port=9999,
        surface="all",
    )


def _tool(mcp, name: str):
    return next(tool.fn for tool in mcp._tool_manager.list_tools() if tool.name == name)


async def _drawer(runtime: MemorySpaceRuntime, content: str) -> None:
    from eidolon.memory.application.ingest import ingest_memory_fragment
    from eidolon.memory.domain.fragments import MemoryFragment

    await ingest_memory_fragment(
        runtime.backend,
        MemoryFragment(
            memory_id=f"m-{content}",
            memory_space_id=runtime.space_id,
            source_device_id="device",
            source_instance_id="default",
            wing="Wing_Profile",
            room="profile_core",
            content=content,
            memory_type="preference",
            importance=4,
            confidence=0.95,
            source_turn_id=f"turn-{content}",
            session_id="s1",
        ),
    )


async def test_a_named_space_is_the_one_answered_about() -> None:
    """The whole point of the parameter. Answering from the default instead
    would report one person's memories under another person's name."""
    alice, bob = _runtime(SPACE_A), _runtime(SPACE_B)
    await _drawer(alice, "alice-drawer")
    await _drawer(bob, "bob-drawer")

    listing = _tool(_server(alice, bob), "eidolon_memory_list")

    only_bob = await listing(memory_space_id=SPACE_B)
    values = [record.get("value") for record in only_bob["records"]]
    assert any("bob-drawer" in (value or "") for value in values)
    assert not any("alice-drawer" in (value or "") for value in values)


async def test_omitting_the_space_still_answers_about_the_default() -> None:
    """Every caller that existed before the parameter did not pass one."""
    alice, bob = _runtime(SPACE_A), _runtime(SPACE_B)
    await _drawer(alice, "alice-drawer")
    await _drawer(bob, "bob-drawer")

    listing = _tool(_server(alice, bob), "eidolon_memory_list")

    values = [record.get("value") for record in (await listing())["records"]]
    assert any("alice-drawer" in (value or "") for value in values)
    assert not any("bob-drawer" in (value or "") for value in values)


async def test_a_space_this_deployment_does_not_serve_is_refused() -> None:
    """Substituting the default would turn a routing bug into a cross-tenant read
    that looks like it worked."""
    listing = _tool(_server(_runtime(SPACE_A)), "eidolon_memory_list")

    with pytest.raises(UnknownMemorySpace):
        await listing(memory_space_id=SPACE_B)


async def test_status_reports_the_resolved_space_not_the_process_default() -> None:
    """``palace_path`` in particular: naming the default's directory while
    reporting another space's readiness is worse than not reporting it."""
    alice, bob = _runtime(SPACE_A), _runtime(SPACE_B)
    status = _tool(_server(alice, bob), "eidolon_memory_status")

    reported = await status(memory_space_id=SPACE_B)
    assert reported["memory_space_id"] == SPACE_B
    assert reported["palace_path"] == bob.palace_path


async def test_a_space_without_the_ledger_is_refused_by_name() -> None:
    """A group is registered on what the default space keeps, so a space that
    keeps no such ledger can still be asked. That has to read as a deployment
    without the record, not as a broken tool."""

    class _Stats:
        async def stats(self):
            raise AssertionError("the default space's ledger must not answer for another space")

    alice = _runtime(SPACE_A, ledgers=SpaceLedgers(canonical_facts=_Stats()))
    bob = _runtime(SPACE_B)

    settings = load_memory_settings()
    service = MemoryService(_Router(alice, bob), settings)
    mcp = build_control_plane_mcp(
        alice.backend,
        settings,
        service=service,
        memory_space_id=SPACE_A,
        palace_path=alice.palace_path,
        host="127.0.0.1",
        port=9999,
        canonical_facts=alice.ledgers.canonical_facts,
        surface="all",
    )

    with pytest.raises(ValueError, match="keeps no canonical_facts ledger"):
        await _tool(mcp, "eidolon_memory_canonical_stats")(memory_space_id=SPACE_B)
