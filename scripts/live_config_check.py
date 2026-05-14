#!/usr/bin/env python3
"""Print the effective YAML-backed memory runtime configuration."""

from __future__ import annotations

from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory


def main() -> None:
    settings = get_memory_settings()
    resolved = resolve_palace_directory(settings)
    print("palace_path (resolved):", resolved)
    print("palace_path (yaml/runtime):", settings.runtime.palace_path or "<empty>")
    print("backend:", "mempalace-python")
    print("nats.url:", settings.nats.url)
    print("nats.stream:", settings.nats.stream)
    print("nats.subject:", settings.nats.subject)
    print("nats.durable:", settings.nats.durable)
    print("steward.mode:", settings.steward.mode)
    print("llm.model:", settings.llm.model or "<missing>")
    print("llm.base_url:", settings.llm.base_url or "<missing>")
    if settings.llm.api_key:
        key_status = "<set in yaml llm.api_key>"
    elif settings.llm.api_key_env.startswith(("sk-", "sess-")):
        key_status = "<set as literal in yaml llm.api_key_env>"
    else:
        key_status = f"<from env {settings.llm.api_key_env}>"
    print("llm.api_key:", key_status)


if __name__ == "__main__":
    main()
