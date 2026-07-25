"""Product routing contract for verbatim confirmed claims."""

from __future__ import annotations

import pytest

from eidolon.memory.application.claim_routing import route_explicit_claim


@pytest.mark.parametrize(
    ("claim", "wing", "memory_type"),
    [
        ("明天我要去北京", "Wing_Event", "event"),
        ("我家住在北京", "Wing_Profile", "profile"),
        ("我工作在常州", "Wing_Work", "work"),
        ("我喜欢乌龙茶", "Wing_Life", "preference"),
        ("我的目标是三年内开一家店", "Wing_Future", "goal"),
    ],
)
def test_route_explicit_claim_without_rewriting(
    claim: str,
    wing: str,
    memory_type: str,
) -> None:
    route = route_explicit_claim(claim)

    assert route.wing == wing
    assert route.memory_type == memory_type
