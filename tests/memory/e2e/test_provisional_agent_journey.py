"""Cross-repository journey using each service's own existing environment."""

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_data_agent_outbox_and_real_memory_sources(live_agent_runner, tmp_path):
    agent = Path(__file__).resolve().parents[4] / "eidolon_agent"
    python = agent / ".venv/bin/python"
    if not python.exists():
        pytest.skip("adjacent Agent test environment is required for cross-repository journey")
    script = Path(__file__).parent / "fixtures/provisional_agent_journey.py"

    async def run(phase, *args):
        process = await asyncio.create_subprocess_exec(
            str(python),
            str(script),
            phase,
            str(tmp_path),
            *args,
            cwd=agent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        return json.loads(stdout.decode().splitlines()[-1])

    seeded = await run("seed")
    handle = live_agent_runner(
        user_id=seeded["realm_id"],
        management_only=True,
        provisional_ingest=True,
        provisional_recall=True,
        env_overrides={"EIDOLON_MEMORY_API_TOKEN": "isolated-person-journey-token"},
    )
    result = await run("verify", handle.nats_url)
    assert result["passed"] and result["read_count"] == 20
    print(json.dumps(result, sort_keys=True))
