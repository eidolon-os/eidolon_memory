"""Memory retrieve quality bench — companion corpus → LLM steward → MCP recall.

End-to-end quality + timing measurement that fills a gap between unit tests
(small in scope) and the existing R-01 latency benches (no quality signal).

Pipeline:

    1. Spawn a fresh ``eidolon-memory-agent`` subprocess against a clean
       palace, steward.mode=llm (project's LLM config inherited).
    2. Publish the full ``companion_corpus.jsonl`` (40 turns) via NATS.
    3. Wait for the LLM steward to drain — kg_stats.triples_total >= 8
       is a robust lower bound (the steward extracts triples from
       relationship + event + work turns at ~30-50% rate).
    4. Run every labeled query in ``quality_queries.jsonl`` exactly once.
       For each: measure end-to-end MCP round-trip time and score against
       four orthogonal signals: KG triple match, vector record match,
       working-memory match, negative-query violation.
    5. Aggregate by category — precision / recall / mean latency. Write
       a markdown summary to ``reports/memory_quality_<ts>/summary.md``
       plus the raw per-query JSON next to it.

The bench is **destructive to its own data only**:
  - Uses ``user_id="quality_bench"`` so it doesn't touch the dev palace
  - Wipes the durable JetStream consumer + purges stream messages on that
    subject filter before spawn (so two consecutive runs start clean)
  - Tears down the agent_runner subprocess on exit

This is a quality REPORT, not a pass/fail gate — LLM extraction has run-to-
run variance and pytest is the wrong idiom. Use the report to track
quality drift across phases / prompt edits / model changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import nats
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO_ROOT / "tests" / "memory" / "e2e" / "fixtures"
_DEFAULT_CORPUS = _FIXTURES / "companion_corpus.jsonl"
_DEFAULT_QUERIES = _FIXTURES / "quality_queries.jsonl"
_REPORTS = _REPO_ROOT / "reports"


# ───────────────────────────────────────────────────────────────────────────
# Spawn / teardown helpers (mirror tests/memory/e2e/conftest.py semantics
# but inlined so the bench runs without pytest)
# ───────────────────────────────────────────────────────────────────────────


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _wait_mcp_ready(port: int, *, timeout_s: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/mcp/", timeout=1.0)
            return True
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.TimeoutException):
            pass
        time.sleep(0.5)
    return False


async def _reset_jetstream(nats_url: str, user_id: str) -> None:
    """Drop durables + purge subjects for ``user_id`` — same logic as the
    e2e fixture, so a re-run starts clean."""
    try:
        nc = await nats.connect(nats_url)
    except Exception:
        return
    try:
        js = nc.jetstream()
        for suffix in ("", "-cmd"):
            try:
                await js.delete_consumer(
                    "MEMORY_TURNS", f"eidolon-memory-agent{suffix}-{user_id}"
                )
            except Exception:
                pass
        for subj in (
            f"agent.memory.conversation.turn.{user_id}",
            f"agent.memory.cmd.{user_id}",
        ):
            try:
                await js.purge_stream("MEMORY_TURNS", subject=subj)
            except Exception:
                pass
    finally:
        await nc.close()


def _spawn_agent(
    *,
    user_id: str,
    port: int,
    palace_root: Path,
    settings_path: Path,
    log_path: Path,
) -> subprocess.Popen:
    project_settings = _REPO_ROOT / "config" / "settings.yaml"
    settings_doc: dict[str, Any] = {
        "steward": {"mode": "llm"},
        "mcp_http": {"host": "127.0.0.1", "port": port},
        "runtime": {"palaces_root": str(palace_root)},
    }
    if project_settings.is_file():
        parent = yaml.safe_load(project_settings.read_text()) or {}
        if isinstance(parent, dict) and "llm" in parent:
            settings_doc["llm"] = parent["llm"]
    settings_path.write_text(yaml.safe_dump(settings_doc, allow_unicode=True), encoding="utf-8")

    env = {**os.environ, "EIDOLON_MEMORY_SETTINGS_YAML": str(settings_path)}
    dotenv = _REPO_ROOT / "config" / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            if line.startswith("EIDOLON_") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip() and k not in env:
                    env[k] = v.strip()

    venv_bin = Path(sys.executable).parent
    agent_cli = venv_bin / "eidolon-memory-agent"
    if not agent_cli.is_file():
        cli = shutil.which("eidolon-memory-agent")
        if not cli:
            raise RuntimeError("eidolon-memory-agent not on PATH or in venv")
        agent_cli = Path(cli)

    proc = subprocess.Popen(
        [str(agent_cli), "--user-id", user_id, "--port", str(port)],
        stdout=log_path.open("ab"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    if not _wait_mcp_ready(port):
        proc.terminate()
        raise RuntimeError(f"agent_runner did not bind :{port} within 45s ({log_path})")
    return proc


# ───────────────────────────────────────────────────────────────────────────
# MCP helpers (kept thin; mirrors conftest.mcp_tool_json)
# ───────────────────────────────────────────────────────────────────────────


def _unwrap(result: Any) -> Any:
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


@asynccontextmanager
async def _mcp_session(url: str):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            yield session


async def _publish_turn(nats_url: str, *, user_id: str, turn: dict) -> None:
    payload = {
        "turn_id": turn["turn_id"],
        "user_id": user_id,
        "session_id": "quality_bench",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_text": turn["user_text"],
        "assistant_text": turn["assistant_text"],
    }
    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()
        await js.publish(
            f"agent.memory.conversation.turn.{user_id}",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    finally:
        await nc.close()


# ───────────────────────────────────────────────────────────────────────────
# Per-query scoring
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class QueryResult:
    id: str
    category: str
    query: str
    elapsed_ms: float
    kg_hit: bool                  # at least one expected_entities[i] surfaced
    vector_hit: bool              # at least one expected_vector_contains[i] surfaced
    working_memory_hit: bool      # if expects_working_memory, did wm contain anything
    negative_violation: bool      # negative query but forbidden term/entity surfaced
    matched_signals: list[str] = field(default_factory=list)
    raw_kg_objects: list[str] = field(default_factory=list)

    is_negative: bool = False

    @property
    def correct(self) -> bool:
        """Composite verdict — what the summary counts as 'success'.

        Positive queries: at least one signal (KG / vector / WM) must hit.
        Negative queries: succeed iff NO forbidden term/entity surfaced,
        regardless of whether positive signals fired.
        """
        if self.is_negative:
            return not self.negative_violation
        return self.kg_hit or self.vector_hit or self.working_memory_hit


def _kg_objects(kg_triples: list[Any]) -> list[str]:
    """Flatten subject + object across triples for substring match scoring."""
    out: list[str] = []
    for t in kg_triples or []:
        if not isinstance(t, dict):
            continue
        s = str(t.get("subject", ""))
        o = str(t.get("object", ""))
        if s:
            out.append(s)
        if o:
            out.append(o)
    return out


def _vector_values(records: list[Any]) -> list[str]:
    out: list[str] = []
    for r in records or []:
        if isinstance(r, dict) and r.get("value"):
            out.append(str(r["value"]))
    return out


def _wm_texts(wm: list[Any]) -> list[str]:
    out: list[str] = []
    for t in wm or []:
        if not isinstance(t, dict):
            continue
        if t.get("user_text"):
            out.append(str(t["user_text"]))
        if t.get("assistant_text"):
            out.append(str(t["assistant_text"]))
    return out


def _score_query(query: dict, response: dict, elapsed_ms: float) -> QueryResult:
    kg = response.get("kg_triples") or []
    records = response.get("records") or []
    wm = response.get("working_memory") or []
    kg_blobs = _kg_objects(kg)
    kg_blob_lower = " ".join(kg_blobs).lower()
    vector_blob = " ".join(_vector_values(records))
    wm_blob = " ".join(_wm_texts(wm))

    expected_entities = [e.lower() for e in (query.get("expected_entities") or [])]
    expected_contains = query.get("expected_vector_contains") or []
    is_negative = bool(query.get("negative"))
    forbidden_entities = [e.lower() for e in (query.get("forbidden_entities") or [])]
    forbidden_contains = query.get("forbidden_contains") or []
    expects_wm = bool(query.get("expects_working_memory"))

    matched: list[str] = []
    kg_hit = False
    for ent in expected_entities:
        if ent and ent in kg_blob_lower:
            kg_hit = True
            matched.append(f"kg:{ent}")
            break

    vector_hit = False
    for s in expected_contains:
        if s and (s in vector_blob or s in kg_blob_lower):
            vector_hit = True
            matched.append(f"vec:{s}")
            break

    wm_hit = False
    if expects_wm and wm:
        wm_hit = True
        matched.append("wm:present")

    # Negative-query violation: any forbidden term/entity surfaced.
    violation = False
    if is_negative:
        for ent in forbidden_entities:
            if ent and ent in kg_blob_lower:
                violation = True
                matched.append(f"VIOLATE-kg:{ent}")
                break
        if not violation:
            for s in forbidden_contains:
                if s and (s in vector_blob or s in kg_blob_lower):
                    violation = True
                    matched.append(f"VIOLATE-vec:{s}")
                    break

    return QueryResult(
        id=query["id"],
        category=query["category"],
        query=query["query"],
        elapsed_ms=round(elapsed_ms, 2),
        kg_hit=kg_hit,
        vector_hit=vector_hit,
        working_memory_hit=wm_hit,
        negative_violation=violation,
        matched_signals=matched,
        raw_kg_objects=kg_blobs[:8],
        is_negative=is_negative,
    )


# ───────────────────────────────────────────────────────────────────────────
# Aggregation + markdown rendering
# ───────────────────────────────────────────────────────────────────────────


def _aggregate(results: list[QueryResult]) -> dict[str, Any]:
    by_cat: dict[str, list[QueryResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    cat_rows: list[dict[str, Any]] = []
    for cat, items in sorted(by_cat.items()):
        n = len(items)
        correct = sum(1 for x in items if x.correct)
        kg_hits = sum(1 for x in items if x.kg_hit)
        vec_hits = sum(1 for x in items if x.vector_hit)
        latencies = [x.elapsed_ms for x in items]
        cat_rows.append({
            "category": cat,
            "n": n,
            "correct": correct,
            "correct_pct": round(correct / n * 100, 1),
            "kg_hits": kg_hits,
            "vec_hits": vec_hits,
            "p50_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(_p95(latencies), 1),
            "mean_ms": round(statistics.mean(latencies), 1),
        })

    overall_lat = [r.elapsed_ms for r in results]
    overall_correct = sum(1 for r in results if r.correct)
    overall = {
        "n": len(results),
        "correct": overall_correct,
        "correct_pct": round(overall_correct / len(results) * 100, 1) if results else 0,
        "p50_ms": round(statistics.median(overall_lat), 1) if overall_lat else 0,
        "p95_ms": round(_p95(overall_lat), 1) if overall_lat else 0,
        "mean_ms": round(statistics.mean(overall_lat), 1) if overall_lat else 0,
        "max_ms": round(max(overall_lat), 1) if overall_lat else 0,
        "min_ms": round(min(overall_lat), 1) if overall_lat else 0,
    }
    return {"per_category": cat_rows, "overall": overall}


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = max(0, int(round(len(s) * 0.95)) - 1)
    return s[idx]


def _render_markdown(
    *,
    agg: dict[str, Any],
    results: list[QueryResult],
    kg_stats: dict[str, Any],
    fragments: int,
    corpus_size: int,
    ingest_seconds: float,
) -> str:
    lines: list[str] = []
    lines.append("# Memory retrieve quality bench\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_\n")
    lines.append("")
    lines.append("## Setup")
    lines.append(f"- Corpus turns published: **{corpus_size}**")
    lines.append(f"- Ingestion wait time:    **{ingest_seconds:.1f}s**")
    lines.append(
        f"- Palace state at query time:  "
        f"fragments=**{fragments}**, "
        f"entities={kg_stats.get('entities')}, "
        f"triples_total={kg_stats.get('triples_total')}, "
        f"triples_active={kg_stats.get('triples_active')}"
    )
    lines.append("")

    lines.append("## Overall")
    o = agg["overall"]
    lines.append(
        f"- Queries: **{o['n']}**, correct: **{o['correct']}/{o['n']}** "
        f"= **{o['correct_pct']}%**"
    )
    lines.append(
        f"- Latency (per query, end-to-end MCP round-trip): "
        f"p50 **{o['p50_ms']}ms**, p95 **{o['p95_ms']}ms**, "
        f"mean **{o['mean_ms']}ms**, min/max {o['min_ms']}/{o['max_ms']}ms"
    )
    lines.append("")

    lines.append("## Per-category breakdown")
    lines.append("")
    lines.append("| Category | n | correct | rate | kg-hit | vec-hit | p50 ms | p95 ms | mean ms |")
    lines.append("|----------|--:|--------:|-----:|-------:|--------:|-------:|-------:|--------:|")
    for row in agg["per_category"]:
        lines.append(
            f"| {row['category']} | {row['n']} | "
            f"{row['correct']}/{row['n']} | {row['correct_pct']}% | "
            f"{row['kg_hits']} | {row['vec_hits']} | "
            f"{row['p50_ms']} | {row['p95_ms']} | {row['mean_ms']} |"
        )
    lines.append("")

    lines.append("## Per-query detail")
    lines.append("")
    lines.append("| id | category | query | ms | kg | vec | wm | violation | matched |")
    lines.append("|----|----------|-------|---:|:--:|:---:|:--:|:---------:|---------|")
    def _x(b: bool, neg: bool = False) -> str:
        return ("✓" if b else "·") if not neg else ("⚠" if b else "·")
    for r in results:
        violation = "⚠" if r.negative_violation else "·"
        matched = ", ".join(r.matched_signals[:3]) if r.matched_signals else ""
        if len(matched) > 60:
            matched = matched[:57] + "…"
        lines.append(
            f"| {r.id} | {r.category} | {r.query[:30]} | {r.elapsed_ms} | "
            f"{_x(r.kg_hit)} | {_x(r.vector_hit)} | {_x(r.working_memory_hit)} | "
            f"{violation} | {matched} |"
        )
    lines.append("")

    # Failure spotlight — the cases the bench couldn't satisfy.
    misses = [r for r in results if not r.correct]
    if misses:
        lines.append("## Misses (composite-correct=false)")
        lines.append("")
        for r in misses:
            kg_obj_str = ", ".join(r.raw_kg_objects[:4]) if r.raw_kg_objects else "—"
            lines.append(
                f"- `{r.id}` ({r.category}): \"{r.query}\" — "
                f"kg objects returned: {kg_obj_str}"
            )
        lines.append("")

    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────────
# Main orchestration
# ───────────────────────────────────────────────────────────────────────────


async def _list_fragment_count(session: ClientSession) -> int:
    """Total fragments in the palace via the MCP listing tool."""
    result = await session.call_tool(
        "eidolon_memory_list", {"limit": 5000, "include_private": True},
    )
    payload = _unwrap(result)
    if not isinstance(payload, dict):
        return 0
    return len(payload.get("records") or [])


async def _count_wing_theme_drawers(session: ClientSession) -> int:
    """Count Wing_Theme drawers via the MCP listing tool."""
    result = await session.call_tool(
        "eidolon_memory_list", {"limit": 5000, "include_private": False},
    )
    payload = _unwrap(result)
    if not isinstance(payload, dict):
        return 0
    rows = payload.get("records") or []
    return sum(
        1 for r in rows
        if isinstance(r, dict)
        and (r.get("metadata") or {}).get("wing") == "Wing_Theme"
    )


async def _run_consolidator_inline(
    *, user_id: str, mcp_url: str, settings_yaml: Any, log_path: Any,
) -> int:
    """Spawn the consolidator subprocess, wait for it, return exit code.

    Inherits ``EIDOLON_MEMORY_LLM_API_KEY`` from the parent env (forwarded
    from ``config/.env`` if necessary) so the LLM call works.
    """
    import subprocess
    from pathlib import Path
    cli = Path(sys.executable).parent / "eidolon-memory-consolidator"
    if not cli.is_file():
        from shutil import which
        cli_str = which("eidolon-memory-consolidator")
        if cli_str is None:
            raise RuntimeError("eidolon-memory-consolidator not on PATH")
        cli = Path(cli_str)
    env = {**os.environ, "EIDOLON_MEMORY_SETTINGS_YAML": str(settings_yaml)}
    dotenv = _REPO_ROOT / "config" / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            if line.startswith("EIDOLON_") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip() and k not in env:
                    env[k] = v.strip()
    with open(log_path, "ab") as log_fp:
        proc = subprocess.run(
            [str(cli), "--user-id", user_id, "--mcp-url", mcp_url,
             "--once", "--min-drawers", "2", "--min-confidence", "0.5"],
            stdout=log_fp, stderr=subprocess.STDOUT, env=env, timeout=240,
        )
    return proc.returncode


async def _wait_for_ingestion(
    session: ClientSession,
    *,
    target_triples: int,
    target_fragments: int,
    timeout_s: float,
) -> tuple[bool, dict[str, Any], int, float]:
    """Block until KG triples AND drawer fragments BOTH cross their thresholds.

    Steward output is non-uniform: relationship/event turns produce triples,
    preference / lifestyle turns produce fragments without triples. Gating
    on just one signal lets the bench start while half the corpus is still
    being processed. Requiring both gives a far more accurate "the palace
    is fully populated" signal.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    last_stats: dict[str, Any] = {}
    last_fragments = 0
    while time.monotonic() < deadline:
        stats_raw = _unwrap(await session.call_tool("eidolon_memory_kg_stats", {}))
        stats = stats_raw if isinstance(stats_raw, dict) else {}
        last_stats = stats
        last_fragments = await _list_fragment_count(session)
        if (
            int(stats.get("triples_total") or 0) >= target_triples
            and last_fragments >= target_fragments
        ):
            return True, stats, last_fragments, time.monotonic() - start
        await asyncio.sleep(2.0)
    return False, last_stats, last_fragments, time.monotonic() - start


async def _run_query(
    session: ClientSession, query: dict
) -> tuple[QueryResult, dict[str, Any]]:
    t0 = time.perf_counter()
    result = await session.call_tool(
        "eidolon_memory_recall_context",
        {"query": query["query"], "top_k": 5, "voice": False},
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    response = _unwrap(result) or {}
    if not isinstance(response, dict):
        response = {}
    qr = _score_query(query, response, elapsed_ms)
    return qr, response


async def amain(args: argparse.Namespace) -> int:
    corpus_path = Path(args.corpus)
    queries_path = Path(args.queries)
    if not corpus_path.is_file():
        print(f"[err] corpus missing: {corpus_path}", file=sys.stderr)
        return 2
    if not queries_path.is_file():
        print(f"[err] queries missing: {queries_path}", file=sys.stderr)
        return 2

    corpus = [json.loads(line) for line in corpus_path.read_text().splitlines() if line.strip()]
    queries = [json.loads(line) for line in queries_path.read_text().splitlines() if line.strip()]

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = _REPORTS / f"memory_quality_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "agent_runner.log"
    settings_path = out_dir / "spawn_settings.yaml"
    palace_root = out_dir / "palaces"
    palace_root.mkdir(parents=True, exist_ok=True)

    # 0) Reset JetStream state for this user_id so re-runs are clean.
    print(f"[setup] reset JetStream durables for user_id={args.user_id}")
    await _reset_jetstream(args.nats_url, args.user_id)

    # 1) Spawn agent.
    if not _port_free(args.port):
        print(f"[err] port {args.port} already in use", file=sys.stderr)
        return 3
    print(f"[setup] spawning agent_runner on :{args.port} ...")
    proc = _spawn_agent(
        user_id=args.user_id,
        port=args.port,
        palace_root=palace_root,
        settings_path=settings_path,
        log_path=log_path,
    )
    print(f"[setup] agent pid={proc.pid}, log={log_path}")

    try:
        # 2) Publish corpus.
        print(f"[seed] publishing {len(corpus)} turns ...")
        publish_t0 = time.monotonic()
        for entry in corpus:
            await _publish_turn(args.nats_url, user_id=args.user_id, turn=entry)
        publish_dt = time.monotonic() - publish_t0
        print(f"[seed] publish complete in {publish_dt:.1f}s")

        # 3) Open MCP session, wait for ingestion.
        mcp_url = f"http://127.0.0.1:{args.port}/mcp"
        async with _mcp_session(mcp_url) as session:
            print(
                f"[wait] waiting for kg_stats.triples_total >= {args.min_triples} "
                f"AND fragments >= {args.min_fragments} ..."
            )
            ok, stats, fragments, ingest_s = await _wait_for_ingestion(
                session,
                target_triples=args.min_triples,
                target_fragments=args.min_fragments,
                timeout_s=args.ingest_timeout,
            )
            if not ok:
                print(
                    f"[warn] timed out waiting for ingestion thresholds; proceeding "
                    f"with kg_stats={stats}, fragments={fragments}",
                    file=sys.stderr,
                )
            print(
                f"[wait] palace state after {ingest_s:.1f}s: "
                f"entities={stats.get('entities')}, "
                f"triples_total={stats.get('triples_total')}, "
                f"fragments={fragments}"
            )

            # 3.5) Optionally run the consolidator + wait for Wing_Theme drawers.
            consolidator_seconds = 0.0
            theme_count = 0
            if args.with_consolidator:
                print("[consolidator] running ...")
                cons_t0 = time.monotonic()
                consolidator_log = out_dir / "consolidator.log"
                rc = await _run_consolidator_inline(
                    user_id=args.user_id, mcp_url=mcp_url,
                    settings_yaml=settings_path, log_path=consolidator_log,
                )
                consolidator_seconds = time.monotonic() - cons_t0
                if rc != 0:
                    print(
                        f"[consolidator] exit={rc}; log={consolidator_log}",
                        file=sys.stderr,
                    )

                # Wait for cmd subscriber to apply theme writes (themes don't
                # show up instantly — give the JetStream loop a window).
                theme_deadline = time.monotonic() + 30.0
                while time.monotonic() < theme_deadline:
                    theme_count = await _count_wing_theme_drawers(session)
                    if theme_count > 0:
                        break
                    await asyncio.sleep(2.0)
                print(
                    f"[consolidator] {consolidator_seconds:.1f}s, "
                    f"Wing_Theme drawers landed: {theme_count}"
                )

            # 4) Run queries.
            print(f"[query] running {len(queries)} queries ...")
            results: list[QueryResult] = []
            raw_responses: list[dict] = []
            for q in queries:
                try:
                    r, raw = await _run_query(session, q)
                except Exception as exc:  # noqa: BLE001 - bench resilience
                    print(f"  [err] {q['id']}: {exc}")
                    continue
                results.append(r)
                raw_responses.append({"id": q["id"], "response": raw})
                marker = "✓" if r.correct else ("⚠" if r.negative_violation else "·")
                print(
                    f"  {marker} {r.id:<22} {r.category:<18} {r.elapsed_ms:>6.1f}ms  "
                    f"{', '.join(r.matched_signals[:2])}"
                )

            agg = _aggregate(results)

        # 5) Render summary.
        md = _render_markdown(
            agg=agg,
            results=results,
            kg_stats=stats,
            fragments=fragments,
            corpus_size=len(corpus),
            ingest_seconds=ingest_s,
        )
        (out_dir / "summary.md").write_text(md, encoding="utf-8")
        (out_dir / "raw_results.json").write_text(
            json.dumps(
                {
                    "config": {
                        "corpus": str(corpus_path),
                        "queries": str(queries_path),
                        "user_id": args.user_id,
                        "min_triples": args.min_triples,
                        "min_fragments": args.min_fragments,
                    },
                    "kg_stats_at_query_time": stats,
                    "fragments_at_query_time": fragments,
                    "ingest_seconds": ingest_s,
                    "aggregate": agg,
                    "per_query": [r.__dict__ for r in results],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # 6) Stdout summary excerpt for terminal users.
        print("")
        print("=" * 72)
        print("SUMMARY")
        print("=" * 72)
        o = agg["overall"]
        print(
            f"  total {o['n']} queries, correct {o['correct']}/{o['n']} "
            f"({o['correct_pct']}%) · "
            f"p50 {o['p50_ms']}ms · p95 {o['p95_ms']}ms · mean {o['mean_ms']}ms"
        )
        print("")
        print("  by category:")
        for row in agg["per_category"]:
            print(
                f"    {row['category']:<18} "
                f"{row['correct']:>2}/{row['n']:<2} "
                f"({row['correct_pct']:>5.1f}%) "
                f"kg={row['kg_hits']:<2} vec={row['vec_hits']:<2} "
                f"p95={row['p95_ms']:>5.1f}ms"
            )
        print("")
        print(f"  report: {out_dir / 'summary.md'}")
        print(f"  raw:    {out_dir / 'raw_results.json'}")
        print("")
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Companion-corpus quality bench for memory retrieve path."
    )
    parser.add_argument("--corpus", default=str(_DEFAULT_CORPUS),
                        help="JSONL companion corpus (default: companion_corpus.jsonl)")
    parser.add_argument("--queries", default=str(_DEFAULT_QUERIES),
                        help="JSONL labeled query battery")
    parser.add_argument("--user-id", default="quality_bench",
                        help="Isolated user_id for this run")
    parser.add_argument("--port", type=int, default=19200,
                        help="MCP port for spawned agent_runner")
    parser.add_argument("--nats-url", default="nats://127.0.0.1:4222")
    parser.add_argument("--min-triples", type=int, default=18,
                        help="Wait until kg_stats.triples_total reaches this")
    parser.add_argument("--min-fragments", type=int, default=25,
                        help="Wait until eidolon_memory_list returns at least this many fragments")
    parser.add_argument("--ingest-timeout", type=float, default=360.0,
                        help="Max seconds to wait for ingestion (LLM steward is slow)")
    parser.add_argument("--with-consolidator", action="store_true",
                        help="Run eidolon-memory-consolidator after ingestion so the "
                             "[主题] section is populated for the query battery")
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
