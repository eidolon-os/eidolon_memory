#!/usr/bin/env python3
"""Delete all active-Realm memory history while preserving Realm registry rows."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import nats
from eidolon_sdk.memory import (
    conversation_turn_subject,
    memory_command_subject,
    memory_sync_subject,
)
from nats.js.errors import NotFoundError

from eidolon.memory.config.memory_settings import (
    load_memory_settings,
    resolve_dlq_log_path,
    resolve_log_dir,
    resolve_run_dir,
)
from eidolon.memory.infrastructure.history_reset import (
    CONFIRMATION,
    acquire_realm_reset_locks,
    active_realm_ids,
    clear_palace_contents,
    clear_realm_process_temp,
    clear_repair_archives,
    realm_registry_digest,
    truncate_memory_history_files,
    validate_reset_scope,
)
from eidolon.memory.infrastructure.nats.names import memory_consumer_name


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--registry-db", type=Path, required=True)
    parser.add_argument("--palaces-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--confirm", required=True)
    return parser.parse_args()


async def _purge_nats(
    nats_url: str,
    stream: str,
    durable_prefix: str,
    realm_ids: list[str],
) -> dict[str, list[str]]:
    client = await nats.connect(nats_url)
    deleted_consumers: list[str] = []
    purged_subjects: list[str] = []
    try:
        js = client.jetstream()
        for realm_id in realm_ids:
            for role in ("turn", "cmd", "sync"):
                name = memory_consumer_name(durable_prefix, realm_id, role=role)
                try:
                    await js.delete_consumer(stream, name)
                    deleted_consumers.append(name)
                except NotFoundError:
                    pass
            for subject in (
                conversation_turn_subject(realm_id),
                memory_command_subject(realm_id),
                memory_sync_subject(realm_id),
            ):
                await js.purge_stream(stream, subject=subject)
                purged_subjects.append(subject)
    finally:
        await client.close()
    return {"deleted_consumers": deleted_consumers, "purged_subjects": purged_subjects}


def main() -> None:
    args = _args()
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"refusing reset: --confirm must equal {CONFIRMATION!r}")

    settings = load_memory_settings(args.settings)
    registry_before = realm_registry_digest(args.registry_db)
    realm_ids = active_realm_ids(args.registry_db)
    palace_by_realm = validate_reset_scope(args.palaces_root, realm_ids)
    process_tmp_root = args.palaces_root.resolve() / ".process-tmp"

    with acquire_realm_reset_locks(resolve_run_dir(settings), realm_ids):
        nats_result = asyncio.run(
            _purge_nats(
                settings.nats.url,
                settings.nats.stream,
                settings.nats.durable_prefix,
                realm_ids,
            )
        )
        removed = {
            realm_id: clear_palace_contents(palace_by_realm[realm_id])
            for realm_id in realm_ids
        }
        removed_archives = clear_repair_archives(
            args.palaces_root,
            list(palace_by_realm.values()),
        )
        cleared_tmp = [
            realm_id
            for realm_id in realm_ids
            if clear_realm_process_temp(process_tmp_root, realm_id)
        ]
        truncated = truncate_memory_history_files(
            resolve_log_dir(settings),
            resolve_dlq_log_path(settings),
        )

    registry_after = realm_registry_digest(args.registry_db)
    if registry_after != registry_before:
        raise RuntimeError("Realm registry changed during memory reset")
    report = {
        "schema_version": 1,
        "completed_at": datetime.now(UTC).isoformat(),
        "realms_preserved": realm_ids,
        "registry_sha256_before": registry_before,
        "registry_sha256_after": registry_after,
        "palace_directories_preserved": {
            realm_id: str(path) for realm_id, path in palace_by_realm.items()
        },
        "removed_top_level_entries": removed,
        "removed_repair_archives": removed_archives,
        "cleared_process_tmp": cleared_tmp,
        "truncated_memory_history_files": truncated,
        "nats": nats_result,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
