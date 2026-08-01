"""Startup warmup for processes that serve reads.

What warming means belongs to the store — see :class:`WarmableBackend`. What is
worth warming belongs here, because it is a recall decision: the wings a voice
turn reaches are the ones whose first read must not be the slow one.
"""

from __future__ import annotations

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import VectorStorePort, WarmableBackend
from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


async def warm_read_path(
    backend: VectorStorePort,
    settings: MemorySettings,
    *,
    role: str = "default",
) -> None:
    """Make the first read cost what a later one does, where that is possible.

    Blocking on purpose — called from startup, before the process accepts
    traffic, so that the cost lands here instead of on someone's first turn.

    A store that does not offer warming is not an error and not a fallback: with
    a vector server there is nothing on this side to warm.
    """

    apply_cpu_thread_env(settings, role=role)  # type: ignore[arg-type]

    if not isinstance(backend, WarmableBackend):
        log.info("warm_read_path_not_supported", backend=type(backend).__name__)
        return

    await backend.warm_read_path(wings=_wings_worth_warming(settings))


def _wings_worth_warming(settings: MemorySettings) -> list[str]:
    """The voice wings, since voice has the tightest budget of any read path.

    Falls back to every wing but Privacy when voice wings are unset — warming a
    private wing would load it into caches for a path that does not read it.
    """

    wings = settings.recall.voice_wings or [
        wing.id for wing in settings.wings if wing.id != "Wing_Privacy"
    ]
    return wings or [settings.wings[0].id]
