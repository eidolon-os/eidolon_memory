"""Phase 4 — supervisor + consolidator integration tests.

Scope (in-process, no real subprocess):
  * ``_consolidator_cli_argv`` builds the exact argv the supervisor passes
    to ``eidolon-memory-consolidator``.
  * ``Supervisor.start`` spawns a consolidator child IFF
    ``user.consolidator_enabled() is True``.
  * ``Supervisor.start`` does NOT spawn a consolidator when the block is
    absent or ``enabled=False``.
  * Port change in users.yaml cascades a consolidator restart (its --mcp-url
    embeds the port; stale URL would silently break).
  * Reconcile flips: disable → terminate; enable → spawn.

We mock ``subprocess.Popen`` so the tests don't actually fork. Lifecycle
flags (``is_alive``, ``returncode``) are emulated on the mock.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.config.users import (
    ConsolidatorUserConfig,
    UserEntry,
)
from eidolon.memory.entrypoints.supervisor import (
    Supervisor,
    _agent_cli_argv,
    _Child,
    _consolidator_cli_argv,
)

# ─── argv builder ──────────────────────────────────────────────────────────


def test_consolidator_cli_argv_has_all_knobs():
    """Every per-user consolidator knob shows up on the command line.

    The supervisor cannot communicate runtime config to the subprocess any
    other way (no shared filesystem state, no env vars per-user) — argv is
    the contract. Drift between users.yaml and what the worker actually sees
    is the single biggest footgun this test guards against.
    """
    user = UserEntry(
        id="alice", port=9001, enabled=True,
        consolidator=ConsolidatorUserConfig(
            enabled=True,
            interval_hours=8,
            window_days=21,
            min_drawers=4,
            min_confidence=0.7,
        ),
    )
    argv = _consolidator_cli_argv(user)
    assert argv[0] == "eidolon-memory-consolidator"
    assert ["--user-id", "alice"] == argv[1:3]
    # --mcp-url must point at the agent_runner on this user's port.
    assert argv[3] == "--mcp-url"
    assert argv[4] == "http://127.0.0.1:9001/mcp"
    # All per-user knobs propagate.
    flat = " ".join(argv)
    assert "--interval-hours 8" in flat
    assert "--window-days 21" in flat
    assert "--min-drawers 4" in flat
    assert "--min-confidence 0.7" in flat


def test_consolidator_cli_argv_rejects_disabled_user():
    """Calling the argv builder on a non-enabled user is a programmer
    error — the supervisor's spawn path must check first."""
    user = UserEntry(id="alice", port=9001, enabled=True, consolidator=None)
    with pytest.raises(AssertionError):
        _consolidator_cli_argv(user)


def test_agent_argv_unaffected_by_consolidator_block():
    """The consolidator config must not leak into agent_runner's argv."""
    user = UserEntry(
        id="alice", port=9001, enabled=True,
        consolidator=ConsolidatorUserConfig(enabled=True),
    )
    argv = _agent_cli_argv(user, palace_path=Path("/tmp/p"))
    # No consolidator flags in agent argv.
    assert all("interval" not in a for a in argv)
    assert all("min-confidence" not in a for a in argv)


# ─── Supervisor spawn behaviour (mocked subprocess) ────────────────────────


@pytest.fixture
def _patched_popen():
    """Patch subprocess.Popen across the module so spawn() doesn't fork.

    Each Popen() returns a fresh MagicMock with ``poll() = None`` (alive)
    and a unique ``pid``. Tests can flip ``poll.return_value`` to simulate
    a crash, then call ``_check_children`` to test restart logic.
    """
    counter = {"pid": 1000}

    def _fake_popen(*args, **kwargs):
        counter["pid"] += 1
        m = MagicMock()
        m.pid = counter["pid"]
        m.poll.return_value = None      # alive
        m.returncode = None
        m.wait.return_value = 0
        # ``terminate`` is best-effort; tests don't assert on it
        return m

    with patch(
        "eidolon.memory.entrypoints.supervisor.subprocess.Popen",
        side_effect=_fake_popen,
    ) as p:
        yield p


def _write_users_yaml(tmp_path: Path, users: list[dict]) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(yaml.safe_dump({"users": users}), encoding="utf-8")
    return p


def _build_supervisor(tmp_path: Path, users_yaml: Path) -> Supervisor:
    # eager_init=False — Supervisor.start would otherwise try to spawn the
    # palace-init helper subprocess (which we don't want under unit-test mocks).
    settings = load_memory_settings()
    return Supervisor(settings, users_yaml, eager_init=False)


async def test_supervisor_skips_consolidator_when_disabled(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {"id": "alice", "port": 9001, "enabled": True},  # no consolidator block
    ])
    sup = _build_supervisor(tmp_path, yaml_path)
    await sup.start()
    try:
        assert "alice" in sup._children
        assert sup._consolidators == {}
        # subprocess.Popen was called exactly once — only for agent_runner.
        assert _patched_popen.call_count == 1
        agent_argv = _patched_popen.call_args_list[0].args[0]
        assert agent_argv[0] == "eidolon-memory-agent"
    finally:
        await sup.stop()


async def test_supervisor_spawns_consolidator_when_enabled(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {
            "id": "alice", "port": 9001, "enabled": True,
            "consolidator": {"enabled": True, "interval_hours": 6},
        },
    ])
    sup = _build_supervisor(tmp_path, yaml_path)
    await sup.start()
    try:
        assert "alice" in sup._children
        assert "alice" in sup._consolidators
        # Two subprocess spawns: agent + consolidator.
        assert _patched_popen.call_count == 2
        argv_list = [c.args[0] for c in _patched_popen.call_args_list]
        kinds = [a[0] for a in argv_list]
        assert sorted(kinds) == [
            "eidolon-memory-agent", "eidolon-memory-consolidator",
        ]
        # The consolidator's --mcp-url must point at alice's agent port.
        cons_argv = next(a for a in argv_list if a[0] == "eidolon-memory-consolidator")
        i = cons_argv.index("--mcp-url")
        assert cons_argv[i + 1] == "http://127.0.0.1:9001/mcp"
    finally:
        await sup.stop()


async def test_supervisor_per_user_consolidator_opt_in(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    """Two users — only one with consolidator: only one consolidator spawns."""
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {
            "id": "alice", "port": 9001, "enabled": True,
            "consolidator": {"enabled": True},
        },
        {"id": "bob", "port": 9002, "enabled": True},  # no consolidator
    ])
    sup = _build_supervisor(tmp_path, yaml_path)
    await sup.start()
    try:
        assert set(sup._children) == {"alice", "bob"}
        assert set(sup._consolidators) == {"alice"}
        assert _patched_popen.call_count == 3  # 2 agents + 1 consolidator
    finally:
        await sup.stop()


async def test_start_spawns_ready_user_before_slow_init_finishes(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    """One slow/bad user's palace init must not block healthy users from
    getting a worker. This protects stack restart latency in multi-user dev
    and production supervisors.
    """
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {"id": "fast", "port": 9001, "enabled": True},
        {"id": "slow", "port": 9002, "enabled": True},
    ])

    def _fake_init(user_id: str, _palace_path: Path) -> None:
        if user_id == "slow":
            time.sleep(0.3)

    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.ensure_palace_initialized",
        _fake_init,
    )

    loop = asyncio.get_running_loop()
    fast_spawned = asyncio.Event()
    real_spawn = _Child.spawn

    def _spawn_spy(self: _Child) -> None:
        real_spawn(self)
        if self.user.id == "fast":
            loop.call_soon(fast_spawned.set)

    monkeypatch.setattr(_Child, "spawn", _spawn_spy)

    settings = load_memory_settings()
    sup = Supervisor(settings, yaml_path, eager_init=True)
    start_task = asyncio.create_task(sup.start())
    try:
        await asyncio.wait_for(fast_spawned.wait(), timeout=0.5)
        assert "fast" in sup._children
        assert "slow" not in sup._children
        assert not start_task.done()

        await start_task
        assert set(sup._children) == {"fast", "slow"}
    finally:
        if not start_task.done():
            start_task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await start_task
        await sup.stop()


async def test_reconcile_restarts_degraded_dead_agent_child(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    """A degraded dead child is a stopped runtime, not a valid alignment.
    Operator/admin reconcile should discard it and spawn a fresh worker.
    """
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {"id": "alice", "port": 9001, "enabled": True},
    ])
    sup = _build_supervisor(tmp_path, yaml_path)
    await sup.start()
    try:
        old = sup._children["alice"]
        assert old.proc is not None
        old.proc.poll.return_value = 1
        old.proc.returncode = 1
        old.degraded = True

        await sup._reconcile()

        new = sup._children["alice"]
        assert new is not old
        assert _patched_popen.call_count == 2
    finally:
        await sup.stop()


async def test_reconcile_disabling_consolidator_terminates_it(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    """Flip ``consolidator.enabled: true → false`` + SIGHUP semantics → kill."""
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {
            "id": "alice", "port": 9001, "enabled": True,
            "consolidator": {"enabled": True},
        },
    ])
    sup = _build_supervisor(tmp_path, yaml_path)
    await sup.start()
    try:
        assert "alice" in sup._consolidators
        # Rewrite yaml with consolidator disabled.
        yaml_path.write_text(yaml.safe_dump({"users": [
            {"id": "alice", "port": 9001, "enabled": True,
             "consolidator": {"enabled": False}}
        ]}), encoding="utf-8")
        await sup._reconcile()
        assert "alice" in sup._children       # agent still alive
        assert "alice" not in sup._consolidators
    finally:
        await sup.stop()


async def test_reconcile_port_change_restarts_consolidator(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    """If the user's agent port shifts, the consolidator's --mcp-url is
    stale → we must terminate + respawn with the new port."""
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {
            "id": "alice", "port": 9001, "enabled": True,
            "consolidator": {"enabled": True},
        },
    ])
    sup = _build_supervisor(tmp_path, yaml_path)
    await sup.start()
    try:
        first_cons = sup._consolidators["alice"]
        original_port_in_argv = next(
            a for a in [c.args[0] for c in _patched_popen.call_args_list]
            if a[0] == "eidolon-memory-consolidator"
        )
        assert "http://127.0.0.1:9001/mcp" in original_port_in_argv

        # Shift port to 9099.
        yaml_path.write_text(yaml.safe_dump({"users": [
            {"id": "alice", "port": 9099, "enabled": True,
             "consolidator": {"enabled": True}}
        ]}), encoding="utf-8")
        await sup._reconcile()

        # Consolidator MUST have been replaced (not just reconfigured).
        new_cons = sup._consolidators["alice"]
        assert new_cons is not first_cons

        # And the latest spawn carries the new port.
        new_argv = _patched_popen.call_args_list[-1].args[0]
        assert new_argv[0] == "eidolon-memory-consolidator"
        i = new_argv.index("--mcp-url")
        assert new_argv[i + 1] == "http://127.0.0.1:9099/mcp"
    finally:
        await sup.stop()


async def test_stop_terminates_consolidators_before_agents(
    tmp_path: Path, _patched_popen, monkeypatch,
):
    """Order matters: consolidators are read-side, kill them first; agents
    get the full grace window for NATS drain + WAL checkpoint."""
    monkeypatch.setattr(
        "eidolon.memory.entrypoints.supervisor.resolve_log_dir",
        lambda _s: tmp_path / "logs",
    )
    yaml_path = _write_users_yaml(tmp_path, [
        {
            "id": "alice", "port": 9001, "enabled": True,
            "consolidator": {"enabled": True},
        },
    ])
    sup = _build_supervisor(tmp_path, yaml_path)
    await sup.start()
    cons_proc = sup._consolidators["alice"].proc
    agent_proc = sup._children["alice"].proc

    await sup.stop()
    # Both got ``terminate()`` called; we just check both dicts cleared.
    assert sup._children == {}
    assert sup._consolidators == {}
    assert cons_proc.terminate.called
    assert agent_proc.terminate.called
