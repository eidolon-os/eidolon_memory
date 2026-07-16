from __future__ import annotations

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.forget import (
    extract_privacy_target,
    find_forget_candidates,
)
from eidolon.memory.application.steward.common import apply_privacy_actions
from eidolon.memory.domain.steward import PrivacyAction
from eidolon.memory.domain.wire import MemoryWireRecord

SPACE = "default.alice.default"


def _seed(backend: FakeMemoryBackend, key: str, text: str) -> None:
    backend.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key=key,
        value=text,
        metadata={"memory_space_id": SPACE, "wing": "Wing_Profile"},
    )


def test_extract_privacy_target_removes_command_language() -> None:
    assert extract_privacy_target("请帮我删掉我喜欢喝绿茶的记忆") == "我喜欢喝绿茶"
    assert extract_privacy_target("忘掉我住在上海") == "我住在上海"


async def test_find_forget_candidates_is_tenant_scoped_and_content_based() -> None:
    backend = FakeMemoryBackend()
    _seed(backend, "drawer_tea", "用户现在喜欢乌龙茶，不再喝绿茶")
    _seed(backend, "drawer_city", "用户住在常州")
    backend.docs["other::drawer_tea"] = MemoryWireRecord(
        memory_space_id="other",
        key="drawer_tea",
        value="绿茶",
        metadata={"memory_space_id": "other"},
    )

    candidates = await find_forget_candidates(backend, SPACE, "绿茶")

    assert [candidate.key for candidate in candidates] == ["drawer_tea"]


async def test_privacy_delete_removes_candidate_and_verifies_invisible() -> None:
    backend = FakeMemoryBackend()
    _seed(backend, "drawer_tea", "用户现在喜欢乌龙茶，不再喝绿茶")
    _seed(backend, "drawer_city", "用户住在常州")

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="请删掉绿茶的记忆",
                reason="explicit user request",
            )
        ],
    )

    assert result.deleted_keys == ["drawer_tea"]
    assert await backend.get(SPACE, "drawer_tea") is None
    assert await backend.get(SPACE, "drawer_city") is not None

