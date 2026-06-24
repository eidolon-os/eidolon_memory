"""``eidolon-memory-consolidator`` — Phase 4 background theme worker.

A *separate process* from ``eidolon-memory-agent`` that:

  1. Reads drawer text via an internal NATS request/reply snapshot endpoint
     served by the user's ``eidolon-memory-agent``. This keeps D1 intact:
     agent_runner remains the only process holding the Chroma/KG handles.
  2. Groups drawers by ``metadata.wing`` within a time window.
  3. Asks an LLM to distil 0–3 high-level themes per wing (skipping
     ``Wing_Theme`` itself and ``Wing_Privacy``).
  4. Publishes each surviving theme back as a ``ConsolidatorIngestThemeCommand``
     on ``agent.memory.cmd.<user_id>``. The agent_runner picks it up, writes
     it as a ``Wing_Theme`` drawer (no steward involvement — themes are
     already structured output).

Why a separate process
  - Long, LLM-heavy work (~30-60s per user) must not block the chat-turn
    pipeline. The voice 300ms budget is precious.
  - "Another agent client" preserves the D1 single-owner story: only
    agent_runner writes chromadb directly; consolidator goes through NATS
    like every other writer.
  - Failures (LLM down, network blip) only affect the post-hoc summary
    layer — chat memory continues untouched.

CLI
  eidolon-memory-consolidator --user-id alice [--once | --interval-hours 6]
                              [--window-days 30] [--min-drawers 3]

The ``--once`` flag is what tests / cron use. Without it the worker loops
on ``--interval-hours`` (default 6).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from eidolon_sdk.memory import ConsolidatorIngestThemeCommand

from eidolon.memory.config.memory_settings import (
    MemorySettings,
    get_memory_settings,
)
from eidolon.memory.infrastructure.nats.commands import JetStreamCommandPublisher
from eidolon.memory.infrastructure.nats.query import NatsMemoryQueryClient
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


# Wings the consolidator distils themes from. Wing_Theme is excluded so the
# worker doesn't loop on its own output; Wing_Privacy is excluded so we don't
# echo do-not-recall material into a summary that bypasses the privacy filter.
_THEMABLE_WINGS_DEFAULT: frozenset[str] = frozenset({
    "Wing_Profile",
    "Wing_Relationship",
    "Wing_Emotion",
    "Wing_Life",
    "Wing_Work",
    "Wing_Health",
    "Wing_Future",
    "Wing_Event",
})


@dataclass
class Theme:
    """One LLM-distilled summary for a (wing, time-window) slice."""

    text: str
    underlying_wing: str
    confidence: float
    source_drawer_ids: list[str]

    def idempotency_hash(self, *, user_id: str, window_days: int) -> str:
        """Deterministic key so re-running the worker on the same input is a no-op.

        Composition: user_id + wing + window + sorted drawer ids. Any change
        to the input set (new drawer, deleted drawer, different window) flips
        the hash and produces a new theme; identical input collapses at the
        chroma layer via the ``fragment_id``.
        """
        body = "|".join([
            user_id, self.underlying_wing, str(window_days),
            ",".join(sorted(self.source_drawer_ids)),
        ])
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


# ───────────────────────────────────────────────────────────────────────────
# Drawer reading (internal NATS query, read-only)
# ───────────────────────────────────────────────────────────────────────────


async def _list_all_drawers(
    query_client: NatsMemoryQueryClient,
    *,
    memory_space_id: str,
    limit: int = 5000,
    page_size: int = 250,
) -> list[dict]:
    """Pull a bounded drawer snapshot through agent_runner's query responder.

    The responder runs inside the owning ``eidolon-memory-agent`` and uses its
    existing ``LockedBackend``. Consolidator therefore stays decoupled from MCP
    HTTP while still avoiding a second Chroma/PersistentClient owner.
    """
    out: list[dict] = []
    offset = 0
    page = max(1, min(page_size, 1000))
    while len(out) < limit:
        take = min(page, limit - len(out))
        payload = await query_client.list_drawers(
            memory_space_id=memory_space_id,
            limit=take,
            offset=offset,
            include_private=False,
        )
        records = payload.get("records") or []
        if not isinstance(records, list) or not records:
            break
        out.extend(r for r in records if isinstance(r, dict))
        if len(records) < take:
            break
        offset += take
    return out


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def group_drawers_by_wing(
    drawers: list[dict],
    *,
    window_days: int,
    now: datetime | None = None,
    themable_wings: frozenset[str] = _THEMABLE_WINGS_DEFAULT,
) -> dict[str, list[dict]]:
    """Filter drawers to ``themable_wings`` ∩ last ``window_days``, group by wing.

    A drawer's "age" comes from its ``created_at`` (mempalace populates this).
    Drawers with missing/unparseable timestamps are kept (treated as recent) —
    we'd rather over-include than under-include for theme synthesis.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    by_wing: dict[str, list[dict]] = defaultdict(list)
    for rec in drawers:
        meta = rec.get("metadata") or {}
        wing = meta.get("wing") or rec.get("user_id")  # MemoryWireRecord stores wing in metadata
        if wing not in themable_wings:
            continue
        ts = _parse_iso(rec.get("created_at"))
        if ts is not None and ts < cutoff:
            continue
        by_wing[wing].append(rec)
    return dict(by_wing)


# ───────────────────────────────────────────────────────────────────────────
# LLM theme extraction
# ───────────────────────────────────────────────────────────────────────────


_THEME_SYSTEM_PROMPT = """你是一个陪伴 AI 的记忆主题归纳器。

给定一个 wing 下最近 N 天的零散记忆片段，归纳 0-3 条**高阶主题**：
- 主题不是事件复述，而是跨片段的"走势"或"关注点"
- 用 1-2 句中文（30-80字），口语化，第二人称称呼用户（如"你"）
- 每条主题给一个 0.0-1.0 的 confidence
- 没有足够素材就返回空数组

输出严格 JSON，shape:
{
  "themes": [
    {"text": "近三周你反复担心妈妈失眠，自己也跟着焦虑得睡不着。", "confidence": 0.85}
  ]
}

只输出 JSON，无任何额外文字。
"""


def _render_drawer_list(records: list[dict], *, max_items: int = 30) -> str:
    """Compact prompt body: oldest → newest, capped, with timestamp + text."""
    sorted_records = sorted(
        records[-max_items:],
        key=lambda r: _parse_iso(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    lines: list[str] = []
    for r in sorted_records:
        ts = (r.get("created_at") or "")[:10] or "????"
        val = str(r.get("value") or "").strip().replace("\n", " ")
        if len(val) > 200:
            val = val[:200] + "…"
        lines.append(f"- [{ts}] {val}")
    return "\n".join(lines)


def _extract_themes_from_llm_response(raw_text: str) -> list[dict]:
    """Parse the LLM JSON envelope, tolerating common formatting wobbles."""
    text = raw_text.strip()
    # Strip ```json fences if the model added them.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: grab the first {...} block.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []
    themes = payload.get("themes") or []
    return [t for t in themes if isinstance(t, dict) and t.get("text")]


async def synthesize_themes_for_wing(
    wing_id: str,
    records: list[dict],
    *,
    settings: MemorySettings,
) -> list[Theme]:
    """One LLM round-trip per wing; never raises (catches + logs)."""
    if not records:
        return []
    import litellm  # local import — only consolidator process needs it

    drawer_ids = [
        str(r.get("metadata", {}).get("fragment_id") or r.get("key") or "")
        for r in records
        if (r.get("metadata", {}).get("fragment_id") or r.get("key"))
    ]
    body = _render_drawer_list(records)
    user_prompt = f"Wing: {wing_id}\n最近的记忆片段:\n{body}\n\n请归纳主题。"

    try:
        resp = await litellm.acompletion(
            model=settings.llm.model,
            api_base=settings.llm.base_url or None,
            api_key=settings.llm.resolve_api_key(),
            messages=[
                {"role": "system", "content": _THEME_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=settings.llm.temperature,
            timeout=settings.llm.timeout_seconds,
            max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001 - consolidator must not crash on LLM blips
        log.warning("consolidator_llm_failed", wing=wing_id, error=str(exc))
        return []

    raw_text = (resp.choices[0].message.content or "").strip()
    parsed = _extract_themes_from_llm_response(raw_text)
    log.info(
        "consolidator_llm_response",
        wing=wing_id,
        drawer_count=len(records),
        raw_chars=len(raw_text),
        parsed_themes=len(parsed),
        # First 300 chars of the raw — diagnoses prompt drift / json failures.
        raw_preview=raw_text[:300],
    )
    themes: list[Theme] = []
    for entry in parsed:
        conf = float(entry.get("confidence") or 0.7)
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        themes.append(Theme(
            text=text,
            underlying_wing=wing_id,
            confidence=max(0.0, min(1.0, conf)),
            source_drawer_ids=drawer_ids,
        ))
    return themes


# ───────────────────────────────────────────────────────────────────────────
# Publication
# ───────────────────────────────────────────────────────────────────────────


async def publish_themes(
    themes: list[Theme],
    *,
    memory_space_id: str,
    window_days: int,
    publisher: JetStreamCommandPublisher,
) -> int:
    """Publish each theme as a ``ConsolidatorIngestThemeCommand``.

    The agent_runner side (``process_command_message``) handles idempotency
    via the deterministic ``fragment_id`` derived from ``request_id``.
    """
    published = 0
    for theme in themes:
        request_id = theme.idempotency_hash(user_id=memory_space_id, window_days=window_days)
        cmd = ConsolidatorIngestThemeCommand(
            request_id=request_id,
            memory_space_id=memory_space_id,
            issued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            issuer="agent",
            text=theme.text,
            underlying_wing=theme.underlying_wing,
            window_days=window_days,
            source_drawer_ids=theme.source_drawer_ids,
            confidence=theme.confidence,
        )
        try:
            await publisher.publish(cmd)
            published += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("consolidator_publish_failed", error=str(exc))
    return published


# ───────────────────────────────────────────────────────────────────────────
# Orchestration
# ───────────────────────────────────────────────────────────────────────────


async def consolidate_once(
    *,
    user_id: str,
    settings: MemorySettings,
    window_days: int = 30,
    min_drawers: int = 3,
    confidence_threshold: float = 0.6,
    query_timeout_seconds: float = 5.0,
    query_startup_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    """One full pass — returns a result dict suitable for logging or assertion.

    The work the worker actually does:
      1. request a drawer snapshot from the owning agent_runner over NATS
      2. group by themable wing within window
      3. for each wing with ≥ ``min_drawers`` drawers:
           - synthesize themes via LLM
           - drop themes below ``confidence_threshold``
      4. publish surviving themes via NATS cmd

    Result schema (one row per wing visited):
      {
        "themes_published": int,
        "wings": [
            {"wing": str, "drawer_count": int, "themes_produced": int,
             "themes_kept": int},
            ...
        ],
      }
    """
    publisher = JetStreamCommandPublisher.from_memory_settings(settings)
    query_client = NatsMemoryQueryClient.from_memory_settings(
        settings,
        timeout_seconds=query_timeout_seconds,
    )
    await publisher.connect()
    await query_client.connect()
    try:
        await query_client.wait_until_ready(
            memory_space_id=user_id,
            timeout_seconds=query_startup_wait_seconds,
        )
        drawers = await _list_all_drawers(query_client, memory_space_id=user_id)
        by_wing = group_drawers_by_wing(drawers, window_days=window_days)

        rows: list[dict[str, Any]] = []
        all_themes: list[Theme] = []
        for wing_id, recs in sorted(by_wing.items()):
            if len(recs) < min_drawers:
                rows.append({
                    "wing": wing_id,
                    "drawer_count": len(recs),
                    "themes_produced": 0,
                    "themes_kept": 0,
                    "skipped_reason": "below_min_drawers",
                })
                continue
            produced = await synthesize_themes_for_wing(wing_id, recs, settings=settings)
            kept = [t for t in produced if t.confidence >= confidence_threshold]
            all_themes.extend(kept)
            rows.append({
                "wing": wing_id,
                "drawer_count": len(recs),
                "themes_produced": len(produced),
                "themes_kept": len(kept),
            })

        published = await publish_themes(
            all_themes, memory_space_id=user_id, window_days=window_days, publisher=publisher,
        )
        return {"themes_published": published, "wings": rows}
    finally:
        await query_client.close()
        await publisher.close()


async def _run(args: argparse.Namespace) -> int:
    settings = get_memory_settings()
    if args.once:
        result = await consolidate_once(
            user_id=args.user_id,
            settings=settings,
            window_days=args.window_days,
            min_drawers=args.min_drawers,
            confidence_threshold=args.min_confidence,
            query_timeout_seconds=args.query_timeout,
            query_startup_wait_seconds=args.query_startup_wait,
        )
        log.info("consolidator_once_done", **{k: v for k, v in result.items() if k != "wings"})
        for row in result["wings"]:
            log.info("consolidator_wing_summary", **row)
        # Stdout-friendly summary for CLI users / cron logs.
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # Loop mode — keep running until SIGTERM.
    interval = max(1, args.interval_hours) * 3600
    while True:
        try:
            await consolidate_once(
                user_id=args.user_id,
                settings=settings,
                window_days=args.window_days,
                min_drawers=args.min_drawers,
                confidence_threshold=args.min_confidence,
                query_timeout_seconds=args.query_timeout,
                query_startup_wait_seconds=args.query_startup_wait,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("consolidator_pass_failed", error=str(exc))
        await asyncio.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consolidation worker — distil cross-time themes per user."
    )
    parser.add_argument("--user-id", required=True,
                        help="Which user's palace to consolidate")
    parser.add_argument("--mcp-url", default="",
                        help=argparse.SUPPRESS)  # deprecated; reads now use NATS query
    parser.add_argument("--once", action="store_true",
                        help="Run a single pass and exit (cron / tests use this)")
    parser.add_argument("--interval-hours", type=float, default=6,
                        help="Loop interval when --once is absent (default 6h)")
    parser.add_argument("--window-days", type=int, default=30,
                        help="Look-back window for drawers")
    parser.add_argument("--min-drawers", type=int, default=3,
                        help="Skip wings with fewer drawers than this")
    parser.add_argument("--min-confidence", type=float, default=0.6,
                        help="Drop themes below this confidence")
    parser.add_argument("--query-timeout", type=float, default=5.0,
                        help="Seconds to wait for the agent_runner NATS query reply")
    parser.add_argument("--query-startup-wait", type=float, default=300.0,
                        help="Seconds to wait for the agent_runner query responder at startup")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
