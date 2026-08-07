from __future__ import annotations

import pytest
from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.forget import (
    DEFAULT_FORGET_PAGE_SIZE,
    ForgetResolutionLimitExceeded,
    extract_privacy_target,
    find_forget_candidates,
    source_turns_for_drawers,
)
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.application.steward.common import apply_privacy_actions
from eidolon.memory.domain.steward import PrivacyAction
from eidolon.memory.domain.wire import MemoryWireRecord

SPACE = "default.alice.default"


def test_default_forget_page_size_matches_55k_memory_latency_curve() -> None:
    assert DEFAULT_FORGET_PAGE_SIZE == 5_000


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


def _seed_with_turn(backend, key: str, text: str, *, turn_id: str) -> None:
    backend.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key=key,
        value=text,
        metadata={
            "memory_space_id": SPACE,
            "wing": "Wing_Profile",
            "source_turn_id": turn_id,
        },
    )


@pytest.fixture
def graph(tmp_path):
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
    from eidolon.memory.domain.space_lock import SpaceLock

    made = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE, lock=SpaceLock())
    yield made
    made.close()


async def test_a_spoken_delete_reaches_the_graph(graph) -> None:
    """The path a person actually takes to be forgotten.

    Saying "忘掉…" runs through the steward, which needs no tool call and no
    confirmation round trip — so it is the common case, and it was the half of
    the defect that stayed broken longest: the drawer went and the triple was
    still transcribed into the next prompt.
    """

    backend = FakeMemoryBackend()
    _seed_with_turn(backend, "drawer_tea", "用户喜欢绿茶", turn_id="turn-tea")
    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶",
        audience="owner", source_turn_id="turn-tea",
    )

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
        kg=graph,
    )

    assert result.deleted_keys == ["drawer_tea"]
    assert result.statements_forgotten == 1
    assert (await graph.stats())["triples_total"] == 0


async def test_a_spoken_archive_ends_the_triple_without_deleting_it(graph) -> None:
    backend = FakeMemoryBackend()
    _seed_with_turn(backend, "drawer_tea", "用户喜欢绿茶", turn_id="turn-tea")
    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶",
        audience="owner", source_turn_id="turn-tea",
    )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="archive_topic",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        kg=graph,
    )

    assert result.archived_keys == ["drawer_tea"]
    assert result.statements_forgotten == 1
    stats = await graph.stats()
    assert stats["triples_total"] == 1
    assert stats["triples_active"] == 0


async def test_an_ambiguous_delete_is_archived_rather_than_abandoned(graph) -> None:
    """It used to find the memories and then decline, telling nobody.

    A delete matching several drawers recorded ``confirmation_required`` and did
    nothing. Nobody was ever asked: this runs on the bus about 23 seconds after
    the companion has already said "好的", so the path has no way to put a
    question to anyone, and the caller discarded the result object anyway. The
    person asked to be forgotten, was told yes, and nothing happened.

    Archiving is what "forget this" means to them — it stops being recalled,
    immediately, on every match — and it is reversible, so an over-broad match
    costs nothing that cannot be undone. No score margin, no ranking tie-break:
    those need a constant nobody can calibrate, and getting it wrong deletes
    something the person wanted.
    """

    backend = FakeMemoryBackend()
    for index, key in enumerate(("drawer_tea_1", "drawer_tea_2")):
        _seed_with_turn(backend, key, f"用户喜欢绿茶{index}", turn_id=f"turn-{index}")
        await graph.add_triple(
            subject="用户", predicate="likes", object=f"绿茶{index}",
            audience="owner", source_turn_id=f"turn-{index}",
        )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        kg=graph,
    )

    assert result.deleted_keys == [], "nothing irreversible on a guess"
    assert sorted(result.archived_keys) == ["drawer_tea_1", "drawer_tea_2"]
    # Recorded distinctly, so an operator can tell "the user asked to archive"
    # from "the user asked to delete and we chose the reversible half".
    assert result.downgraded_to_archive["绿茶"] == result.archived_keys
    assert result.confirmation_required, "the ambiguity is still on the record"

    # The graph half matches: ended, not removed.
    stats = await graph.stats()
    assert stats["triples_active"] == 0, "the statements stopped being recalled"
    assert stats["triples_total"] == 2, "and none of them were deleted"


async def test_a_space_without_a_graph_still_forgets_its_drawers() -> None:
    backend = FakeMemoryBackend()
    _seed_with_turn(backend, "drawer_tea", "用户喜欢绿茶", turn_id="turn-tea")

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
    assert result.statements_forgotten == 0


def test_every_caller_hands_the_graph_to_the_privacy_handler() -> None:
    """A fourth call site that forgets ``kg=`` restores the bug silently.

    ``kg`` has to default to ``None`` — a space can genuinely have no graph — so
    nothing at runtime distinguishes "no graph here" from "forgot to pass it".
    That is exactly how this survived: the parameter did not exist, every caller
    was consistent, and consistency looked like correctness.
    """

    import inspect

    from eidolon.memory.application import turn_processor
    from eidolon.memory.application.steward import common, llm, rules

    for module in (turn_processor, llm, rules):
        source = inspect.getsource(module)
        for index, line in enumerate(source.splitlines()):
            if "apply_privacy_actions(" not in line or "def " in line:
                continue
            call = "\n".join(source.splitlines()[index : index + 8])
            assert "kg=" in call, f"{module.__name__} calls it without a graph"

    assert "kg" in inspect.signature(common.apply_privacy_actions).parameters


async def test_the_turn_lookup_uses_one_round_trip_not_one_per_drawer() -> None:
    """A privacy command carries up to a hundred drawer ids.

    As a loop over ``get`` that was a hundred calls, each crossing the space
    lock, to answer a single question before the deletion could start. Invisible
    on a laptop and not on a four-core board sharing itself with Chroma.
    """

    backend = FakeMemoryBackend()
    for index in range(25):
        _seed_with_turn(
            backend, f"drawer_{index}", f"记忆{index}", turn_id=f"turn-{index % 5}"
        )

    calls = {"get": 0, "get_many": 0}
    original_get = backend.get
    original_many = backend.get_many

    async def _counted_get(space, key):
        calls["get"] += 1
        return await original_get(space, key)

    async def _counted_many(space, keys):
        calls["get_many"] += 1
        return await original_many(space, keys)

    backend.get = _counted_get
    backend.get_many = _counted_many

    turns = await source_turns_for_drawers(
        backend, SPACE, [f"drawer_{i}" for i in range(25)]
    )

    assert calls == {"get": 0, "get_many": 1}
    # Five distinct turns behind twenty-five drawers, deduplicated and ordered.
    assert turns == [f"turn-{i}" for i in range(5)]


async def test_a_backend_without_the_batch_still_answers() -> None:
    """The plural is an optimisation, not a requirement of the port."""

    class _SingularOnly:
        def __init__(self, inner):
            self._inner = inner

        async def get(self, space, key):
            return await self._inner.get(space, key)

    backend = FakeMemoryBackend()
    _seed_with_turn(backend, "drawer_tea", "用户喜欢绿茶", turn_id="turn-tea")

    assert await source_turns_for_drawers(
        _SingularOnly(backend), SPACE, ["drawer_tea", "drawer_gone"]
    ) == ["turn-tea"]


async def test_a_locked_backend_does_not_hide_the_batch() -> None:
    """The wrapper is what production uses, so the fast path must survive it.

    ``LockedBackend`` has no ``__getattr__``: anything it does not declare is
    simply absent, and the caller probing for ``get_many`` would quietly fall
    back to N round trips in every real deployment while the tests — which use a
    bare fake — kept exercising the batch.
    """

    from eidolon.memory.adapters.locked_backend import LockedBackend

    backend = FakeMemoryBackend()
    _seed_with_turn(backend, "drawer_tea", "用户喜欢绿茶", turn_id="turn-tea")
    locked = LockedBackend(backend)

    assert hasattr(locked, "get_many")
    assert await source_turns_for_drawers(locked, SPACE, ["drawer_tea"]) == ["turn-tea"]


async def test_a_fact_that_only_the_graph_holds_can_still_be_forgotten(graph) -> None:
    """The hole the two independent write gates open.

    ``min_importance_to_write`` is 3 and ``min_confidence_to_write`` is 0.6, so an
    ordinary but reliable sentence — "用户喜欢绿茶", importance 2, confidence 0.9 —
    becomes a triple and no drawer. Asking to forget it scanned drawers, found
    nothing, recorded ``unmatched_targets``, and did nothing at all, while the
    statement went on being rendered into every later prompt.
    """

    backend = FakeMemoryBackend()  # no drawers at all
    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶",
        audience="owner", source_turn_id="turn-tea",
    )
    await graph.add_triple(
        subject="用户", predicate="likes", object="咖啡",
        audience="owner", source_turn_id="turn-coffee",
    )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="用户 喜欢 绿茶",
                reason="explicit user request",
            )
        ],
        kg=graph,
    )

    assert result.unmatched_targets == [], "it used to report that it found nothing"
    assert result.statements_forgotten == 1

    remaining = {r.object for r in await graph.query_entity("用户", audiences=("owner",))}
    assert remaining == {"咖啡"}, "only the statement asked about"
    # Reversible, whatever the action said: a sentence match is a looser
    # identification than a stored drawer text, so the reversible half is the
    # right answer to it.
    assert (await graph.stats())["triples_total"] == 2


async def test_forgetting_a_graph_fact_does_not_take_its_turn_mates(graph) -> None:
    """One turn can carry several facts and only one of them was asked about.

    Turn-scoped forgetting is right when a drawer is the thing matched — the
    drawer *is* the turn's fragment. It is wrong when a statement is matched by
    its own sentence, which is why this path forgets by statement.
    """

    backend = FakeMemoryBackend()
    for obj in ("杭州", "北京"):
        await graph.add_triple(
            subject="妈妈" if obj == "杭州" else "爸爸",
            predicate="lives_in", object=obj,
            audience="owner", source_turn_id="one-turn-two-facts",
        )

    await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        actions=[
            PrivacyAction(
                action="delete_request", target="妈妈 住在 杭州", reason="user"
            )
        ],
        kg=graph,
    )

    assert (await graph.stats())["triples_active"] == 1
    assert [r.object for r in await graph.query_entity("爸爸", audiences=("owner",))] == [
        "北京"
    ]
