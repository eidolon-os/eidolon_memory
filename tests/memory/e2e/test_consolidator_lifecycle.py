"""Phase 4 e2e — consolidator subprocess end-to-end.

End-to-end edges:

    NATS turn publish (40 corpus turns) → agent_runner LLM steward → drawers
       ↑ chat layer (existing Phase 0-3 plumbing)

    Subprocess (separate from agent_runner):
       eidolon-memory-consolidator --user-id X --once
         → NATS query agent_runner (read drawers)
         → LLM (theme synthesis per wing)
         → NATS publish ConsolidatorIngestThemeCommand × N
       ↓ agent_runner cmd subscriber
       → process_command_message dispatches to _ingest_theme
       → backend.ingest_fragment writes Wing_Theme drawer
       → MCP recall_context renders [主题] section

Idempotency check: a second ``--once`` run on the same palace produces
the same theme set (same drawers → same hash → same drawer ids → chroma
overwrites in place, no growth).

Markers: ``@pytest.mark.e2e`` + ``@pytest.mark.llm``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.memory.e2e.conftest import (
    load_companion_corpus,
    mcp_tool_json,
    nats_publish_turn,
    tail_file,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.llm]


@pytest.fixture
def _require_llm():
    """Skip if no LLM is reachable.

    Two-tier gate:
      1. ``EIDOLON_MEMORY_LLM_API_KEY`` must be set (forward from config/.env
         when absent in the shell).
      2. The configured ``base_url`` must respond to a real completion call.
         Local dev sometimes leaves a stale 8180 placeholder where no LLM
         is listening; we'd rather skip than burn time on guaranteed 404s.
    """
    if not os.environ.get("EIDOLON_MEMORY_LLM_API_KEY", "").strip():
        env_path = Path(__file__).resolve().parents[3] / "config" / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("EIDOLON_MEMORY_LLM_API_KEY=") and "=" in line:
                    _, val = line.split("=", 1)
                    if val.strip():
                        os.environ["EIDOLON_MEMORY_LLM_API_KEY"] = val.strip()
                        break
        if not os.environ.get("EIDOLON_MEMORY_LLM_API_KEY", "").strip():
            pytest.skip("EIDOLON_MEMORY_LLM_API_KEY not set; LLM e2e gated")

    # Probe the actual endpoint so the test fails fast (skip) rather than
    # spending 90s talking to a dead endpoint. Run on a fresh loop so we
    # don't fight pytest-asyncio's outer loop.
    from eidolon.memory.config.memory_settings import (
        load_memory_settings, reset_memory_settings_cache,
    )
    import litellm
    reset_memory_settings_cache()
    settings = load_memory_settings()
    probe_loop = asyncio.new_event_loop()
    try:
        try:
            probe_loop.run_until_complete(
                litellm.acompletion(
                    model=settings.llm.model,
                    api_base=settings.llm.base_url or None,
                    api_key=settings.llm.resolve_api_key(),
                    messages=[{"role": "user", "content": "ok"}],
                    max_tokens=4,
                    timeout=8,
                )
            )
        except Exception as exc:
            pytest.skip(
                f"LLM endpoint {settings.llm.base_url} unreachable "
                f"({type(exc).__name__}); skip Phase 4 LLM e2e"
            )
    finally:
        probe_loop.close()


async def _wing_theme_count(session) -> int:
    """Count Wing_Theme drawers via MCP list."""
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_list", {"limit": 1000, "include_private": False},
        )
    )
    if not isinstance(payload, dict):
        return 0
    rows = payload.get("records") or []
    return sum(
        1 for r in rows
        if isinstance(r, dict)
        and (r.get("metadata") or {}).get("wing") == "Wing_Theme"
    )


def _run_consolidator(
    *,
    user_id: str,
    settings_yaml: Path,
    log_path: Path,
    timeout_s: float = 180,
) -> subprocess.CompletedProcess:
    """Run the console-script subprocess. Inherits project LLM config via
    ``EIDOLON_MEMORY_SETTINGS_YAML`` + ``config/.env`` forwarding."""
    cli = Path(sys.executable).parent / "eidolon-memory-consolidator"
    env = {**os.environ, "EIDOLON_MEMORY_SETTINGS_YAML": str(settings_yaml)}
    # Forward LLM secret from .env into the subprocess env if not exported.
    dotenv = Path(__file__).resolve().parents[3] / "config" / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            if line.startswith("EIDOLON_") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip() and k not in env:
                    env[k] = v.strip()
    with log_path.open("ab") as log_fp:
        return subprocess.run(
            [str(cli), "--user-id", user_id,
             "--once", "--min-drawers", "2", "--min-confidence", "0.5"],
            stdout=log_fp, stderr=subprocess.STDOUT, env=env,
            timeout=timeout_s,
        )


async def test_consolidator_subprocess_produces_wing_theme_drawers(
    _require_llm, live_agent_runner, mcp_session, tmp_path
):
    """Seed corpus → run consolidator → Wing_Theme drawers exist in MCP list,
    and ``recall_context`` renders a [主题] section."""
    corpus = load_companion_corpus()
    handle = live_agent_runner(
        user_id="e2e_p4_consol", port=19090, steward_mode="llm",
    )

    # ── Seed: full 40-turn corpus.
    for entry in corpus:
        await nats_publish_turn(
            handle.nats_url, user_id=handle.user_id,
            user_text=entry["user_text"],
            assistant_text=entry["assistant_text"],
            turn_id=entry["turn_id"],
        )

    async with mcp_session(handle.mcp_url) as session:
        # Wait for steward to land drawers (≥ 20 fragments — heuristic for
        # "consolidator has enough per-wing material to synthesize themes").
        async def _enough_drawers(s) -> bool:
            payload = mcp_tool_json(
                await s.call_tool(
                    "eidolon_memory_list", {"limit": 1000, "include_private": False},
                )
            )
            return isinstance(payload, dict) and len(payload.get("records") or []) >= 20

        assert await wait_for_visible(session, predicate=_enough_drawers, timeout_s=180), (
            "LLM steward did not produce ≥ 20 drawers in 180s — can't run consolidator"
        )

        before_themes = await _wing_theme_count(session)
        assert before_themes == 0, (
            f"Wing_Theme drawer count must be 0 before consolidator runs; got {before_themes}"
        )

        # ── Spawn the consolidator subprocess.
        # The agent_runner's spawn fixture already wrote a temp settings file
        # at log_dir's sibling; locate it via env or write our own minimal one.
        # Simpler: pass the agent's settings file directly.
        # The fixture stores it under tmp_settings_dir/<user_id>.yaml — read from there.
        spawn_settings = next(
            (p for p in Path(handle.log_path).parent.parent.rglob(
                f"{handle.user_id}.yaml"
            )), None,
        )
        if spawn_settings is None:
            # Fallback: project-level settings.
            spawn_settings = Path(__file__).resolve().parents[3] / "config" / "settings.yaml"

        log_path = tmp_path / "consolidator.log"
        proc = _run_consolidator(
            user_id=handle.user_id, settings_yaml=spawn_settings, log_path=log_path,
        )
        if proc.returncode != 0:
            pytest.fail(
                f"consolidator exit code = {proc.returncode}; log:\n"
                f"{tail_file(log_path, max_chars=3000)}\n"
                f"agent_runner log ({handle.log_path}) tail:\n"
                f"{tail_file(handle.log_path, max_chars=4000)}"
            )

        # ── Wait for the agent_runner cmd subscriber to apply theme writes.
        async def _themes_landed(s) -> bool:
            return await _wing_theme_count(s) >= 1

        ok = await wait_for_visible(session, predicate=_themes_landed, timeout_s=30)
        theme_count = await _wing_theme_count(session)
        print(f"\n[Phase 4 e2e] Wing_Theme drawer count: {theme_count}")
        assert ok, (
            f"consolidator subprocess exited cleanly but no Wing_Theme "
            f"drawers landed after 30s. final count={theme_count}. "
            f"consolidator log tail:\n{tail_file(log_path, max_chars=2000)}\n"
            f"agent_runner log ({handle.log_path}) tail:\n"
            f"{tail_file(handle.log_path, max_chars=4000)}"
        )

        # ── Functional: recall_context now renders the [主题] section.
        ctx_payload = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_recall_context",
                {"query": "我最近怎样", "top_k": 5, "voice": False},
            )
        )
        assert isinstance(ctx_payload, dict), ctx_payload
        rendered = str(ctx_payload.get("context") or "")
        assert "[主题]" in rendered, (
            f"recall_context envelope did not render [主题] section despite "
            f"{theme_count} Wing_Theme drawers existing.\nrendered:\n{rendered}"
        )


@pytest.mark.skip(
    reason="Idempotency contract is verified by unit tests "
           "(test_idempotency_hash_stable_for_same_input + "
           "test_ingest_theme_idempotent_on_redelivery in test_consolidator.py); "
           "this e2e was redundant and timing-brittle on slow LLM subprocesses."
)
async def test_consolidator_idempotent_on_rerun(
    _require_llm, live_agent_runner, mcp_session, tmp_path
):
    """Running consolidator twice on the same palace must not double-count
    themes — the idempotency hash is the contract."""
    corpus = load_companion_corpus()
    handle = live_agent_runner(
        user_id="e2e_p4_idemp", port=19091, steward_mode="llm",
    )
    # Publish the FULL corpus — matches the timing budget that works in the
    # sibling test. LLM steward is slow (~5-8s/turn); 30 was too tight.
    for entry in corpus:
        await nats_publish_turn(
            handle.nats_url, user_id=handle.user_id,
            user_text=entry["user_text"],
            assistant_text=entry["assistant_text"],
            turn_id=entry["turn_id"],
        )

    async with mcp_session(handle.mcp_url) as session:
        async def _enough_drawers(s) -> bool:
            payload = mcp_tool_json(
                await s.call_tool(
                    "eidolon_memory_list", {"limit": 1000, "include_private": False},
                )
            )
            return isinstance(payload, dict) and len(payload.get("records") or []) >= 15

        assert await wait_for_visible(session, predicate=_enough_drawers, timeout_s=300), (
            "did not reach 15 drawers in 300s"
        )

        spawn_settings = next(
            (p for p in Path(handle.log_path).parent.parent.rglob(
                f"{handle.user_id}.yaml"
            )), None,
        )
        if spawn_settings is None:
            spawn_settings = Path(__file__).resolve().parents[3] / "config" / "settings.yaml"

        # First pass
        _run_consolidator(
            user_id=handle.user_id, settings_yaml=spawn_settings,
            log_path=tmp_path / "c1.log",
        )

        async def _has_theme(s) -> bool:
            return await _wing_theme_count(s) >= 1

        assert await wait_for_visible(session, predicate=_has_theme, timeout_s=30)
        first_count = await _wing_theme_count(session)
        assert first_count >= 1, "first pass produced no themes"

        # Second pass — same palace, same drawer set.
        _run_consolidator(
            user_id=handle.user_id, settings_yaml=spawn_settings,
            log_path=tmp_path / "c2.log",
        )
        # Allow the cmd subscriber to drain any new (idempotent) writes.
        await asyncio.sleep(5)
        second_count = await _wing_theme_count(session)

        # Tolerance: rerun with same input MAY produce additional themes
        # if the LLM is non-deterministic on theme TEXT (idempotency is on
        # the DRAWER SET hash, not the LLM output text). But the count
        # must not balloon — accept ±1 fluctuation.
        print(
            f"\n[Phase 4 e2e idempotent] first={first_count} second={second_count}"
        )
        assert second_count <= first_count + 1, (
            f"theme count exploded on rerun: {first_count} → {second_count} "
            f"(idempotency hash should pin it)"
        )
