"""Shared e2e fixtures.

Contract: tests under this directory **must** drive the memory service
through MCP (read) and NATS (write) only. No in-process shortcuts.
``live_agent_runner`` ensures the agent is a separate subprocess with its
own palace, so any state-sharing accidentally falls back to going through
the real wire.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import nats
import pytest
import pytest_asyncio
from eidolon_memory_contracts import (
    ConversationTurnPayload,
    build_memory_actor_context,
    conversation_turn_subject,
    derive_memory_space_id,
    envelope_memory_payload,
    memory_command_subject,
    memory_space_storage_name,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPORTS_ROOT = _REPO_ROOT / "reports"


def _e2e_memory_space_id(value: str) -> str:
    # A memory realm id is opaque to the SDK now (``memory_space_id ==
    # memory_realm_id``); ``derive_memory_space_id`` simply validates it. The
    # old ``<tenant>.<owner>.<persona>`` decomposition is gone.
    return derive_memory_space_id(value)


def tail_file(path: Path, *, max_chars: int = 4000) -> str:
    """Return a readable tail for pytest failure messages."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<could not read {path}: {exc}>"
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


# ─── nats-server lifecycle ─────────────────────────────────────────────────


def _nats_port_free(port: int) -> bool:
    """Return True if a NATS server is NOT listening on ``port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
            return False
        except (ConnectionRefusedError, OSError):
            return True


def _unused_tcp_port() -> int:
    """Ask the kernel for a loopback port for this isolated E2E session."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def live_nats(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Provide an isolated NATS JetStream broker for this test session.

    Reusing a developer's port 4222 and persistent JetStream directory made
    the E2E suite mutate external consumer state and fail when a stale store
    could not be reopened.  Every run now gets its own port, data directory,
    and startup log.  An explicitly supplied ``EIDOLON_MEMORY_E2E_NATS_URL``
    remains available for CI environments that manage the broker themselves.
    """
    external_url = os.environ.get("EIDOLON_MEMORY_E2E_NATS_URL", "").strip()
    if external_url:
        yield external_url
        return

    port = _unused_tcp_port()
    url = f"nats://127.0.0.1:{port}"
    proc: subprocess.Popen[bytes] | None = None
    nats_bin = shutil.which("nats-server")
    if nats_bin is None:
        pytest.skip("nats-server not installed; install via brew install nats-server")
    state_dir = tmp_path_factory.mktemp("e2e_nats")
    log_path = state_dir / "nats-server.log"
    with log_path.open("ab") as log_fp:
        proc = subprocess.Popen(
            [nats_bin, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(state_dir)],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    for _ in range(50):
        if not _nats_port_free(port):
            break
        if proc.poll() is not None:
            pytest.fail(
                f"nats-server exited with {proc.returncode} before binding {port}.\n"
                f"log tail:\n{tail_file(log_path)}"
            )
        time.sleep(0.2)
    else:
        if proc.poll() is None:
            proc.terminate()
        pytest.fail(
            f"nats-server failed to bind {port} after 10s.\nlog tail:\n{tail_file(log_path)}"
        )
    try:
        yield url
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ─── agent_runner lifecycle ────────────────────────────────────────────────


def _wait_mcp_ready(port: int, *, timeout_s: float = 45.0) -> bool:
    """Poll both MCP surfaces until each responds with ANY non-5xx HTTP status.

    A successful TCP+HTTP round-trip means uvicorn + FastMCP are up; we don't care
    which status code FastMCP picks for a bare GET.

    **Both** paths, not just the agent's. Nearly every e2e test drives operator
    tools, which live on ``/ops/mcp``; checking only ``/mcp`` would report a healthy
    start for a process whose operator surface failed to mount, and every one of
    those tests would then fail on a 404 that says nothing about why.
    """
    deadline = time.monotonic() + timeout_s
    paths = ("/mcp/", "/ops/mcp/")
    while time.monotonic() < deadline:
        try:
            responses = [
                httpx.get(f"http://127.0.0.1:{port}{path}", timeout=1.0, trust_env=False)
                for path in paths
            ]
            # Any non-5xx HTTP response means uvicorn + FastMCP are up. A
            # 502/500 means the MCP endpoint is alive but unhealthy; keep
            # polling so startup failures surface with the agent log tail.
            if all(response.status_code < 500 for response in responses):
                return True
        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
        ):
            pass
        time.sleep(0.5)
    return False


async def _delete_e2e_durables(nats_url: str, user_id: str) -> None:
    """Reset the JetStream state for ``memory_space_id`` so the next spawn starts
    from a virgin subscription with no stale messages.

    Three steps, each best-effort:

      1. Delete the durable turn + cmd consumers (prior spawn's pending /
         in-flight messages are wiped along with the consumer).
      2. Purge any stream messages whose subject is the user's turn /
         cmd subject — without this a fresh durable with
         ``DeliverAllPolicy`` would re-deliver every leftover message
         from prior test runs.

    Failures are swallowed: on the very first spawn the durable / messages
    simply don't exist yet, and we don't want the fixture to flake.
    """
    try:
        nc = await nats.connect(nats_url)
    except Exception:
        return
    try:
        js = nc.jetstream()
        # 1) drop durables
        for suffix in ("", "-cmd"):
            name = f"eidolon-memory-agent{suffix}-{user_id}"
            try:
                await js.delete_consumer("MEMORY_TURNS", name)
            except Exception:
                pass
        # 2) purge stream messages on this user's subjects
        for subject in (
            conversation_turn_subject(user_id),
            memory_command_subject(user_id),
        ):
            try:
                await js.purge_stream("MEMORY_TURNS", subject=subject)
            except Exception:
                pass
    finally:
        await nc.close()


#: Ports this session has already handed out. The kernel does not immediately
#: reuse a port it just released, but it is under no obligation not to, and a
#: suite that spawns thirty agents should not be relying on that.
_ISSUED_PORTS: set[int] = set()

#: Deliberately below the ephemeral range. Asking the kernel for a port with
#: bind(0) returns one from 49152-65535 on macOS, which is exactly the range it
#: draws from for outgoing connections — so between our close() and the child's
#: bind(), any outbound socket in the suite can take it. That is not
#: theoretical: it produced an agent that never bound and never logged, which
#: reads identically to a slow start.
_PORT_BAND = (20000, 29999)


def _free_port() -> int:
    """A port nobody is listening on, verified rather than assumed.

    The e2e tests used to name their own: thirty-three literals for thirty
    distinct values, so three pairs shared one. Two agents that share a port
    only collide when both run — never when you run the file on its own,
    sometimes when you run the suite, which is the shape of a failure nobody
    can reproduce.

    Chosen from a band the kernel will not hand to an outbound connection, and
    proved free by binding it. Between that bind and the child's there is still
    a window, but nothing else on this machine is being assigned ports here, so
    the only thing that could take it is another agent — and those are tracked.
    """

    for _ in range(200):
        candidate = random.randint(*_PORT_BAND)
        if candidate in _ISSUED_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        _ISSUED_PORTS.add(candidate)
        return candidate
    raise RuntimeError(
        f"no free port in {_PORT_BAND[0]}-{_PORT_BAND[1]} for an e2e agent"
    )


@pytest.fixture
def live_agent_runner(live_nats: str, tmp_path_factory: pytest.TempPathFactory):
    """Factory for spawning isolated agent_runner subprocesses.

    Usage:

        def test_x(live_agent_runner):
            handle = live_agent_runner(user_id="e2e_p0")
            # The port is the fixture's; ask the handle where it went.
            async with mcp_session(handle.mcp_url) as session:
                ...

    Cleanup: every spawned process is SIGTERM'd (then SIGKILL) at fixture
    teardown. Each user_id gets a fresh palace (any pre-existing palace at
    `~/eidolon/memory/mempalaces/<user_id>` is rm -rf'd first).
    """
    handles: list[_AgentHandle] = []

    tmp_settings_dir = tmp_path_factory.mktemp("e2e_settings")
    # Pin the palace root to a dedicated test directory so e2e tests never
    # touch the user's real palaces and the fixture knows exactly where data
    # will land. Each test run gets its own root subdir for isolation.
    # Chroma's Rust compactor reproducibly raises SQLITE_IOERR_SHORT_READ
    # (extended code 522) around the 17th write when a Palace lives under
    # macOS's per-user /private/var/folders pytest temp root.  The identical
    # 40-turn, two-Realm scenario is stable under /private/tmp.  Keep storage
    # tests on an explicitly local, non-synced root while leaving callers an
    # override for CI hosts.
    safe_tmp_root = Path(os.environ.get("EIDOLON_MEMORY_E2E_TMPDIR", "/private/tmp")).expanduser()
    safe_tmp_root.mkdir(parents=True, exist_ok=True)
    palaces_root = Path(tempfile.mkdtemp(prefix="eidolon-memory-e2e-palaces-", dir=safe_tmp_root))
    # Spawned runners claim each space with an advisory lock under run_dir. Left
    # at its default that is the developer's real ~/eidolon/run, shared with any
    # production runner on the machine — so a test space that happened to share a
    # name with a live one would fight it for the claim. Give the run its own.
    run_dir = palaces_root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    def _spawn(
        *,
        user_id: str,
        steward_mode: str = "noop",
        env_overrides: dict[str, str] | None = None,
        palace_root_override: Path | None = None,
        extra_settings: dict[str, Any] | None = None,
        keep_palace: bool = False,
    ) -> _AgentHandle:
        memory_space_id = _e2e_memory_space_id(user_id)
        # Not a parameter. Where an agent listens is a fact about running two
        # processes on one machine, not something a test has an opinion about,
        # and every caller that named one was naming it wrong eventually.
        port = _free_port()
        palace_root = palace_root_override or palaces_root
        # The agent_runner stores each palace at
        # ``<palaces_root>/<memory_space_storage_name(id)>`` (a reversible
        # ``b64_...`` encoding, see eidolon.memory.config.palace_directory),
        # NOT under the raw memory_space_id. Mirror that here so ``palace_dir``
        # points at the real on-disk palace — otherwise the wipe is a no-op and
        # tests that read the palace (restart handoff copytree, KG sqlite
        # probes) hit a non-existent path.
        palace_dir = palace_root / memory_space_storage_name(memory_space_id)
        # A fresh palace per spawn, unless the test's subject is data that
        # outlived a process: durability across restart, or a realm restored
        # from a copy. Those cannot be written as a spawn with a different
        # ``user_id`` — the palace directory is named after the space id, and
        # copying files between two of them is a test of copytree.
        if palace_dir.exists() and not keep_palace:
            shutil.rmtree(palace_dir)
        # Keep SQLite/Chroma temporary files isolated per agent process as
        # well as the persistent Palace itself.  On macOS, leaving child
        # processes on pytest's shared /private/var/folders TMPDIR reproduces
        # SQLITE_IOERR_SHORT_READ (522) during Chroma log compaction even
        # when the Palace is under /private/tmp.  A per-Realm directory makes
        # this dependency explicit and prevents two A/B processes from
        # sharing the same temporary-file namespace.
        process_tmp_dir = palace_root / ".process-tmp" / memory_space_storage_name(memory_space_id)
        if process_tmp_dir.exists():
            shutil.rmtree(process_tmp_dir)
        process_tmp_dir.mkdir(parents=True, exist_ok=True)
        # Wipe any stale JetStream state for this user — prior crashes or
        # aborted runs leave pending messages that would otherwise bleed
        # into this spawn's pull subscription. ``_spawn`` is sync but the
        # cleanup is async; in pytest-asyncio context there's a running
        # loop, so use ``run_until_complete`` via a fresh helper loop to
        # avoid "asyncio.run() cannot be called from a running event loop".
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside the test's loop — spin a dedicated thread.
                    import threading

                    t = threading.Thread(
                        target=lambda: asyncio.new_event_loop().run_until_complete(
                            _delete_e2e_durables(live_nats, memory_space_id)
                        )
                    )
                    t.start()
                    t.join(timeout=10)
                else:
                    loop.run_until_complete(_delete_e2e_durables(live_nats, memory_space_id))
            except RuntimeError:
                asyncio.run(_delete_e2e_durables(live_nats, memory_space_id))
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        log_dir = _REPORTS_ROOT / f"e2e_{memory_space_id}_{ts}"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "agent_runner.log"

        venv_bin = Path(sys.executable).parent
        agent_cli = venv_bin / "eidolon-memory-agent"
        if not agent_cli.is_file():
            agent_cli_str = shutil.which("eidolon-memory-agent")
            if agent_cli_str is None:
                pytest.fail("eidolon-memory-agent not on PATH or in venv")
            agent_cli = Path(agent_cli_str)

        # Write a per-spawn settings yaml so tests can override steward.mode
        # without touching the user's local config/settings.yaml.
        import yaml

        settings_path = tmp_settings_dir / f"{memory_space_id}.yaml"
        settings_doc: dict[str, Any] = {
            "steward": {"mode": steward_mode},
            "mcp_http": {"host": "127.0.0.1", "port": port},
            "mempalace": {"embedding_threads": 1},
            "nats": {"url": live_nats},
            "runtime": {"palaces_root": str(palace_root), "run_dir": str(run_dir)},
        }
        # If the test runs in LLM-steward mode, inherit the project's LLM
        # config from config/settings.yaml. Otherwise the LiteLLM steward
        # silently falls back to rules (no model/api_base), and the test
        # would assert against degraded behaviour. Inheritance keeps the
        # test source minimal: tests don't have to know the model name.
        if steward_mode == "llm":
            project_settings = _REPO_ROOT / "config" / "settings.yaml"
            if project_settings.is_file():
                with project_settings.open("r", encoding="utf-8") as fh:
                    parent = yaml.safe_load(fh) or {}
                if isinstance(parent, dict) and "llm" in parent:
                    settings_doc["llm"] = parent["llm"]
        if extra_settings:
            # Deep merge so callers can drop in nested overrides like
            # ``{"recall": {"rerank_enabled": False}}`` without clobbering the
            # baseline keys above.
            def _deep_merge(dst: dict, src: dict) -> None:
                for k, v in src.items():
                    if isinstance(v, dict) and isinstance(dst.get(k), dict):
                        _deep_merge(dst[k], v)
                    else:
                        dst[k] = v

            _deep_merge(settings_doc, extra_settings)
        settings_path.write_text(
            yaml.safe_dump(settings_doc, allow_unicode=True),
            encoding="utf-8",
        )

        env = {**os.environ}
        env["EIDOLON_MEMORY_SETTINGS_YAML"] = str(settings_path)
        # Set explicitly rather than relying on the settings file: run_dir
        # resolution reads the environment first, so an exported value in the
        # developer's shell would otherwise put test claims back in the shared
        # directory.
        env["EIDOLON_MEMORY_RUN_DIR"] = str(run_dir)
        # Activate isolation in the parent before the child imports Chroma's
        # native modules. Agent startup repeats this configuration as a guard.
        env["EIDOLON_MEMORY_PROCESS_TMP_ROOT"] = str(process_tmp_dir.parent)
        env["TMPDIR"] = str(process_tmp_dir)
        env["SQLITE_TMPDIR"] = str(process_tmp_dir)
        # Forward LLM secret from config/.env if not already exported, so the
        # llm-mode tests have credentials without each test having to call
        # ``dotenv.load_dotenv`` manually.
        if "EIDOLON_MEMORY_LLM_API_KEY" not in env or not env["EIDOLON_MEMORY_LLM_API_KEY"]:
            dotenv = _REPO_ROOT / "config" / ".env"
            if dotenv.is_file():
                for line in dotenv.read_text().splitlines():
                    if line.startswith("EIDOLON_") and "=" in line:
                        k, v = line.split("=", 1)
                        if v.strip() and k not in env:
                            env[k] = v.strip()
        if env_overrides:
            env.update(env_overrides)

        with log_path.open("ab") as log_fp:
            proc = subprocess.Popen(
                [str(agent_cli), "--memory-space-id", memory_space_id, "--port", str(port)],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        if not _wait_mcp_ready(port):
            # Ask before killing: a process that exited has told us something,
            # and a process still running has told us something else. Reporting
            # only "did not bind" with an empty log leaves those two — a child
            # that died on startup and a child that is merely slow — looking
            # exactly alike, which is the whole reason this was unexplainable.
            exited = proc.poll()
            alive = "still running" if exited is None else f"exited with {exited}"
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_tail = tail_file(log_path) or "(the agent wrote nothing at all)"
            pytest.fail(
                f"agent_runner --memory-space-id {memory_space_id} --port {port} failed to "
                f"bind healthy MCP within 45s; the process was {alive}.\n"
                f"agent log ({log_path}) tail:\n{log_tail}"
            )

        handle = _AgentHandle(
            user_id=memory_space_id,
            port=port,
            mcp_url=f"http://127.0.0.1:{port}/ops/mcp",
            agent_mcp_url=f"http://127.0.0.1:{port}/mcp",
            nats_url=live_nats,
            palace_dir=palace_dir,
            log_path=log_path,
            settings_path=settings_path,
            process=proc,
        )
        handles.append(handle)
        return handle

    yield _spawn

    # Tear down every spawned process.
    for h in handles:
        if h.process.poll() is None:
            h.process.terminate()
            try:
                h.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                h.process.kill()
                try:
                    h.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
    shutil.rmtree(palaces_root, ignore_errors=True)


class _AgentHandle:
    """Handle returned by ``live_agent_runner`` fixture factory."""

    __slots__ = (
        "user_id",
        "port",
        "mcp_url",
        "agent_mcp_url",
        "nats_url",
        "palace_dir",
        "log_path",
        "settings_path",
        "process",
    )

    def __init__(
        self,
        *,
        user_id: str,
        port: int,
        mcp_url: str,
        agent_mcp_url: str,
        nats_url: str,
        palace_dir: Path,
        log_path: Path,
        settings_path: Path,
        process: subprocess.Popen,
    ) -> None:
        self.user_id = user_id
        self.port = port
        # The operator surface. e2e drives nearly every operator tool, and this is
        # the path that offers them; ``agent_mcp_url`` is the two-tool surface the
        # conversational agent actually connects to. Same process, same service,
        # same handles — only the tool list differs.
        self.mcp_url = mcp_url
        self.agent_mcp_url = agent_mcp_url
        self.nats_url = nats_url
        self.palace_dir = palace_dir
        self.log_path = log_path
        self.settings_path = settings_path
        self.process = process

    def kill(self) -> None:
        """Force-stop this agent_runner (test may do this to exercise restart)."""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()


# ─── MCP session helper ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def mcp_session():
    """Async context manager factory for MCP sessions.

    Usage:

        async with mcp_session(handle.mcp_url) as session:
            result = await session.call_tool("eidolon_memory_status", {})
    """
    from contextlib import asynccontextmanager

    def _local_http_client(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            trust_env=False,
        )

    @asynccontextmanager
    async def _open(url: str) -> AsyncIterator[ClientSession]:
        async with _local_http_client() as http_client:
            async with streamable_http_client(
                url,
                http_client=http_client,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    yield _open


# ─── NATS publish helpers ──────────────────────────────────────────────────


async def _nats_publish(nats_url: str, subject: str, payload: dict[str, Any]) -> None:
    """Single-shot JetStream publish + close. All helpers funnel through here
    so the connect/close discipline is in one place.
    """
    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()
        await js.publish(subject, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    finally:
        await nc.close()


async def _publish_command(nats_url: str, payload: dict[str, Any]) -> None:
    """Envelope a ``MemoryCommandPayload`` dict and publish to its cmd subject.

    The command consumer (``parse_memory_command``) only accepts the versioned
    memory envelope, so every command — like turns — must be wrapped before it
    hits JetStream. Kind is derived from the payload's own ``kind`` field.
    """
    envelope = envelope_memory_payload(payload, trace_id=str(payload["request_id"]))
    await _nats_publish(
        nats_url,
        memory_command_subject(str(payload["memory_space_id"])),
        envelope.model_dump(mode="json"),
    )


async def nats_publish_turn(
    nats_url: str,
    *,
    user_id: str,
    user_text: str,
    assistant_text: str = "",
    turn_id: str | None = None,
    session_id: str = "e2e",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Publish a ConversationTurnPayload to ``eidolon.memory.turn.<space_token>``.

    Mirrors the real producer (``eidolon_agent`` MemoryNatsPublisher.publish_turn):
    identity is expressed via ``owner_id / companion_id / memory_realm_id`` on
    ``MemoryActorContext`` (``memory_realm_id == memory_space_id``), and the
    payload is wrapped in the versioned memory envelope — the consumer's
    ``parse_conversation_turn`` rejects anything that isn't enveloped.

    Returns the turn_id (caller can use it for later poll).
    """
    memory_space_id = _e2e_memory_space_id(user_id)
    context = build_memory_actor_context(
        memory_realm_id=memory_space_id,
        owner_id="e2e",
        companion_id="e2e",
        device_id="e2e",
        session_id=session_id,
    )
    turn = ConversationTurnPayload(
        turn_id=turn_id or uuid.uuid4().hex,
        context=context,
        timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        user_text=user_text,
        assistant_text=assistant_text,
        metadata=metadata if metadata is not None else {},
    )
    envelope = envelope_memory_payload(turn, trace_id=turn.turn_id)
    await _nats_publish(
        nats_url,
        conversation_turn_subject(memory_space_id),
        envelope.model_dump(mode="json"),
    )
    return turn.turn_id


def e2e_actor_context(
    memory_space_id: str,
    *,
    session_id: str = "e2e",
    device_id: str = "e2e",
    owner_id: str = "e2e",
    companion_id: str = "e2e",
) -> dict[str, Any]:
    """Build the ``MemoryActorContext`` dict the recall/search MCP tools now
    require, mirroring the identity ``nats_publish_turn`` attributes writes to.

    Read-side context MUST match the write side or scoped fragments won't be
    recalled. Concretely (see ``RecallPolicyRegistry`` +
    ``recall_with_kg_fusion``):

      - ``memory_space_id`` is the hard visibility filter — pass ``handle.user_id``
        (which *is* the derived memory_space_id) so it equals the write side.
      - ``device_id`` gates ``visibility="current_device"`` records and, together
        with ``session_id``, scopes the working-memory ring snapshot.
      - ``session_id`` also drives the same-session ranking boost.
      - ``owner_id`` / ``companion_id`` are not filtered on today, but are kept
        identical to the write side for wire parity (and future-proofing).

    ``memory_space_id`` is accepted as ``memory_realm_id`` (they're equal;
    ``derive_memory_space_id`` is idempotent on an already-derived id).
    """
    return build_memory_actor_context(
        memory_realm_id=memory_space_id,
        owner_id=owner_id,
        companion_id=companion_id,
        device_id=device_id,
        session_id=session_id,
    ).model_dump(mode="json")


def _now_iso_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base_cmd(user_id: str, kind: str, request_id: str | None) -> dict[str, Any]:
    """Shared scaffolding for ``MemoryCommandPayload`` JSON bodies."""
    memory_space_id = _e2e_memory_space_id(user_id)
    return {
        "kind": kind,
        "request_id": request_id or uuid.uuid4().hex,
        "memory_space_id": memory_space_id,
        "issued_at": _now_iso_z(),
        "issuer": "admin",
    }


async def nats_publish_kg_add_triple(
    nats_url: str,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float = 1.0,
    valid_from: str | None = None,
    valid_to: str | None = None,
    adapter_name: str = "e2e",
    request_id: str | None = None,
) -> str:
    """Publish a ``KgAddTripleCommand`` to ``eidolon.memory.cmd.<memory_space_id>``.

    Mirrors the admin / agent KG-write channel — the only durable way to add
    a triple outside the steward (without bypassing JetStream, which would
    violate the NATS-write contract).
    """
    payload = _base_cmd(user_id, "kg_add_triple", request_id)
    payload.update(
        {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "confidence": confidence,
            "adapter_name": adapter_name,
        }
    )
    if valid_from is not None:
        payload["valid_from"] = valid_from
    if valid_to is not None:
        payload["valid_to"] = valid_to
    await _publish_command(nats_url, payload)
    return str(payload["request_id"])


async def nats_publish_kg_invalidate(
    nats_url: str,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    obj: str,
    ended: str | None = None,
    request_id: str | None = None,
) -> str:
    """Publish a ``KgInvalidateCommand`` to ``eidolon.memory.cmd.<memory_space_id>``.

    Mirrors the admin path that closes (``valid_to`` set) a triple — the
    same cmd subject as adds, dispatched on ``kind="kg_invalidate"``.
    """
    payload = _base_cmd(user_id, "kg_invalidate", request_id)
    payload.update(
        {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
        }
    )
    if ended is not None:
        payload["ended"] = ended
    await _publish_command(nats_url, payload)
    return str(payload["request_id"])


async def nats_publish_user_confirm(
    nats_url: str,
    *,
    user_id: str,
    text: str,
    wing: str = "Wing_Profile",
    memory_type: str = "preference",
    request_id: str | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
    operation_hint: str = "confirm",
    companion_id: str = "e2e",
) -> str:
    """Publish an explicit ``MemoryIntentCommand`` to the cmd subject.

    This is the exact wire shape the ``eidolon_memory_user_confirm`` MCP tool
    emits — e2e tests publish it directly to verify the worker → drawer →
    recall-pin path end to end.
    """
    payload = _base_cmd(user_id, "memory_intent", request_id)
    event_id = f"e2e:{payload['request_id']}"
    intent_type = "preference" if memory_type == "preference" else "fact"
    payload.update(
        {
            "issuer": "agent",
            "intent": {
                "intent_id": f"intent:{payload['request_id']}",
                "memory_space_id": payload["memory_space_id"],
                "source_event_id": event_id,
                "authority": "explicit_user",
                "intent_type": intent_type,
                "raw_claim": text,
                "operation_hint": operation_hint,
                "confidence": 0.99,
                "attributes": {
                    "wing": wing,
                    "memory_type": memory_type,
                    "importance": 5,
                    "source_instance_id": companion_id,
                },
            },
        }
    )
    structured = (subject, predicate, object_value)
    if any(structured):
        if not all(structured):
            raise ValueError("structured confirmation requires subject/predicate/object")
        payload["intent"].update(
            {
                "subject": subject,
                "predicate": predicate,
                "object": object_value,
            }
        )
    await _publish_command(nats_url, payload)
    return str(payload["request_id"])


async def nats_publish_exact_correction(
    nats_url: str,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    object_value: str,
    text: str,
    request_id: str | None = None,
    companion_id: str = "e2e",
) -> str:
    """Publish a business-level exact correction through ``MemoryIntent``."""
    payload = _base_cmd(user_id, "memory_intent", request_id)
    payload.update(
        {
            "issuer": "agent",
            "intent": {
                "intent_id": f"intent:{payload['request_id']}",
                "memory_space_id": payload["memory_space_id"],
                "source_event_id": f"e2e:{payload['request_id']}",
                "authority": "explicit_user",
                "intent_type": "correction",
                "raw_claim": text,
                "operation_hint": "invalidate",
                "subject": subject,
                "predicate": predicate,
                "object": object_value,
                "occurred_at": payload["issued_at"],
                "confidence": 1.0,
                "attributes": {"source_instance_id": companion_id},
            },
        }
    )
    await _publish_command(nats_url, payload)
    return str(payload["request_id"])


async def nats_publish_commitment(
    nats_url: str,
    *,
    user_id: str,
    subject: str,
    action: str,
    text: str,
    operation_hint: str,
    request_id: str,
    target_id: str | None = None,
    status: str | None = None,
    beneficiaries: list[str] | None = None,
    participants: list[str] | None = None,
    due_at: str | None = None,
    companion_id: str = "e2e",
) -> str:
    """Publish one explicit structured commitment revision."""
    payload = _base_cmd(user_id, "memory_intent", request_id)
    attributes: dict[str, Any] = {"source_instance_id": companion_id}
    if beneficiaries is not None:
        attributes["beneficiaries"] = beneficiaries
    if participants is not None:
        attributes["participants"] = participants
    if due_at is not None:
        attributes["due_at"] = due_at
    if status is not None:
        attributes["status"] = status
    intent: dict[str, Any] = {
        "intent_id": f"intent:{payload['request_id']}",
        "memory_space_id": payload["memory_space_id"],
        "source_event_id": f"e2e:{payload['request_id']}",
        "authority": "explicit_user",
        "intent_type": "commitment",
        "raw_claim": text,
        "operation_hint": operation_hint,
        "subject": subject,
        "predicate": "promised",
        "object": action,
        "occurred_at": payload["issued_at"],
        "confidence": 1.0,
        "attributes": attributes,
    }
    if target_id is not None:
        intent["target_id"] = target_id
    payload.update({"issuer": "agent", "intent": intent})
    await _publish_command(nats_url, payload)
    return str(payload["request_id"])


# ─── MCP tool result unwrapping ────────────────────────────────────────────


def mcp_tool_json(result: Any) -> Any:
    """Unwrap a FastMCP ``CallToolResult`` into native Python.

    FastMCP returns the JSON-serialised body inside ``result.content[0].text``
    and *sometimes* wraps it as ``{"result": ...}``. Centralise the dance so
    no test has to remember the dual shape.

    Returns ``None`` if the payload is missing or not parseable.
    """
    if not getattr(result, "content", None):
        return None
    text = getattr(result.content[0], "text", "") or ""
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


# ─── Corpus loader ─────────────────────────────────────────────────────────


def load_companion_corpus() -> list[dict[str, Any]]:
    """Load the 40-turn labeled corpus(`tests/memory/e2e/fixtures/companion_corpus.jsonl`)."""
    path = _FIXTURES_DIR / "companion_corpus.jsonl"
    if not path.is_file():
        pytest.fail(f"corpus missing at {path}")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


# ─── Polling helper ────────────────────────────────────────────────────────


async def wait_for_visible(
    session: ClientSession,
    *,
    predicate,
    timeout_s: float = 60.0,
    poll_interval_s: float = 0.5,
    predicate_timeout_s: float = 10.0,
) -> bool:
    """Poll the MCP session via ``predicate(session) -> bool/awaitable[bool]``
    until it returns truthy or timeout.

    The predicate is the test's choice — typical patterns:
    - "fragment count >= N":  ``await s.call_tool("eidolon_memory_list", {"limit":1000})``
                              then check ``len(payload['records']) >= N``
    - "specific drawer present": same call then string match
    """
    import inspect

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            result = predicate(session)
            if inspect.isawaitable(result):
                remaining = max(0.01, deadline - time.monotonic())
                result = await asyncio.wait_for(
                    result,
                    timeout=min(max(0.01, predicate_timeout_s), remaining),
                )
        except TimeoutError:
            result = False
        if result:
            return True
        await asyncio.sleep(poll_interval_s)
    return False
