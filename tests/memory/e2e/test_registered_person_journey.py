"""G3 for registered people across Data, Agent and Memory (design §4.1).

The journey itself runs in the adjacent Agent environment; this test owns the
broker and both Memory workers it writes to: the registered Person's Realm and
the household Realm.
"""

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_registered_person_chains_agree_on_who_spoke(live_agent_runner, tmp_path):
    agent = Path(__file__).resolve().parents[4] / "eidolon_agent"
    python = agent / ".venv/bin/python"
    if not python.exists():
        pytest.skip("adjacent Agent test environment is required for the G3 journey")
    script = Path(__file__).parent / "fixtures/registered_person_journey.py"

    async def run(phase, *args):
        process = await asyncio.create_subprocess_exec(
            str(python), str(script), phase, str(tmp_path), *args, cwd=agent,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()[-6000:] + stdout.decode()[-2000:]
        return json.loads(stdout.decode().splitlines()[-1])

    realms = await run("seed")
    workers = {
        realms["person_realm"]: live_agent_runner(
            user_id=realms["person_realm"], steward_mode="test-verbatim", realm_scope="person"),
        realms["household_realm"]: live_agent_runner(
            user_id=realms["household_realm"], steward_mode="test-verbatim", realm_scope="owner"),
    }
    nats_url = next(iter(workers.values())).nats_url
    urls = {realm: [w.agent_mcp_url, w.mcp_url] for realm, w in workers.items()}
    result = await run("verify", nats_url, json.dumps(realms), json.dumps(urls))
    assert result["passed"]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
