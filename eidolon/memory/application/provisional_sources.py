"""Scoped source lookup for trusted runtime callers on the existing private bus."""

from eidolon_memory_contracts import MemoryActorContext


async def query_provisional_sources(payload, *, memory_space_id, decision_store):
    # The caller must authorize and freshly verify the actor; the worker enables
    # this responder only under its Data roster's independent read permission.
    if not isinstance(payload, dict) or set(payload) != {"context"}:
        raise ValueError("provisional query requires only an actor context")
    context = MemoryActorContext.model_validate(payload["context"])
    if context.memory_space_id != memory_space_id:
        raise ValueError("provisional query is outside this Realm")
    if not (
        context.owner_id
        and context.person_id
        and context.session_id
        and (context.companion_id or context.council_id)
    ):
        raise ValueError("provisional query requires a bound session actor")
    sources = await decision_store.list_provisional_sources(context)
    return {
        "identity_binding": context.identity_binding(),
        "sources": [source.model_dump(mode="json") for source in sources],
    }
