"""Everything needed to serve one memory space, and how it is obtained.

The service used to bind a process to a single space: one palace, one set of
handles, one port derived from the space id, one advisory lock on the palace
directory. That shape came from embedded storage — Chroma's client and the
SQLite ledgers each want a single owning process — and it was correct for a
laptop serving one person.

It is wrong as an architecture. It made the resident embedding model a per-space
cost, which is what limits how many spaces a host can serve, and it made a
deployment that stores nothing locally still behave as though it did.

So a space's handles become something a caller *asks for* rather than something
a process *is*:

    runtime = await router.resolve(space_id)

Two implementations, chosen by configuration, and the difference is not visible
above this line:

- Embedded storage keeps a pool. Each space has its own palace directory, its own
  storage handles, and its own lock; the embedding model is shared by the process
  because MemPalace caches it. One process serves many spaces.
- Shared storage keeps no pool. Handles are stateless views onto a vector server
  and a database, scoped by the space id passed with every call, so any replica
  can serve any space and replicas scale horizontally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eidolon.memory.domain.ports import VectorStorePort


@dataclass(frozen=True)
class SpaceLedgers:
    """The append-only records a space keeps alongside its memories.

    Grouped so callers take one parameter instead of six, and so the storage
    decision for all of them is made in one place — locally they are SQLite files
    in the palace directory, on shared storage they are rows in a database.

    Every one of them is optional: each consumer already treats None as "this
    deployment does not keep that record", which is what lets a minimal
    deployment run without provisioning any of it.
    """

    command_status: Any = None
    dlq: Any = None
    decisions: Any = None
    canonical_facts: Any = None
    commitments: Any = None
    sync: Any = None


@dataclass(frozen=True)
class MemorySpaceRuntime:
    """Handles for one space, resolved for the duration of one operation.

    ``palace_path`` is where MemPalace keeps a space's bookkeeping — its embedder
    identity and backend marker. With embedded storage that directory *is* the
    data and must persist. With a vector server the data lives remotely and the
    directory only holds markers derived from configuration, so a replica may
    rebuild it locally and discard it on exit.
    """

    space_id: str
    backend: VectorStorePort
    palace_path: str
    kg: Any = None
    ledgers: SpaceLedgers = SpaceLedgers()

    @property
    def has_kg(self) -> bool:
        return self.kg is not None


@runtime_checkable
class MemorySpaceRouter(Protocol):
    """Resolves a space id to the handles that serve it.

    Implementations must be safe to call concurrently for different spaces, and
    repeatedly for the same one — callers resolve per operation rather than
    holding a runtime across awaits.

    Whether resolving is cheap is an implementation detail a caller must not
    assume either way: a pool returns an existing handle set, while a stateless
    router may construct a view each time. Neither should do I/O proportional to
    the amount of data stored.
    """

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        """Return handles for ``space_id``.

        Raises :class:`UnknownMemorySpace` when this deployment does not serve
        it — which is a routing error, not an empty result.
        """
        ...

    def serves(self, space_id: str) -> bool:
        """Whether this deployment will resolve ``space_id`` at all."""
        ...

    async def aclose(self) -> None:
        """Release whatever was held. Idempotent."""
        ...


class UnknownMemorySpace(LookupError):
    """A space was addressed that this deployment does not serve.

    Distinct from "the space has no memories": that is a legitimate empty
    result, whereas this means the request reached the wrong place. Callers
    surface it rather than treating it as an empty recall, so a misrouted
    request is visible instead of looking like amnesia.

    Permanent for this deployment — retrying reaches the same wrong place. For a
    space we do serve but cannot open right now, see
    :class:`MemorySpaceUnavailable`.
    """


class MemorySpaceUnavailable(RuntimeError):
    """A space this deployment serves cannot be opened at the moment.

    Either another process holds it, or this one is already at its limit. Both
    are temporary and may resolve without any change to configuration, which is
    what separates them from :class:`UnknownMemorySpace` — a caller can retry
    this, and should not retry that.
    """
