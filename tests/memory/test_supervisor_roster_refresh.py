"""The supervisor converges on the roster without being told.

The roster is desired state. Before this, the only ways in were ``SIGHUP`` and
``POST /api/admin/reconcile``, and nothing in the system sent either one when a
Realm was added — so a Realm created while this process was already running
waited for a human. The 5s loop tick only ever checked whether children were
still alive; it never re-read the roster.

Explicit signals stay, as idempotent accelerators. What they must not be is the
*only* path: a lost signal, or an Admin that exited before sending it, cannot
leave a Realm stranded with no runtime.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.config.users import UsersConfig
from eidolon.memory.entrypoints.supervisor import Supervisor

pytestmark = pytest.mark.asyncio


def _supervisor(tmp_path: Path, *, refresh_seconds: int) -> Supervisor:
    settings = load_memory_settings()
    settings = settings.model_copy(
        update={
            "runtime": settings.runtime.model_copy(
                update={"palaces_root": str(tmp_path / "palaces")}
            ),
            "supervisor": settings.supervisor.model_copy(
                update={"roster_refresh_seconds": refresh_seconds}
            ),
        }
    )
    return Supervisor(settings, eager_init=False)


async def test_default_is_periodic_convergence_not_manual() -> None:
    """A shipped default of 0 would put us back where we started."""
    assert load_memory_settings().supervisor.roster_refresh_seconds > 0


async def test_not_due_immediately_after_a_read(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path, refresh_seconds=60)
    sup._last_roster_read_at = None
    assert sup._roster_refresh_due() is True

    sup._last_roster_read_at = time.monotonic()
    assert sup._roster_refresh_due() is False


async def test_due_once_the_interval_has_passed(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, refresh_seconds=60)
    now = 1_000.0
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.time.monotonic", lambda: now
    )
    sup._last_roster_read_at = now - 59.0
    assert sup._roster_refresh_due() is False
    sup._last_roster_read_at = now - 60.0
    assert sup._roster_refresh_due() is True


async def test_zero_disables_the_periodic_pass(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path, refresh_seconds=0)
    sup._last_roster_read_at = None
    assert sup._roster_refresh_due() is False


async def test_any_reconcile_defers_the_next_periodic_one(
    tmp_path: Path, monkeypatch
) -> None:
    """A SIGHUP or admin call is a read too, so it resets the clock.

    Stamping only on the periodic path would make an admin-driven reconcile and
    the timer fire back to back for no reason.
    """
    sup = _supervisor(tmp_path, refresh_seconds=60)
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.load_users_config",
        lambda _settings: UsersConfig.model_validate({"users": []}),
    )
    assert sup._roster_refresh_due() is True
    await sup.reconcile_now()
    assert sup._roster_refresh_due() is False


async def test_run_loop_reconciles_with_nobody_asking(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end through the loop: no SIGHUP, no admin call, still converges."""
    sup = _supervisor(tmp_path, refresh_seconds=1)
    calls: list[str] = []

    async def _fake_start() -> None:
        calls.append("start")

    async def _fake_reconcile(*, periodic: bool = False) -> None:
        # The loop marks the timer-driven pass, which is the only one that can
        # fire here: no reload event, no init failures.
        calls.append("periodic" if periodic else "reconcile")
        sup._stop_event.set()

    async def _fake_stop() -> None:
        calls.append("stop")

    monkeypatch.setattr(sup, "start", _fake_start)
    monkeypatch.setattr(sup, "_reconcile", _fake_reconcile)
    monkeypatch.setattr(sup, "stop", _fake_stop)
    monkeypatch.setattr(sup, "_check_children", lambda: None)

    # No reload event, no init failures: the only thing that can drive this is
    # the periodic pass.
    assert not sup._reload_event.is_set()
    assert sup._init_retry_due() is False

    await asyncio.wait_for(sup.run(), timeout=5.0)
    assert calls == ["start", "periodic", "stop"]


async def test_a_failing_roster_source_does_not_stop_the_children(
    tmp_path: Path, monkeypatch
) -> None:
    """Convergence must be safe to run often, including while the source is down.

    ``_reconcile`` returns before it diffs when the roster cannot be read, so a
    transient outage is a no-op rather than a mass terminate. Running this pass
    every minute makes that property load-bearing, so it is pinned here.
    """
    sup = _supervisor(tmp_path, refresh_seconds=60)
    sentinel = object()
    sup._children = {"r_a": sentinel}  # type: ignore[dict-item]

    def _explode(_settings):
        raise RuntimeError("roster source is unavailable")

    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.load_users_config", _explode
    )
    await sup._reconcile()

    assert sup._children == {"r_a": sentinel}
    # And the attempt still counts, so a broken source is retried on the
    # interval rather than on every tick of the loop.
    assert sup._roster_refresh_due() is False
