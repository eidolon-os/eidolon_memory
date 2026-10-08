"""Paired prompt probe; sends only three turns from the authorized test corpus."""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from eidolon_memory_contracts import (  # noqa: E402
    ConversationTurnPayload,
    build_memory_actor_context,
)

from eidolon.memory.application.steward.llm import LiteLLMSteward  # noqa: E402
from eidolon.memory.config.memory_settings import get_memory_settings  # noqa: E402
from eidolon.memory.domain.predicates import fact_sentence  # noqa: E402

OUT = Path(__file__).resolve().parent
PROMPT = "eidolon/memory/config/prompts/memory_steward.md"
BASE = "5cb9e62"


async def main():
    old = subprocess.check_output(["git", "show", f"{BASE}:{PROMPT}"], cwd=ROOT)
    old_path = OUT / "before-prompt.md"
    old_path.write_bytes(old)
    paths = {"before": old_path, "after": OUT / "prompt-run1.md"}
    settings = get_memory_settings()
    corpus = [
        json.loads(line)
        for line in (ROOT / "tests/memory/e2e/fixtures/companion_corpus.jsonl")
        .read_text()
        .splitlines()
    ]
    samples = [row for row in corpus if row["turn_id"] in {"c-001", "c-021", "c-023"}]
    report = {
        "model": settings.llm.model,
        "temperature": settings.llm.temperature,
        "prompt_sha256": {
            key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()
        },
        "results": [],
    }
    for repeat in range(2):
        for variant in ["before", "after"] if repeat == 0 else ["after", "before"]:
            configured = settings.model_copy(
                update={
                    "steward": settings.steward.model_copy(
                        update={"prompt_template_path": str(paths[variant])}
                    )
                }
            )
            steward = LiteLLMSteward(configured)
            try:
                for sample in samples:
                    turn = ConversationTurnPayload(
                        turn_id=sample["turn_id"],
                        timestamp="2026-10-05T14:58:00Z",
                        context=build_memory_actor_context(
                            owner_id="quality_bench",
                            companion_id="quality_bench",
                            memory_realm_id="quality-language-probe",
                            device_id="quality_bench",
                            session_id="quality_bench",
                        ),
                        user_text=sample["user_text"],
                        assistant_text="",
                    )
                    row = {
                        "variant": variant,
                        "repeat": repeat,
                        "turn_id": turn.turn_id,
                        "user_text": turn.user_text,
                    }
                    try:
                        decision = await steward.decide(turn)
                        row["decision"] = decision.model_dump(mode="json")
                        row["projected_sentences"] = [
                            fact_sentence(t.subject, t.predicate, t.object)
                            for t in decision.triples
                        ]
                    except Exception as exc:
                        row["error"] = type(exc).__name__
                    report["results"].append(row)
                    (OUT / "probe-results.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                    )
                    print(
                        variant,
                        repeat,
                        turn.turn_id,
                        row.get("projected_sentences", row.get("error")),
                        flush=True,
                    )
            finally:
                await steward.aclose()
    old_path.unlink()


if __name__ == "__main__":
    asyncio.run(main())
