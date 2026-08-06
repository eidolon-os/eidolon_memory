"""Who each MCP tool is offered to.

Two things are being protected, and only one of them is about tokens.

The measurable one: 27 tools are 15,602 characters of name, description and JSON
schema — roughly 3,900 tokens in front of every agent request, of which 3,347
describe tools the agent must never call. On a board running a local model beside
the rest of Eidolon, that is context spent on noise.

The one that matters more: that list included ``forget_confirm``, ``dlq_replay``,
``dlq_resolve``, ``kg_invalidate`` and ``user_confirm``. A model reading "忘了这件事吧"
from a user had a plausible destructive tool within reach and nothing but its own
judgement in between. Removing it from the list is a stronger guarantee than
prompting against it.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.config.memory_settings import MemorySettings, load_memory_settings
from eidolon.memory.entrypoints.mcp_server import (
    AGENT_SURFACE_TOOLS,
    build_control_plane_mcp,
)

#: Tools that change or destroy stored memory, or act on the user's behalf.
DESTRUCTIVE = (
    "eidolon_memory_forget_confirm",
    "eidolon_memory_dlq_replay",
    "eidolon_memory_dlq_resolve",
    "eidolon_memory_kg_invalidate",
    "eidolon_memory_kg_add_triple",
    "eidolon_memory_user_confirm",
)


class _Kg:
    async def known_audiences(self):
        return ("owner",)


class _Present:
    """Stands in for a ledger: registration only checks it is not None."""


def _build(surface: str = "all"):
    return build_control_plane_mcp(
        FakeMemoryBackend(),
        load_memory_settings(),
        memory_space_id="default.alice.default",
        palace_path=tempfile.mkdtemp(),
        host="127.0.0.1",
        port=9999,
        kg=_Kg(),
        command_publisher=_Present(),
        command_status=_Present(),
        canonical_facts=_Present(),
        commitments=_Present(),
        dlq_store=_Present(),
        replay_publisher=_Present(),
        surface=surface,
        path=None if surface == "all" else "/ops/mcp",
    )


def _tools(surface: str) -> set[str]:
    return {t.name for t in _build(surface)._tool_manager.list_tools()}


def _schema_chars(surface: str) -> int:
    return sum(
        len(
            json.dumps(
                {"name": t.name, "description": t.description, "inputSchema": t.parameters},
                ensure_ascii=False,
            )
        )
        for t in _build(surface)._tool_manager.list_tools()
    )


def test_the_agent_surface_is_exactly_the_two_tools_it_calls() -> None:
    assert _tools("agent") == set(AGENT_SURFACE_TOOLS)


def test_no_destructive_tool_is_offered_to_the_agent() -> None:
    """The reason for the split that is not about context size.

    Asserted against a named list rather than against "everything except two", so
    that adding a seventh destructive tool to the operator surface does not quietly
    become a seventh way for a conversation to trigger one.
    """

    offered = _tools("agent")

    assert not [name for name in DESTRUCTIVE if name in offered]


def test_every_destructive_tool_is_still_reachable_by_an_operator() -> None:
    """The other half: splitting must not remove a capability, only relocate it.

    A test that only checked the agent surface would pass just as well if the tools
    had been deleted.
    """

    offered = _tools("all")

    assert not [name for name in DESTRUCTIVE if name not in offered]


def test_the_operator_surface_is_a_superset() -> None:
    """An operator debugging a space wants ``search`` before anything else. A
    second endpoint that could not answer the first question asked of it would
    just send people back to the agent's."""

    assert _tools("agent") < _tools("all")


def test_the_split_is_worth_its_complexity() -> None:
    """The measurement that justifies a second endpoint, kept where it can be
    re-run rather than quoted from a commit message.

    Bounds rather than exact numbers: this should fail when the agent surface grows
    a tool nobody weighed, and not when a description is reworded.
    """

    agent_chars = _schema_chars("agent")
    all_chars = _schema_chars("all")

    assert agent_chars < 3_500, (
        f"the agent's tool surface has grown to {agent_chars} chars "
        f"(~{agent_chars // 4} tokens per request)"
    )
    assert all_chars > 3 * agent_chars, (
        "the operator surface is no longer meaningfully larger than the agent's, "
        "so the second endpoint may no longer be earning its complexity"
    )


def test_the_default_surface_is_unchanged() -> None:
    """``build_control_plane_mcp`` has other callers — tests, the admin path — and
    the split must not have moved anything under them."""

    assert len(_tools("all")) == 27


def test_the_two_surfaces_have_distinct_paths() -> None:
    """They share a port. Identical paths would mean the second mount shadows the
    first, and whichever won would look like the whole server."""

    mcp_http = MemorySettings().mcp_http

    assert mcp_http.path != mcp_http.ops_path
    assert mcp_http.base_url(port=1234) == "http://127.0.0.1:1234/mcp"
    assert mcp_http.ops_base_url(port=1234) == "http://127.0.0.1:1234/ops/mcp"


@pytest.mark.parametrize("surface", ["agent", "all"])
def test_both_surfaces_answer_from_the_same_service(surface: str) -> None:
    """The split is about who may see a tool, not about which store it reaches.

    Two servers over two different backends would be a far worse bug than the one
    being fixed, and it would look identical from the tool list.
    """

    backend = FakeMemoryBackend()
    built = build_control_plane_mcp(
        backend,
        load_memory_settings(),
        memory_space_id="default.alice.default",
        palace_path=tempfile.mkdtemp(),
        host="127.0.0.1",
        port=9999,
        surface=surface,
        path=None if surface == "all" else "/ops/mcp",
    )

    assert built is not None
    assert set(AGENT_SURFACE_TOOLS) <= {t.name for t in built._tool_manager.list_tools()}
