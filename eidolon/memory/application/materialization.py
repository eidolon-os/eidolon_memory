"""One truthful materialization status for every Memory consumer."""

from __future__ import annotations

from eidolon_memory_contracts import ServiceStatus

from eidolon.memory.domain.space_runtime import MemorySpaceRuntime


async def inspect_materialization(runtime: MemorySpaceRuntime) -> ServiceStatus:
    """Prove storage readability and ledger projection convergence.

    Process liveness is deliberately absent. A running worker with unreadable
    Chroma data or pending projections is not a ready memory Realm.
    """

    try:
        await runtime.backend.get_all(runtime.space_id, limit=1, offset=0)
    except Exception as exc:  # noqa: BLE001 - status must carry the failure
        return ServiceStatus(
            memory_space_id=runtime.space_id,
            ready=False,
            details={
                "data_readable": False,
                "materialization_state": "unavailable",
                "projection_pending": 0,
                "last_materialized_at": None,
                "degraded_reason": f"{type(exc).__name__}: {exc}",
            },
        )

    ledger = runtime.ledgers.canonical_facts
    if ledger is None:
        return ServiceStatus(
            memory_space_id=runtime.space_id,
            ready=False,
            details={
                "data_readable": True,
                "materialization_state": "degraded",
                "projection_pending": 0,
                "last_materialized_at": None,
                "degraded_reason": "canonical assertion ledger is unavailable",
            },
        )

    try:
        stats = await ledger.stats()
    except Exception as exc:  # noqa: BLE001 - status must carry the failure
        return ServiceStatus(
            memory_space_id=runtime.space_id,
            ready=False,
            details={
                "data_readable": True,
                "materialization_state": "degraded",
                "projection_pending": 0,
                "last_materialized_at": None,
                "degraded_reason": f"fact ledger unreadable: {type(exc).__name__}: {exc}",
            },
        )

    pending = sum(
        (
            stats.reactivations_pending,
            stats.invalidations_pending,
            stats.supersessions_pending,
            stats.forget_projections_pending,
            stats.drawer_not_projected,
            stats.kg_not_projected,
        )
    )

    if runtime.kg is not None:
        try:
            await runtime.kg.stats()
        except Exception as exc:  # noqa: BLE001 - status must carry the failure
            return ServiceStatus(
                memory_space_id=runtime.space_id,
                ready=False,
                details={
                    "data_readable": False,
                    "materialization_state": "unavailable",
                    "projection_pending": pending,
                    "last_materialized_at": stats.last_materialized_at,
                    "degraded_reason": f"knowledge graph unreadable: {type(exc).__name__}: {exc}",
                },
            )
    return ServiceStatus(
        memory_space_id=runtime.space_id,
        ready=pending == 0,
        details={
            "data_readable": True,
            "materialization_state": "ready" if pending == 0 else "materializing",
            "projection_pending": pending,
            "last_materialized_at": stats.last_materialized_at,
            "degraded_reason": (
                "" if pending == 0 else f"{pending} canonical projection(s) pending"
            ),
        },
    )
