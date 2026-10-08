"""Verify the five authorized fixture turns that hit the first run's output errors."""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from eidolon_memory_contracts import (  # noqa: E402
    ConversationTurnPayload,
    build_memory_actor_context,
)

from eidolon.memory.application.steward.llm import LiteLLMSteward  # noqa: E402
from eidolon.memory.config.memory_settings import get_memory_settings  # noqa: E402


async def main():
    service = LiteLLMSteward(get_memory_settings())
    context = build_memory_actor_context(
        owner_id="quality_bench",
        companion_id="quality_bench",
        memory_realm_id="claims-retry-check",
        device_id="quality_bench",
        session_id="quality_bench",
    )
    rows = []
    try:
        for line in (
            (ROOT / "tests/memory/e2e/fixtures/companion_corpus.jsonl").read_text().splitlines()
        ):
            item = json.loads(line)
            if item["turn_id"] not in {"c-017", "c-019", "c-031", "c-032", "c-033"}:
                continue
            start = time.perf_counter()
            row = {"turn_id": item["turn_id"]}
            try:
                decision = await service.decide(
                    ConversationTurnPayload(
                        context=context,
                        turn_id=item["turn_id"],
                        timestamp="2026-10-08T10:00:00Z",
                        user_text=item["user_text"],
                        assistant_text="",
                    )
                )
                row["decision"] = decision.model_dump(mode="json")
            except Exception as exc:
                row["error"] = str(exc)
            row["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
            rows.append(row)
            Path(__file__).with_name("retry-cases.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
            )
            print(row["turn_id"], row.get("error", "ok"), row["elapsed_ms"], flush=True)
    finally:
        await service.aclose()


if __name__ == "__main__":
    asyncio.run(main())
