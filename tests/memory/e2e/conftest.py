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
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import httpx
import nats
import pytest
import pytest_asyncio
from eidolon_sdk.memory import (
    MemoryActorContext,
    conversation_turn_subject,
    derive_memory_space_id,
    memory_command_subject,
    validate_memory_space_id,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPORTS_ROOT = _REPO_ROOT / "reports"
_NATS_DATA_FALLBACK = Path.home() / "eidolon" / "data" / "nats-jetstream-e2e"


def _e2e_memory_space_id(value: str) -> str:
    try:
        return validate_memory_space_id(value)
    except ValueError:
        return derive_memory_space_id("default", value, "default")


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


def _nats_port_free(port: int = 4222) -> bool:
    """Return True if a NATS server is NOT listening on ``port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
            return False
        except (ConnectionRefusedError, OSError):
            return True


@pytest.fixture(scope="session")
def live_nats() -> Iterator[str]:
    """Provide a live NATS JetStream broker on ``nats://127.0.0.1:4222``.

    If one is already up (e.g. user's persistent dev server) we re-use it;
    otherwise we ``subprocess.Popen("nats-server -js")`` and tear it down
    at session end. Either way the URL is returned.
    """
    url = "nats://127.0.0.1:4222"
    proc: subprocess.Popen[bytes] | None = None

    if _nats_port_free(4222):
        # No one's listening — spin up our own.
        nats_bin = shutil.which("nats-server")
        if nats_bin is None:
            pytest.skip("nats-server not installed; install via brew install nats-server")
        _NATS_DATA_FALLBACK.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [nats_bin, "-js", "-p", "4222", "-sd", str(_NATS_DATA_FALLBACK)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait for it to bind (up to 10s)
        for _ in range(50):
            if not _nats_port_free(4222):
                break
            time.sleep(0.2)
        else:
            proc.terminate()
            pytest.fail("nats-server failed to bind 4222 after 10s")
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
    """Poll the agent_runner's MCP HTTP until it responds with ANY HTTP status.

    A successful TCP+HTTP round-trip on /mcp/ means uvicorn + FastMCP are up;
    we don't care which status code FastMCP picks for a bare GET.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/mcp/",
                timeout=1.0,
                trust_env=False,
            )
            # Any non-5xx HTTP response means uvicorn + FastMCP are up. A
            # 502/500 means the MCP endpoint is alive but unhealthy; keep
            # polling so startup failures surface with the agent log tail.
            if response.status_code < 500:
                return True
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.TimeoutException):
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


@pytest.fixture
def live_agent_runner(live_nats: str, tmp_path_factory: pytest.TempPathFactory):
    """Factory for spawning isolated agent_runner subprocesses.

    Usage:

        def test_x(live_agent_runner):
            handle = live_agent_runner(user_id="e2e_p0", port=19030)
            assert handle.mcp_url == "http://127.0.0.1:19030/mcp"
            # ... talk to it via mcp_session / nats_publish_turn ...

    Cleanup: every spawned process is SIGTERM'd (then SIGKILL) at fixture
    teardown. Each user_id gets a fresh palace (any pre-existing palace at
    `~/eidolon/memory/mempalaces/<user_id>` is rm -rf'd first).
    """
    handles: list[_AgentHandle] = []

    tmp_settings_dir = tmp_path_factory.mktemp("e2e_settings")
    # Pin the palace root to a dedicated test directory so e2e tests never
    # touch the user's real palaces and the fixture knows exactly where data
    # will land. Each test run gets its own root subdir for isolation.
    palaces_root = tmp_path_factory.mktemp("e2e_palaces")

    def _spawn(*, user_id: str, port: int, steward_mode: str = "noop",
               env_overrides: dict[str, str] | None = None,
               palace_root_override: Path | None = None,
               extra_settings: dict[str, Any] | None = None) -> _AgentHandle:
        memory_space_id = _e2e_memory_space_id(user_id)
        palace_root = palace_root_override or palaces_root
        palace_dir = palace_root / memory_space_id
        if palace_dir.exists():
            shutil.rmtree(palace_dir)
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

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
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
            "runtime": {"palaces_root": str(palace_root)},
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
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            pytest.fail(
                f"agent_runner --memory-space-id {memory_space_id} --port {port} failed to "
                f"bind healthy MCP within 45s.\n"
                f"agent log ({log_path}) tail:\n{tail_file(log_path)}"
            )

        handle = _AgentHandle(
            user_id=memory_space_id,
            port=port,
            mcp_url=f"http://127.0.0.1:{port}/mcp",
            nats_url=live_nats,
            palace_dir=palace_dir,
            log_path=log_path,
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


class _AgentHandle:
    """Handle returned by ``live_agent_runner`` fixture factory."""

    __slots__ = ("user_id", "port", "mcp_url", "nats_url", "palace_dir",
                 "log_path", "process")

    def __init__(
        self,
        *,
        user_id: str,
        port: int,
        mcp_url: str,
        nats_url: str,
        palace_dir: Path,
        log_path: Path,
        process: subprocess.Popen,
    ) -> None:
        self.user_id = user_id
        self.port = port
        self.mcp_url = mcp_url
        self.nats_url = nats_url
        self.palace_dir = palace_dir
        self.log_path = log_path
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
        async with streamablehttp_client(
            url,
            httpx_client_factory=_local_http_client,
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
    """Publish a ConversationTurnPayload to ``eidolon.memory.turn.<memory_space_id>``.

    Returns the turn_id (caller can use it for later poll).
    """
    memory_space_id = _e2e_memory_space_id(user_id)
    tenant_id, owner_user_id, persona_id = memory_space_id.split(".", 2)
    context = MemoryActorContext(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        persona_id=persona_id,
        agent_id="e2e",
        device_id="e2e",
        instance_id="e2e",
        session_id=session_id,
    )
    payload: dict[str, Any] = {
        "turn_id": turn_id or uuid.uuid4().hex,
        "context": context.model_dump(mode="json"),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_text": user_text,
        "assistant_text": assistant_text,
    }
    if metadata is not None:
        payload["metadata"] = metadata
    await _nats_publish(nats_url, conversation_turn_subject(memory_space_id), payload)
    return payload["turn_id"]


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    payload.update({
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "confidence": confidence,
        "adapter_name": adapter_name,
    })
    if valid_from is not None:
        payload["valid_from"] = valid_from
    if valid_to is not None:
        payload["valid_to"] = valid_to
    await _nats_publish(
        nats_url,
        memory_command_subject(str(payload["memory_space_id"])),
        payload,
    )
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
    payload.update({
        "subject": subject,
        "predicate": predicate,
        "object": obj,
    })
    if ended is not None:
        payload["ended"] = ended
    await _nats_publish(
        nats_url,
        memory_command_subject(str(payload["memory_space_id"])),
        payload,
    )
    return str(payload["request_id"])


async def nats_publish_user_confirm(
    nats_url: str,
    *,
    user_id: str,
    text: str,
    wing: str = "Wing_Profile",
    memory_type: str = "preference",
    request_id: str | None = None,
) -> str:
    """Publish a ``UserConfirmedFactCommand`` (Phase 5.2) to the cmd subject.

    This is the exact wire shape the ``eidolon_memory_user_confirm`` MCP tool
    emits — e2e tests publish it directly to verify the worker → drawer →
    recall-pin path end to end.
    """
    payload = _base_cmd(user_id, "user_confirm_fact", request_id)
    payload.update({
        "issuer": "agent",
        "text": text,
        "wing": wing,
        "memory_type": memory_type,
    })
    await _nats_publish(
        nats_url,
        memory_command_subject(str(payload["memory_space_id"])),
        payload,
    )
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
        result = predicate(session)
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(poll_interval_s)
    return False
