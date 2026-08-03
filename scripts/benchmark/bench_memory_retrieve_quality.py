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
       independent evidence groups: KG entity, vector record, and working
       memory.  Unanswerable queries require a clean evidence boundary.
    5. Aggregate by category — full-case accuracy, evidence recall, omissions,
       clean abstention, and latency. Write
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import nats
import yaml
from eidolon_memory_contracts import (
    ConversationTurnPayload,
    build_memory_actor_context,
    conversation_turn_subject,
    envelope_memory_payload,
    memory_command_subject,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.infrastructure.nats.names import memory_consumer_name

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.benchmark.preflight import require_nats  # noqa: E402
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
            response = httpx.get(
                f"http://127.0.0.1:{port}/mcp/",
                timeout=1.0,
                trust_env=False,
            )
            if response.status_code < 500:
                return True
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.TimeoutException):
            pass
        time.sleep(0.5)
    return False


def require_expected_embedder(palace_root: Path, *, configured: str) -> None:
    """Refuse to report if the palace was not built with the configured embedder.

    Copying the config into the spawn settings is not enough on its own: MemPalace
    records what it actually used, and that record is the only authority. A
    mismatch is not a warning — the whole run measures a different retriever than
    the one in production, which is what silently happened to every quality figure
    before 2026-08-03.

    Checked after ingestion rather than before, because the file does not exist
    until the palace is created.
    """

    if not configured:
        print(
            "[FAIL] mempalace.embedding_model is empty in the project settings.\n"
            "       MemPalace then picks its own default (minilm, English-only),\n"
            "       so the run would measure a retriever nobody deploys.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    markers = sorted(palace_root.glob("*/mempalace_embedder.json"))
    if not markers:
        print(
            f"[FAIL] no mempalace_embedder.json under {palace_root}; cannot "
            f"confirm which embedder built this palace.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    doc = json.loads(markers[0].read_text(encoding="utf-8"))
    actual = {
        str(section.get("model_name") or "")
        for section in doc.values()
        if isinstance(section, dict)
    }
    if actual != {configured}:
        print(
            f"[FAIL] palace was built with {sorted(actual)} but the settings say "
            f"{configured!r}.\n"
            f"       Every number from this run would describe the wrong "
            f"retriever. Delete {palace_root} and rerun, or align the config.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(f"[check] palace embedder is {configured} (read from the palace, not the config)")


async def _turns_pending(nats_url: str, user_id: str) -> int | None:
    """Turns published but not yet acknowledged by the agent's consumer.

    This is what "the corpus is ingested" actually means. The threshold check
    below asks whether *enough* was produced, which is a different question and
    the reason two earlier runs measured an incomplete palace: with 24 of 40 turns
    processed the thresholds were already satisfied, so the bench stopped waiting
    and queried anyway. Queries written against the full corpus then failed for
    turns that had never arrived.

    Returns None when the consumer cannot be inspected — a missing consumer is not
    the same as an empty backlog, and treating it as zero would restore exactly the
    false "done" this replaces.
    """

    try:
        nc = await nats.connect(nats_url)
    except Exception:
        return None
    try:
        js = nc.jetstream()
        info = await js.consumer_info(
            "MEMORY_TURNS",
            memory_consumer_name("eidolon-memory-agent", user_id, role="turn"),
        )
        return int(info.num_pending) + int(info.num_ack_pending)
    except Exception:
        return None
    finally:
        await nc.close()


async def _reset_jetstream(nats_url: str, user_id: str) -> None:
    """Drop durables + purge subjects for ``user_id`` — same logic as the
    e2e fixture, so a re-run starts clean."""
    try:
        nc = await nats.connect(nats_url)
    except Exception:
        return
    try:
        js = nc.jetstream()
        for role in ("turn", "cmd"):
            try:
                await js.delete_consumer(
                    "MEMORY_TURNS",
                    memory_consumer_name(
                        "eidolon-memory-agent", user_id, role=role
                    ),
                )
            except Exception:
                pass
        for subj in (
            conversation_turn_subject(user_id),
            memory_command_subject(user_id),
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
    steward_mode: str = "llm",
) -> subprocess.Popen:
    project_settings = _REPO_ROOT / "config" / "settings.yaml"
    settings_doc: dict[str, Any] = {
        "steward": {"mode": steward_mode},
        "mcp_http": {"host": "127.0.0.1", "port": port},
        "runtime": {"palaces_root": str(palace_root)},
    }
    if project_settings.is_file():
        parent = yaml.safe_load(project_settings.read_text()) or {}
        if isinstance(parent, dict):
            # The vector section matters as much as the LLM one. Omitting it left
            # embedding_model empty, and MemPalace then applies its own default of
            # minilm — an English-only model. Every quality figure before
            # 2026-08-03 was therefore measured on minilm against a Chinese
            # corpus, where cross-lingual cosine is about 0.35, while production
            # runs embeddinggemma. settings.example.yaml warns about exactly this
            # failure; the bench was an instance of it.
            for section in ("llm", "mempalace", "kg", "recall"):
                if section in parent:
                    settings_doc[section] = parent[section]
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
        [str(agent_cli), "--memory-space-id", user_id, "--port", str(port)],
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
    def _local_http_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)

    async with streamablehttp_client(url, httpx_client_factory=_local_http_client) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            yield session


async def _publish_turn(nats_url: str, *, user_id: str, turn: dict) -> None:
    context = build_memory_actor_context(
        owner_id="quality_bench",
        companion_id="quality_bench",
        memory_realm_id=user_id,
        device_id="quality_bench",
        session_id="quality_bench",
    )
    payload = ConversationTurnPayload(
        turn_id=turn["turn_id"],
        context=context,
        timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        user_text=turn["user_text"],
        assistant_text=turn["assistant_text"],
        metadata={"source": "memory_retrieve_quality_bench"},
    )
    envelope = envelope_memory_payload(payload, trace_id=payload.turn_id)
    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()
        await js.publish(
            conversation_turn_subject(context.memory_space_id),
            json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False).encode(
                "utf-8"
            ),
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
    evidence_groups_hit: int = 0
    evidence_groups_total: int = 0
    omission_count: int = 0
    returned_evidence_count: int = 0
    expects_abstention: bool = False
    abstention_correct: bool | None = None

    @property
    def correct(self) -> bool:
        """All labelled evidence groups must hit, or abstention must be clean.

        The old scorer accepted a positive case when *any* channel happened to
        match and accepted an unlabelled negative case vacuously.  That hid the
        exact failure we care about: a useful vector hit alongside a wrong KG
        fact, or irrelevant evidence injected for an unknown question.
        """
        if self.expects_abstention:
            return self.abstention_correct is True
        return self.evidence_groups_total > 0 and self.omission_count == 0

    @property
    def evidence_recall(self) -> float:
        if self.evidence_groups_total == 0:
            return 1.0 if self.expects_abstention else 0.0
        return self.evidence_groups_hit / self.evidence_groups_total


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
    expects_abstention = bool(query.get("expect_abstention", query.get("negative")))
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
        if s and s in vector_blob:
            vector_hit = True
            matched.append(f"vec:{s}")
            break

    wm_hit = False
    if expects_wm and wm:
        wm_hit = True
        matched.append("wm:present")

    # Negative-query violation: any forbidden term/entity surfaced.
    violation = False
    if expects_abstention:
        for ent in forbidden_entities:
            if ent and ent in kg_blob_lower:
                violation = True
                matched.append(f"VIOLATE-kg:{ent}")
                break
        if not violation:
            for s in forbidden_contains:
                if s and (s in vector_blob or s in kg_blob_lower or s in wm_blob):
                    violation = True
                    matched.append(f"VIOLATE-vec:{s}")
                    break

    evidence_groups = [
        bool(expected_entities),
        bool(expected_contains),
        expects_wm,
    ]
    evidence_group_hits = [
        kg_hit if expected_entities else False,
        vector_hit if expected_contains else False,
        wm_hit if expects_wm else False,
    ]
    groups_total = sum(evidence_groups)
    groups_hit = sum(
        hit for enabled, hit in zip(evidence_groups, evidence_group_hits, strict=True)
        if enabled
    )
    returned_evidence_count = len(kg) + len(records) + len(wm)
    abstention_correct = None
    if expects_abstention:
        # At the retrieval boundary every returned row is unsupported evidence
        # for an explicitly unanswerable query.  Final-answer abstention belongs
        # to the Agent benchmark; this metric deliberately grades evidence
        # cleanliness before generation.
        abstention_correct = returned_evidence_count == 0 and not violation

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
        evidence_groups_hit=groups_hit,
        evidence_groups_total=groups_total,
        omission_count=max(0, groups_total - groups_hit),
        returned_evidence_count=returned_evidence_count,
        expects_abstention=expects_abstention,
        abstention_correct=abstention_correct,
    )


def _validate_queries(queries: list[dict[str, Any]]) -> None:
    """Reject labels that would make a quality case pass vacuously."""

    seen: set[str] = set()
    for index, query in enumerate(queries, 1):
        label = str(query.get("id") or f"line {index}")
        missing = [key for key in ("id", "category", "query") if not query.get(key)]
        if missing:
            raise ValueError(f"{label}: missing required fields {missing}")
        if label in seen:
            raise ValueError(f"{label}: duplicate query id")
        seen.add(label)

        expects_abstention = bool(
            query.get("expect_abstention", query.get("negative"))
        )
        has_positive_label = bool(
            query.get("expected_entities")
            or query.get("expected_vector_contains")
            or query.get("expects_working_memory")
        )
        if expects_abstention and has_positive_label:
            raise ValueError(
                f"{label}: abstention case cannot also require positive evidence"
            )
        if not expects_abstention and not has_positive_label:
            raise ValueError(
                f"{label}: answerable case must label at least one evidence group"
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
        omissions = sum(x.omission_count for x in items)
        abstention_total = sum(1 for x in items if x.expects_abstention)
        abstention_correct = sum(
            1 for x in items if x.expects_abstention and x.abstention_correct
        )
        latencies = [x.elapsed_ms for x in items]
        cat_rows.append({
            "category": cat,
            "n": n,
            "correct": correct,
            "correct_pct": round(correct / n * 100, 1),
            "kg_hits": kg_hits,
            "vec_hits": vec_hits,
            "evidence_recall": round(
                sum(x.evidence_groups_hit for x in items)
                / max(1, sum(x.evidence_groups_total for x in items)),
                3,
            ),
            "omissions": omissions,
            "abstention_correct": abstention_correct,
            "abstention_total": abstention_total,
            "p50_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(_p95(latencies), 1),
            "mean_ms": round(statistics.mean(latencies), 1),
        })

    overall_lat = [r.elapsed_ms for r in results]
    overall_correct = sum(1 for r in results if r.correct)
    evidence_groups_total = sum(r.evidence_groups_total for r in results)
    evidence_groups_hit = sum(r.evidence_groups_hit for r in results)
    abstention_total = sum(1 for r in results if r.expects_abstention)
    abstention_correct = sum(
        1 for r in results if r.expects_abstention and r.abstention_correct
    )
    overall = {
        "n": len(results),
        "correct": overall_correct,
        "correct_pct": round(overall_correct / len(results) * 100, 1) if results else 0,
        "evidence_recall": round(
            evidence_groups_hit / max(1, evidence_groups_total), 3
        ),
        "omissions": sum(r.omission_count for r in results),
        "abstention_correct": abstention_correct,
        "abstention_total": abstention_total,
        "abstention_rate": round(
            abstention_correct / max(1, abstention_total), 3
        ),
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
    lines.append(f"_Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC_\n")
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
        f"- Queries: **{o['n']}**, fully correct: **{o['correct']}/{o['n']}** "
        f"= **{o['correct_pct']}%**"
    )
    lines.append(
        f"- Evidence-group recall: **{o['evidence_recall']:.1%}**, "
        f"omissions: **{o['omissions']}**, clean abstention: "
        f"**{o['abstention_correct']}/{o['abstention_total']}**"
    )
    lines.append(
        f"- Latency (per query, end-to-end MCP round-trip): "
        f"p50 **{o['p50_ms']}ms**, p95 **{o['p95_ms']}ms**, "
        f"mean **{o['mean_ms']}ms**, min/max {o['min_ms']}/{o['max_ms']}ms"
    )
    lines.append("")

    lines.append("## Per-category breakdown")
    lines.append("")
    lines.append(
        "| Category | n | correct | rate | evidence recall | omissions | "
        "abstain | p50 ms | p95 ms |"
    )
    lines.append("|----------|--:|--------:|-----:|----------------:|----------:|--------:|-------:|-------:|")
    for row in agg["per_category"]:
        lines.append(
            f"| {row['category']} | {row['n']} | "
            f"{row['correct']}/{row['n']} | {row['correct_pct']}% | "
            f"{row['evidence_recall']:.1%} | {row['omissions']} | "
            f"{row['abstention_correct']}/{row['abstention_total']} | "
            f"{row['p50_ms']} | {row['p95_ms']} |"
        )
    lines.append("")

    lines.append("## Per-query detail")
    lines.append("")
    lines.append(
        "| id | category | query | ms | evidence | omitted | returned | "
        "abstain | matched |"
    )
    lines.append("|----|----------|-------|---:|---------:|--------:|---------:|:-------:|---------|")
    for r in results:
        abstain = "✓" if r.abstention_correct else ("✗" if r.expects_abstention else "·")
        matched = ", ".join(r.matched_signals[:3]) if r.matched_signals else ""
        if len(matched) > 60:
            matched = matched[:57] + "…"
        lines.append(
            f"| {r.id} | {r.category} | {r.query[:30]} | {r.elapsed_ms} | "
            f"{r.evidence_groups_hit}/{r.evidence_groups_total} | {r.omission_count} | "
            f"{r.returned_evidence_count} | {abstain} | {matched} |"
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


def _render_ab_comparison(
    *,
    baseline: dict[str, Any],
    themed: dict[str, Any],
    theme_count: int,
    consolidator_seconds: float,
) -> str:
    """Render the before/after (no-themes vs themed) per-category delta.

    This is the table that actually answers "did Phase 4 lift the
    topic / emotion / preference categories the original bench flagged at
    0-25%?" — the one gap the Phase 4 P-gate never closed.
    """
    b_cats = {r["category"]: r for r in baseline["per_category"]}
    t_cats = {r["category"]: r for r in themed["per_category"]}
    cats = sorted(set(b_cats) | set(t_cats))

    lines: list[str] = []
    lines.append("## Phase 4 A/B — query battery before vs after consolidation\n")
    lines.append(
        f"- Wing_Theme drawers produced: **{theme_count}** "
        f"(consolidator ran {consolidator_seconds:.1f}s)"
    )
    bo, to = baseline["overall"], themed["overall"]
    lines.append(
        f"- Overall correct: baseline **{bo['correct_pct']}%** → "
        f"themed **{to['correct_pct']}%** "
        f"(Δ {to['correct_pct'] - bo['correct_pct']:+.1f}pp)"
    )
    lines.append("")
    lines.append("| Category | baseline | themed | Δ pp |")
    lines.append("|----------|---------:|-------:|-----:|")
    for cat in cats:
        b = b_cats.get(cat, {}).get("correct_pct", 0.0)
        t = t_cats.get(cat, {}).get("correct_pct", 0.0)
        arrow = "▲" if t > b else ("▼" if t < b else "=")
        lines.append(f"| {cat} | {b}% | {t}% | {arrow} {t - b:+.1f} |")
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
            [str(cli), "--memory-space-id", user_id, "--mcp-url", mcp_url,
             "--once", "--min-drawers", "2", "--min-confidence", "0.5"],
            stdout=log_fp, stderr=subprocess.STDOUT, env=env, timeout=240,
        )
    return proc.returncode


async def _wait_for_ingestion(
    session: ClientSession,
    *,
    nats_url: str,
    user_id: str,
    target_triples: int,
    target_fragments: int,
    timeout_s: float,
) -> tuple[bool, dict[str, Any], int, float]:
    """Block until every published turn has been consumed.

    The gate is the consumer backlog, not the amount of output. This used to
    wait for triple and fragment counts to cross thresholds, and its docstring
    claimed that gave a "fully populated" signal — it cannot. Output volume is a
    proxy for progress, and it saturated at 24 of 40 turns, at which point the
    bench stopped waiting and queried a palace missing 40% of the corpus. Every
    query written against a turn that never arrived then failed, and the result
    looked like a retrieval problem.

    Thresholds are still checked, but as a floor beneath the real condition
    rather than instead of it: a drained queue that produced almost nothing means
    extraction is broken, and that should not read as success either.
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
        pending = await _turns_pending(nats_url, user_id)
        # None means the consumer could not be read; waiting is the safe reading,
        # since an unreadable backlog is not an empty one.
        if pending == 0 and (
            int(stats.get("triples_total") or 0) >= target_triples
            and last_fragments >= target_fragments
        ):
            return True, stats, last_fragments, time.monotonic() - start
        await asyncio.sleep(2.0)
    return False, last_stats, last_fragments, time.monotonic() - start


async def _run_battery(
    session: ClientSession,
    queries: list[dict],
    *,
    context: dict[str, Any],
    label: str,
) -> tuple[list[QueryResult], list[dict]]:
    """Run the full query battery once, printing a one-line trace per query.

    ``label`` distinguishes the before/after passes in the A/B flow
    (e.g. "baseline" vs "themed").
    """
    print(f"[query:{label}] running {len(queries)} queries ...")
    results: list[QueryResult] = []
    raw: list[dict] = []
    for q in queries:
        try:
            r, response = await _run_query(session, q, context=context)
        except Exception as exc:  # noqa: BLE001 - bench resilience
            print(f"  [err] {q['id']}: {exc}")
            continue
        results.append(r)
        raw.append({"id": q["id"], "response": response})
        marker = "✓" if r.correct else ("⚠" if r.negative_violation else "·")
        print(
            f"  {marker} {r.id:<22} {r.category:<18} {r.elapsed_ms:>6.1f}ms  "
            f"{', '.join(r.matched_signals[:2])}"
        )
    return results, raw


async def _run_query(
    session: ClientSession,
    query: dict,
    *,
    context: dict[str, Any],
) -> tuple[QueryResult, dict[str, Any]]:
    t0 = time.perf_counter()
    result = await session.call_tool(
        "eidolon_memory_recall_context",
        {
            "query": query["query"],
            "context": context,
            "top_k": 5,
            "voice": False,
        },
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
    try:
        _validate_queries(queries)
    except ValueError as exc:
        print(f"[err] invalid query labels: {exc}", file=sys.stderr)
        return 2
    actor_context = build_memory_actor_context(
        owner_id="quality_bench",
        companion_id="quality_bench",
        memory_realm_id=args.user_id,
        device_id="quality_bench",
        session_id="quality_bench",
    ).model_dump(mode="json")

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = _REPORTS / f"memory_quality_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "agent_runner.log"
    settings_path = out_dir / "spawn_settings.yaml"
    palace_root = out_dir / "palaces"
    palace_root.mkdir(parents=True, exist_ok=True)

    # Checked before the reset, which is itself the first thing needing a broker.
    # Without this the failure surfaces 45s later as "the agent never bound its
    # port", which is true and useless.
    require_nats(args.nats_url)

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
        steward_mode=args.steward_mode,
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
                f"[wait] waiting for the turn consumer to drain, then for "
                f"triples >= {args.min_triples} and fragments >= "
                f"{args.min_fragments} ..."
            )
            ok, stats, fragments, ingest_s = await _wait_for_ingestion(
                session,
                nats_url=args.nats_url,
                user_id=args.user_id,
                target_triples=args.min_triples,
                target_fragments=args.min_fragments,
                timeout_s=args.ingest_timeout,
            )
            if not ok:
                # A warning on stderr does not reach the report, so every number
                # after it looked like a measurement of a fully ingested corpus.
                # It is not: with a slow model this timeout can fire having
                # processed a fraction of the turns, and the resulting accuracy
                # then measures the wait budget rather than the memory.
                print(
                    f"[FAIL] ingestion did not finish within "
                    f"{args.ingest_timeout:.0f}s. Reached kg_stats={stats}, "
                    f"fragments={fragments}; the turn consumer still had a "
                    f"backlog, or output stayed below the floor "
                    f"(triples >= {args.min_triples}, "
                    f"fragments >= {args.min_fragments}).\n"
                    f"        Any quality number from this run would describe an "
                    f"incompletely ingested corpus.\n"
                    f"        Raise --ingest-timeout, or check how long the steward's "
                    f"LLM is taking per turn.",
                    file=sys.stderr,
                )
                if not args.allow_partial_ingestion:
                    return 2
            require_expected_embedder(
                palace_root,
                configured=(get_memory_settings().mempalace.embedding_model or "").strip(),
            )

            print(
                f"[wait] palace state after {ingest_s:.1f}s: "
                f"entities={stats.get('entities')}, "
                f"triples_total={stats.get('triples_total')}, "
                f"fragments={fragments}"
            )

            # 4) Run the query battery. With --with-consolidator we run it
            #    TWICE — once now (baseline, no themes) and once after the
            #    consolidator lands Wing_Theme drawers — to quantify the
            #    Phase 4 gain in a single, same-palace A/B.
            baseline_results, raw_responses = await _run_battery(
                session,
                queries,
                context=actor_context,
                label="baseline" if args.with_consolidator else "all",
            )
            agg = _aggregate(baseline_results)
            results = baseline_results  # default reporting target

            consolidator_seconds = 0.0
            theme_count = 0
            themed_agg: dict[str, Any] | None = None
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
                # Wait for the cmd subscriber to apply theme writes.
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

                # Re-run the same battery now that themes exist.
                themed_results, raw_responses = await _run_battery(
                    session,
                    queries,
                    context=actor_context,
                    label="themed",
                )
                themed_agg = _aggregate(themed_results)
                results = themed_results   # report the themed pass as primary
                agg = themed_agg

        # 5) Render summary.
        md = _render_markdown(
            agg=agg,
            results=results,
            kg_stats=stats,
            fragments=fragments,
            corpus_size=len(corpus),
            ingest_seconds=ingest_s,
        )
        if themed_agg is not None:
            md += "\n" + _render_ab_comparison(
                baseline=_aggregate(baseline_results),
                themed=themed_agg,
                theme_count=theme_count,
                consolidator_seconds=consolidator_seconds,
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
                        "with_consolidator": args.with_consolidator,
                    },
                    "kg_stats_at_query_time": stats,
                    "fragments_at_query_time": fragments,
                    "ingest_seconds": ingest_s,
                    "consolidator_seconds": consolidator_seconds,
                    "theme_count": theme_count,
                    "aggregate": agg,
                    "baseline_aggregate": _aggregate(baseline_results),
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
        if themed_agg is not None:
            bo = _aggregate(baseline_results)["overall"]
            print(
                f"  A/B overall: baseline {bo['correct_pct']}% → "
                f"themed {o['correct_pct']}% "
                f"(Δ {o['correct_pct'] - bo['correct_pct']:+.1f}pp); "
                f"themes={theme_count}"
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
    parser.add_argument(
        "--steward-mode",
        choices=("llm", "rules"),
        default="llm",
        help="llm for quality evaluation; rules for deterministic pipeline smoke",
    )
    parser.add_argument("--min-triples", type=int, default=18,
                        help="Wait until kg_stats.triples_total reaches this")
    parser.add_argument("--min-fragments", type=int, default=25,
                        help="Wait until eidolon_memory_list returns at least this many fragments")
    parser.add_argument(
        "--allow-partial-ingestion",
        action="store_true",
        help=(
            "Report quality even when ingestion did not drain. Off by default: "
            "such a number measures the wait budget, not the memory."
        ),
    )
    # 40 turns are processed strictly one at a time (the subscriber awaits each
    # handler), and one steward call measured 22.9s — so a full corpus needs ~15
    # minutes. The first complete run took 1192.7s against a 1200s budget: seven
    # seconds of margin, i.e. the next run would have failed on variance alone.
    # 1800 gives roughly half again, which is the margin this should have started
    # with rather than one derived from a single observation.
    parser.add_argument("--ingest-timeout", type=float, default=1800.0,
                        help="Max seconds to wait for ingestion (LLM steward is slow)")
    parser.add_argument("--with-consolidator", action="store_true",
                        help="Run eidolon-memory-consolidator after ingestion so the "
                             "[主题] section is populated for the query battery")
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
