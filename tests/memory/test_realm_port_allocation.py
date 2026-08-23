"""What decides a Realm's port, and the one case where that hurts.

Ports are derived from the realm id — deterministic, so a Realm keeps its port
across restarts with nobody recording the allocation. Collisions inside the
2000-port span are resolved by probing forward, and the roster is walked in
realm-id order, so the *set* of Realms is an input: adding one can take the slot
another already occupies and push that one to a different port. The supervisor
notices (``supervisor_reload_port_change``) and honours it by terminating and
respawning that Realm's agent — a silent restart of a Realm nobody touched.

This is characterization, not endorsement. It is reachable only with more than
one Realm on a Host, and after 多Companion记忆隔离机制裁决 an Owner has exactly
one Realm and the first release is one Owner per Host — so creating a Companion
cannot trigger it. It becomes reachable with the multi-Owner / family work
(轴 2), which is where it has to be decided: either allocation stops depending
on the set, or the supervisor keeps a live Realm's port when only collision
avoidance moved it.

Pinned here so that work cannot ship without noticing.
"""

from __future__ import annotations

import pytest

from eidolon_memory_contracts.runtime_route import (
    DEFAULT_MEMORY_MCP_BASE_PORT,
    MEMORY_MCP_PORT_SPAN,
    stable_memory_realm_port,
)


def _port(realm_id: str, *, used: set[int] | None = None) -> int:
    return stable_memory_realm_port(
        realm_id,
        base_port=DEFAULT_MEMORY_MCP_BASE_PORT,
        used_ports=used,
    )


def test_a_realm_alone_always_lands_on_the_same_port() -> None:
    """The property worth keeping: no stored allocation, stable across restarts."""
    first = _port("r_06607258a65055708c91880e8f2fb9a9")
    assert first == _port("r_06607258a65055708c91880e8f2fb9a9")
    assert DEFAULT_MEMORY_MCP_BASE_PORT <= first
    assert first < DEFAULT_MEMORY_MCP_BASE_PORT + MEMORY_MCP_PORT_SPAN


def test_two_realms_never_share_a_port() -> None:
    a = _port("r_a")
    b = _port("r_b", used={a})
    assert a != b


def test_allocation_never_moves_a_port_already_taken() -> None:
    """Whoever holds a slot keeps it; the newcomer probes forward.

    So allocation order decides who moves, and the roster is walked in realm-id
    order — meaning the id decides, not who arrived first in time.
    """
    incumbent = _port("r_a")
    newcomer = _port("r_b", used={incumbent})
    assert newcomer != incumbent
    assert _port("r_a", used={newcomer}) == incumbent


def test_adding_a_realm_can_move_an_existing_realms_port() -> None:
    """The defect, stated as an assertion so it cannot be forgotten.

    Two realm ids are chosen that hash to the same preferred port. Alone, each
    gets that port. Together, the one allocated second is pushed off it — so
    whichever of them the roster walks last changes port when the other appears.
    """
    base = DEFAULT_MEMORY_MCP_BASE_PORT
    span = MEMORY_MCP_PORT_SPAN

    # Find a colliding pair rather than hard-coding ids: the hash is an
    # implementation detail, the collision is the point.
    seen: dict[int, str] = {}
    pair: tuple[str, str] | None = None
    for index in range(20_000):
        realm_id = f"r_{index:032x}"
        preferred = _port(realm_id)
        if preferred in seen:
            pair = (seen[preferred], realm_id)
            break
        seen[preferred] = realm_id
    if pair is None:  # pragma: no cover - 20k ids over a 2000 span must collide
        pytest.fail("no colliding realm ids found; the span or hash changed")

    first, second = pair
    assert _port(first) == _port(second), "chosen pair does not collide"

    alone = _port(second)
    with_other_present = _port(second, used={_port(first)})
    assert with_other_present != alone
    assert base <= with_other_present < base + span
