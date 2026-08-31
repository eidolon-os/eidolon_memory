"""Who each MCP tool is offered to.

Two things are being protected, and only one of them is about tokens.

The measurable one: 27 tools are 15,602 characters of name, description and JSON
schema — roughly 3,900 tokens in front of every agent request, of which 3,347
describe tools the agent must never call. On a board running a local model beside
the rest of Eidolon, that is context spent on noise.

The one that matters more: that list included ``forget_confirm``, ``dlq_replay``,
``dlq_resolve`` and ``kg_invalidate``. A model reading "忘了这件事吧"
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
)


class _Kg:
    """A graph whose method signatures match :class:`KnowledgeGraphPort` exactly.

    The signatures are the point, not the return values. A stub with
    ``**kwargs``, or one missing a method entirely, turns a tool that calls the
    port wrongly into an ``AttributeError`` or a silent success — and the
    invocation probe below would then pass over exactly the defect it exists to
    catch. That is not hypothetical: the first version of this file had
    ``known_audiences`` and nothing else, and it reported the broken
    ``kg_snapshot`` as fine.
    """

    async def known_audiences(self) -> list[str]:
        return ["owner"]

    async def list_entity_names(self) -> list[str]:
        return []

    async def match_entities_for_query(self, query: str, *, cap: int) -> list[str]:
        return []

    async def query_entity(
        self,
        name: str,
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        direction: str = "outgoing",
        include_sensitive: bool = False,
    ) -> list:
        return []

    async def query_subjects(
        self,
        names,
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        limit_per_subject: int = 8,
        include_sensitive: bool = False,
    ) -> list:
        return []

    async def query_entity_combined(
        self,
        names,
        *,
        audiences: tuple[str, ...],
        as_of: str | None = None,
        limit_per_entity: int = 8,
        include_sensitive: bool = False,
    ) -> list:
        return []

    async def timeline(
        self,
        entity_name: str | None = None,
        *,
        audiences: tuple[str, ...],
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        current_only: bool = False,
        include_sensitive: bool = False,
    ) -> list:
        return []

    async def stats(self) -> dict:
        return {"entities": 0, "triples_total": 0, "triples_active": 0}

    async def has_triple(self, triple_id: str) -> bool:
        return False


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


def test_the_agent_surface_is_exactly_the_read_tools_it_calls() -> None:
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

    assert agent_chars < 5_000, (
        f"the agent's tool surface has grown to {agent_chars} chars "
        f"(~{agent_chars // 4} tokens per request)"
    )
    assert all_chars > 3 * agent_chars, (
        "the operator surface is no longer meaningfully larger than the agent's, "
        "so the second endpoint may no longer be earning its complexity"
    )


def test_operator_surface_excludes_retired_direct_fact_write() -> None:
    """The ops surface has no second conversational fact source."""

    tools = _tools("all")
    assert len(tools) == 27
    assert "eidolon_memory_user_confirm" not in tools


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


# ── every registered tool can actually be called ──────────────────────────────


async def test_every_registered_tool_survives_being_invoked() -> None:
    """``kg_snapshot`` shipped calling ``timeline()`` without its required
    keyword-only ``audiences``, so every invocation raised ``TypeError``.

    Six of the seven graph tools passed it and one did not. That is not a
    discipline problem — it is what happens when seven call sites each have to
    remember the same argument in a project with no type checker
    (``test_layering`` says so in as many words), and a wiring mistake in a tool
    nothing calls stays invisible until an operator calls it.

    So: invoke every registered tool with its schema's required arguments and
    assert none fails to *bind*. Results are deliberately not asserted — a fake
    backend makes most of them meaningless, and a behavioural assertion here would
    have to be weakened per tool until it caught nothing. ``TypeError`` is the
    whole target.

    This only works because ``_Kg`` mirrors the port's real signatures. A thinner
    stub raises ``AttributeError`` first and the probe passes over the bug; the
    first version of this test did exactly that and was verified to be vacuous by
    re-breaking ``kg_snapshot`` and watching it stay green.
    """

    import inspect

    mcp = _build("all")
    tools = mcp._tool_manager.list_tools()
    assert len(tools) > 20, "the operator surface lost tools; this test is now vacuous"

    context = {
        "memory_realm_id": "default.alice.default",
        "owner_id": "alice",
        "companion_id": "default",
    }
    # Only what has no usable default. Plausible values rather than "" so a tool
    # that validates its input is still exercised.
    required_values = {
        "query": "什么",
        "context": context,
        "subject": "用户",
        "predicate": "likes",
        "object": "乌龙茶",
        "text": "用户喜欢乌龙茶",
        "target": "乌龙茶",
        "name": "用户",
        "entity_name": "用户",
        "source_turn_id": "turn-1",
        "request_id": "req-1",
        "entry_id": "dlq-1",
        "commitment_id": "c-1",
        "confirmation_token": "token-1",
        # Required on purpose: resolving a dead letter without recording why is
        # not something an operator should be able to do by omission.
        "note": "closed by the invocation probe",
    }

    binding_failures: list[str] = []
    unsupplied: list[str] = []
    for tool in tools:
        properties = (tool.parameters or {}).get("properties") or {}
        required = set((tool.parameters or {}).get("required") or ())
        missing = [name for name in required if name not in required_values]
        if missing:
            # A required argument this probe has no value for means the tool was
            # never actually invoked — which is the vacuum this test is guarding
            # against, so it fails rather than skipping quietly.
            unsupplied.append(f"{tool.name}: {sorted(missing)}")
            continue
        kwargs = {name: required_values[name] for name in properties if name in required}
        try:
            result = tool.fn(**kwargs)
            if inspect.isawaitable(result):
                await result
        except TypeError as exc:
            binding_failures.append(f"{tool.name}: {exc}")
        except Exception:
            # Anything else is a stand-in ledger refusing to do real work.
            pass

    assert not unsupplied, (
        "required_values has no entry for these, so they were skipped:\n  "
        + "\n  ".join(unsupplied)
    )
    assert not binding_failures, "tools that cannot be invoked:\n  " + "\n  ".join(binding_failures)


def test_the_agent_cannot_widen_its_own_visibility() -> None:
    """Neither visibility axis is a parameter the agent supplies.

    Audience was always derived: the agent passes ``context`` and
    ``readable_audiences`` computes what it may see. Sensitivity used to be an
    argument — ``include_sensitive_kg=True`` on the recall tool — which let the
    least-trusted caller grant itself a capability. It is a deployment setting
    now, and the operator surface keeps the explicit parameter.

    Checked on the schema rather than the signature, because the schema is what
    the model reads and therefore what it can ask for.
    """

    recall = next(
        tool
        for tool in _build("agent")._tool_manager.list_tools()
        if tool.name == "eidolon_memory_recall_context"
    )
    properties = set((recall.parameters or {}).get("properties") or {})

    assert "include_sensitive_kg" not in properties
    assert "audiences" not in properties
    # The derived input is still there — removing the axis, not the context.
    assert "context" in properties


async def test_agent_surface_rejects_bare_council_scope() -> None:
    """A Council id is an audience selector, not proof of participation."""

    search = next(
        tool
        for tool in _build("agent")._tool_manager.list_tools()
        if tool.name == "eidolon_memory_search"
    )

    with pytest.raises(ValueError, match="authoritative participant-scope adapter"):
        await search.fn(
            query="计划",
            context={
                "memory_realm_id": "default.alice.default",
                "owner_id": "alice",
                "companion_id": "default",
                "council_id": "caller-minted-council",
            },
        )


async def test_operator_surface_keeps_council_projection_access() -> None:
    """Closing the product entrance must not delete the storage contract."""

    search = next(
        tool
        for tool in _build("all")._tool_manager.list_tools()
        if tool.name == "eidolon_memory_search"
    )

    result = await search.fn(
        query="计划",
        context={
            "memory_realm_id": "default.alice.default",
            "owner_id": "alice",
            "companion_id": "default",
            "council_id": "operator-inspected-council",
        },
    )

    assert result == []
