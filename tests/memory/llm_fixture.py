"""A blocking provider SDK loaded only by real model subprocesses."""

import os
from pathlib import Path

import pytest

SDK_SOURCE = """
import json
import os
import time
from pathlib import Path

events = Path(os.environ["TEST_LLM_EVENTS"])
def event(name):
    with events.open("a") as f:
        f.write(name + "\\n")

event("import_started")
time.sleep(float(os.environ.get("TEST_LLM_IMPORT_DELAY", "0")))
event("import_finished")

async def acompletion(**kwargs):
    event("call_started")
    print("SDK stdout must not corrupt the response pipe", flush=True)
    model = kwargs["model"]
    if model == "exit":
        os._exit(9)
    if model == "error":
        raise RuntimeError("credential-must-not-be-returned")
    if model == "oversized":
        return {"choices": [{"message": {"content": "x" * (5 * 1024 * 1024)}}]}
    time.sleep(float(os.environ.get("TEST_LLM_CALL_DELAY", "0")))
    event("call_finished")
    body = {
        "should_write": True,
        "reason": "test provider",
        "fragments": [{
            "wing": "Wing_Life", "room": "tea", "memory_type": "preference",
            "content": "用户喜欢乌龙茶", "evidence_quote": "我喜欢乌龙茶",
            "importance": 4, "confidence": 0.95,
        }],
    }
    return {
        "choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}],
        "provider_pid": os.getpid(), "model": model, "api_base": kwargs.get("api_base"),
    }
"""


@pytest.fixture(name="isolated_model_sdk")
def model_sdk_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sdk = tmp_path / "model_sdk"
    sdk.mkdir()
    (sdk / "litellm.py").write_text(SDK_SOURCE)
    events = sdk / "events"
    monkeypatch.setenv("TEST_LLM_EVENTS", str(events))
    monkeypatch.setenv("PYTHONPATH", str(sdk) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return events
