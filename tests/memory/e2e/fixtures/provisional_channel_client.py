"""Use the adjacent Channel environment to drive the existing journey's real RPC."""

import asyncio
import json
import sys

from eidolon.livekit.agent.eidolon_agent_rpc.grpc_llm import EidolonAgentGrpcLlm
from livekit.agents.llm import ChatContext, ChatMessage


async def main():
    request = json.loads(sys.stdin.read())
    adapter = EidolonAgentGrpcLlm(
        target=request["target"],
        device_token=lambda: request["runtime_token"],
        conversation_id="room-session:room",
    )
    try:
        adapter.set_turn_trace_id(request["trace"])
        if request["observation_token"]:
            adapter.set_speaker_observation(request["observation_token"])
        context = ChatContext()
        context.items.append(ChatMessage(role="user", content=[request["text"]]))
        chunks = []
        async for chunk in adapter.chat(chat_ctx=context):
            if chunk.delta and chunk.delta.content:
                chunks.append(chunk.delta.content)
        assert "".join(chunks) == "accepted"
    finally:
        await adapter.aclose()


asyncio.run(main())
