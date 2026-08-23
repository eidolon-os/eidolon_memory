"""A realm knows whose memory it is; each request says who is asking.

The read side of the audience axis was built and tested — ``audience`` is a
column, the filter is an ``IN`` clause, an empty set returns nothing rather than
everything — and then never given anything to compare against: the runner was
started with only the space id and the port.

The split matters. A space belongs to an **Owner** and serves every Companion
that Owner has (docs/跨系统/多Companion记忆隔离机制裁决.md), so:

  - the Owner is a property of the space → passed at startup;
  - which Companion is asking is a property of the request → a query parameter.

Passing a Companion at startup would pin the audience filter to whichever one
happened to be named, and every other Companion on that Owner would silently
read as if it were that one.

None of this opens companion-private *writes*: every production write still
lands in the owner layer, and ``test_kg_audience_layering.py`` holds that line.
"""

from __future__ import annotations

from pathlib import Path

from eidolon.memory.config.users import UserEntry
from eidolon.memory.entrypoints.agent_runner import _parse_args
from eidolon.memory.entrypoints.recollections_http import _context
from eidolon.memory.entrypoints.supervisor import _agent_cli_argv


def _argv(entry: UserEntry) -> list[str]:
    return _agent_cli_argv(entry, Path("/unused"))


def test_supervisor_passes_the_owner_from_the_roster() -> None:
    argv = _argv(UserEntry(id="r_a", owner_id="o_1", port=10_030))
    assert argv[argv.index("--owner-id") + 1] == "o_1"


def test_supervisor_never_pins_a_companion_at_startup() -> None:
    """The regression this file exists for.

    A realm serving one Companion is the shape we left behind; a startup flag
    naming one would bring it back in effect, without bringing back the schema
    that made it visible.
    """
    argv = _argv(UserEntry(id="r_a", owner_id="o_1", port=10_030))
    assert "--companion-id" not in argv


def test_the_runner_accepts_the_owner_and_defaults_to_none() -> None:
    parsed = _parse_args(
        ["--memory-space-id", "r_a", "--port", "10030", "--owner-id", "o_1"]
    )
    assert parsed.owner_id == "o_1"
    assert not hasattr(parsed, "companion_id")

    bare = _parse_args(["--memory-space-id", "r_a"])
    assert bare.owner_id == ""


def test_the_asking_companion_reaches_the_recall_context() -> None:
    """The end of the wire: what the audience filter actually reads."""
    context = _context(memory_space_id="r_a", owner_id="o_1", companion_id="c_1")
    assert context.memory_space_id == "r_a"
    assert context.memory_realm_id == "r_a"
    assert context.owner_id == "o_1"
    assert context.companion_id == "c_1"


def test_no_asking_companion_means_the_owner_layer() -> None:
    context = _context(memory_space_id="r_a", owner_id="o_1", companion_id=None)
    assert context.companion_id is None
