#!/usr/bin/env python3
"""Print the effective memory runtime configuration."""

from __future__ import annotations

from eidolon.memory.config.memory_settings import (
    get_memory_settings,
    resolve_log_dir,
    resolve_run_dir,
)
from eidolon.memory.config.palace_directory import resolve_palaces_root
from eidolon.memory.config.users import resolve_admin_api_url
from eidolon.memory.config.registry import load_users_config
from eidolon_memory_contracts import conversation_turn_stream_pattern


def main() -> None:
    settings = get_memory_settings()
    print("palaces_root:", resolve_palaces_root(settings))
    print("log_dir:", resolve_log_dir(settings))
    print("run_dir:", resolve_run_dir(settings))
    print("admin_registry:", f"{resolve_admin_api_url(settings)}/api/users/registry")
    try:
        ucfg = load_users_config(settings)
        for u in ucfg.users:
            print(f"  {u.id:14s} enabled={u.enabled} port={u.port}")
    except Exception as exc:
        print(f"  (admin registry read failed: {exc})")
    print("backend:", "mempalace-python")
    print("nats.url:", settings.nats.url)
    print("nats.stream:", settings.nats.stream)
    print("nats.subject_base:", settings.nats.conversation_turn_subject_base)
    print("nats.stream_pattern:", conversation_turn_stream_pattern())
    print("nats.durable_prefix:", settings.nats.durable_prefix)
    print("mcp_http.default_url:", settings.mcp_http.base_url())
    print("chromadb.synchronous:", settings.chromadb.synchronous)
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
