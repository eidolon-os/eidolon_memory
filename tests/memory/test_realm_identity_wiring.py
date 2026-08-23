"""A realm runner is told who it serves, so the audience filter can apply.

The read side of the audience axis was built and tested — ``audience`` is a
column, the filter is an ``IN`` clause, an empty set returns nothing — and then
never given anything to compare against: ``_agent_cli_argv`` passed only the
space id and the port, so every runner in production ran with
``companion_id=None`` and could answer with the owner layer and nothing else.

The roster already carries owner and companion for every realm. This pins that
they reach the runner. It does **not** open companion-private writes: the write
side stays owner-layer until the per-owner data model lands (see
docs/跨系统/多Companion记忆隔离机制裁决.md §4.3), and
``test_kg_audience_layering.py`` is what holds that line.
"""

from __future__ import annotations

from pathlib import Path

from eidolon.memory.config.users import UserEntry
from eidolon.memory.entrypoints.agent_runner import _parse_args
from eidolon.memory.entrypoints.recollections_http import _context
from eidolon.memory.entrypoints.supervisor import _agent_cli_argv


def _argv(entry: UserEntry) -> list[str]:
    return _agent_cli_argv(entry, Path("/unused"))


def test_supervisor_passes_owner_and_companion_from_the_roster() -> None:
    argv = _argv(
        UserEntry(id="r_a", owner_id="o_1", companion_id="c_1", port=10_030)
    )
    assert "--owner-id" in argv and argv[argv.index("--owner-id") + 1] == "o_1"
    assert "--companion-id" in argv and argv[argv.index("--companion-id") + 1] == "c_1"


def test_an_owner_scoped_realm_names_no_companion() -> None:
    """After the per-owner migration a realm serves every Companion the Owner has.

    So the flag is absent rather than empty — the runner then applies the owner
    layer, which is exactly right for a space that belongs to no single
    Companion.
    """
    argv = _argv(UserEntry(id="r_a", owner_id="o_1", companion_id=None, port=10_030))
    assert "--companion-id" not in argv
    assert argv[argv.index("--owner-id") + 1] == "o_1"


def test_the_runner_accepts_them_and_defaults_to_neither() -> None:
    parsed = _parse_args(
        [
            "--memory-space-id",
            "r_a",
            "--port",
            "10030",
            "--owner-id",
            "o_1",
            "--companion-id",
            "c_1",
        ]
    )
    assert (parsed.owner_id, parsed.companion_id) == ("o_1", "c_1")

    bare = _parse_args(["--memory-space-id", "r_a"])
    assert (bare.owner_id, bare.companion_id) == ("", "")


def test_the_identity_reaches_the_recall_context() -> None:
    """The end of the wire: what the filter actually reads."""
    context = _context(memory_space_id="r_a", owner_id="o_1", companion_id="c_1")
    assert context.memory_space_id == "r_a"
    assert context.memory_realm_id == "r_a"
    assert context.owner_id == "o_1"
    assert context.companion_id == "c_1"

    without = _context(memory_space_id="r_a", owner_id=None, companion_id=None)
    assert without.companion_id is None
