#!/usr/bin/env python3
"""Publish realistic conversation-turn cases to NATS JetStream."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.nats.turns import JetStreamTurnPublisher


CASES = {
    "emotion_work": {
        "user_text": "我最近因为项目 deadline 很焦虑，但晚上听轻音乐会舒服一点。",
        "assistant_text": "我记住了：最近压力和项目 deadline 有关，轻音乐对你有安抚作用。",
    },
    "relationship": {
        "user_text": "我妈妈最近睡眠不好，我有点担心她，但又不知道怎么劝她去医院。",
        "assistant_text": "这听起来既让你担心也有点无力，我们可以一起想一个温和的沟通方式。",
    },
    "preference_life": {
        "user_text": "我其实不喜欢早上被突然叫醒，最好先放轻音乐再慢慢提醒我。",
        "assistant_text": "好的，以后涉及早晨提醒时，我会尽量用更柔和的方式。",
    },
    "privacy": {
        "user_text": "刚才我说的那件尴尬事不要记住，以后也不要再提。",
        "assistant_text": "明白，我不会把它作为普通记忆保存，也会避免再主动提起。",
    },
}


def build_payload(case_name: str, *, user_id: str, session_id: str) -> ConversationTurnPayload:
    case = CASES[case_name]
    return ConversationTurnPayload(
        turn_id=f"{case_name}-{uuid4().hex}",
        user_id=user_id,
        session_id=session_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        user_text=case["user_text"],
        assistant_text=case["assistant_text"],
        metadata={"source": "scripts/live_publish_turn_case.py", "case": case_name},
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "case",
        choices=sorted(CASES),
        help="Realistic memory case to publish.",
    )
    parser.add_argument("--user-id", default="user_local_001")
    parser.add_argument("--session-id", default="desktop_home_real_test")
    args = parser.parse_args()

    settings = get_memory_settings()
    publisher = JetStreamTurnPublisher.from_memory_settings(settings)
    payload = build_payload(args.case, user_id=args.user_id, session_id=args.session_id)
    await publisher.publish_turn(payload)
    await publisher.close()
    print(f"published case={args.case} turn_id={payload.turn_id}")
    print(f"nats url={settings.nats.url} stream={settings.nats.stream} subject={settings.nats.subject}")


if __name__ == "__main__":
    asyncio.run(main())
