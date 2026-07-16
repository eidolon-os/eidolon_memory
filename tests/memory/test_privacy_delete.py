from __future__ import annotations

import pytest
from eidolon_sdk.memory import MemoryActorContext

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.forget import (
    ForgetResolutionLimitExceeded,
    extract_privacy_target,
    find_forget_candidates,
)
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
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


async def test_archive_topic_keeps_drawer_but_blocks_recall() -> None:
    backend = FakeMemoryBackend()
    _seed(backend, "drawer_tea", "用户喜欢喝绿茶")

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="archive_topic",
                target="以后别再提绿茶",
                reason="explicit user request",
            )
        ],
    )

    assert result.archived_keys == ["drawer_tea"]
    archived = await backend.get(SPACE, "drawer_tea")
    assert archived is not None
    assert archived.metadata["privacy"] == "do_not_recall"
    context = MemoryActorContext(memory_realm_id=SPACE, memory_space_id=SPACE)
    assert not RecallPolicyRegistry.default().visible(archived, context=context)


async def test_multiple_delete_candidates_require_confirmation_without_mutation() -> None:
    class TrackingBackend(FakeMemoryBackend):
        def __init__(self) -> None:
            super().__init__()
            self.delete_many_calls: list[list[str]] = []

        async def delete_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
            self.delete_many_calls.append(list(keys))
            return await super().delete_many(memory_space_id, keys)

    backend = TrackingBackend()
    _seed(backend, "drawer_tea_1", "绿茶口味偏好")
    _seed(backend, "drawer_tea_2", "不再购买绿茶")

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="删掉绿茶",
                reason="explicit user request",
            )
        ],
    )

    assert result.deleted_keys == []
    assert backend.delete_many_calls == []
    assert [
        item["drawer_id"]
        for item in result.confirmation_required["删掉绿茶"]
    ] == ["drawer_tea_1", "drawer_tea_2"]


async def test_candidate_resolution_pages_through_multi_year_history() -> None:
    backend = FakeMemoryBackend()
    for index in range(12):
        text = "很久以前喜欢绿茶" if index == 10 else f"历史记录 {index}"
        _seed(backend, f"drawer_{index:02d}", text)

    candidates = await find_forget_candidates(
        backend,
        SPACE,
        "绿茶",
        max_scan=20,
        page_size=3,
    )

    assert [candidate.key for candidate in candidates] == ["drawer_10"]


async def test_candidate_resolution_fails_instead_of_silently_truncating_scan() -> None:
    backend = FakeMemoryBackend()
    for index in range(6):
        _seed(backend, f"drawer_{index:02d}", f"历史记录 {index}")

    with pytest.raises(ForgetResolutionLimitExceeded, match="exceeds 5 drawers"):
        await find_forget_candidates(
            backend,
            SPACE,
            "不存在的话题",
            max_scan=5,
            page_size=2,
        )


async def test_candidate_resolution_fails_on_ambiguous_result_overflow() -> None:
    backend = FakeMemoryBackend()
    for index in range(4):
        _seed(backend, f"drawer_{index:02d}", f"绿茶记录 {index}")

    with pytest.raises(ForgetResolutionLimitExceeded, match="more than 3 drawers"):
        await find_forget_candidates(
            backend,
            SPACE,
            "绿茶",
            max_scan=10,
            max_candidates=3,
            page_size=2,
        )
