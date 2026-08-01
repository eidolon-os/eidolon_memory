"""The raw shape of a palace's room graph, before any presentation decisions.

A room that appears under more than one wing is a tunnel between them, which is
what makes this worth visualising. Extracting the rooms is something only the
store can do; deciding which of them to show is not, so the two are separated
here — the store returns everything it has, and the logic layer ranks and caps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoomNode:
    """One room, and the wings it appears under."""

    wings: tuple[str, ...] = ()
    halls: tuple[str, ...] = ()
    count: int = 0

    @property
    def is_tunnel(self) -> bool:
        """Whether this room connects wings rather than sitting inside one."""
        return len(self.wings) >= 2


@dataclass(frozen=True)
class RoomGraphSnapshot:
    """Every room a store knows about, with whatever summary it keeps.

    ``stats`` is passed through rather than modelled: it is a store's own summary
    of itself, and giving it a schema here would mean this file changes whenever
    a store learns to count something new.
    """

    rooms: dict[str, RoomNode] = field(default_factory=dict)
    stats: dict[str, Any] | None = None
