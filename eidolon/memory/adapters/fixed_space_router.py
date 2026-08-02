"""A router over handles that are already open, for exactly one space.

The other two routers open storage: one claims a directory, the other connects to
a server. This one opens nothing — it is handed the handles and answers with them,
provided the caller asks about the space they belong to.

It exists for the cases where a space has already been resolved and threading a
real router through would add a layer with nothing in it:

* a process that serves a single space and resolved it at startup;
* a test that wants a service over a fake backend.

Asking it about a different space raises :class:`UnknownMemorySpace`, the same as
any router that does not serve one. That matters more here than it looks: a
fixed-space router that quietly answered every space id with the same handles
would turn a routing bug into cross-tenant reads, and it would look like it was
working.
"""

from __future__ import annotations

from eidolon.memory.domain.space_runtime import (
    MemorySpaceRuntime,
    UnknownMemorySpace,
)


class FixedSpaceRouter:
    """Serves one space, from handles opened elsewhere."""

    def __init__(self, runtime: MemorySpaceRuntime) -> None:
        self._runtime = runtime

    def serves(self, space_id: str) -> bool:
        return space_id == self._runtime.space_id

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        if space_id != self._runtime.space_id:
            raise UnknownMemorySpace(
                f"this deployment serves only {self._runtime.space_id!r}, "
                f"not {space_id!r}"
            )
        return self._runtime

    def held_spaces(self) -> list[str]:
        return [self._runtime.space_id]

    async def aclose(self) -> None:
        """Nothing to release: whoever opened the handles closes them."""
        return None
