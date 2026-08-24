"""Record what produced a set of measurements.

A latency number without the machine, or a quality number without the embedder,
is not comparable to anything — including a later run of itself. This collects
those facts so they land beside the measurements rather than in someone's memory.

The embedder is read from the palace rather than from configuration on purpose.
Configuration says what a *new* palace would use; the palace records what its
index was actually built with, and the two can disagree. They did: a quality
benchmark once measured minilm while production ran embeddinggemma, because the
config field was left blank and MemPalace applied its own default.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from eidolon.memory.infrastructure.palace_inventory import palace_embedder
from typing import Any


def utc_stamp() -> str:
    """Run identifier and directory name. Sorts chronologically as a string."""

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def code_provenance(repo_root: Path) -> dict[str, Any]:
    """The commit, and whether the tree matched it.

    A dirty tree means the numbers did not come from the recorded sha, so such a
    run must not become a baseline others compare against.
    """

    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    provenance: dict[str, Any] = {
        "git_sha": _git("rev-parse", "HEAD") or "unknown",
        "dirty": bool(_git("status", "--porcelain")),
        "python": platform.python_version(),
    }
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        provenance["branch"] = branch
    try:
        from importlib.metadata import version

        provenance["mempalace"] = version("mempalace")
    except Exception:  # noqa: BLE001 - absent is a fact, not a failure
        pass
    return provenance


def machine_facts() -> dict[str, Any]:
    facts: dict[str, Any] = {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count() or 0,
    }
    processor = platform.processor()
    if processor:
        facts["processor"] = processor
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        facts["memory_gb"] = round(page_size * pages / (1024**3), 1)
    except (ValueError, OSError, AttributeError):
        pass
    return facts


def storage_facts(settings: Any, *, palace_path: Path | None = None) -> dict[str, Any]:
    """Which stores these numbers came from, and the embedder behind them."""

    embedder, source = ("", "unknown")
    if palace_path is not None:
        recorded = palace_embedder(palace_path)
        embedder, source = recorded.name, recorded.source
    if not embedder:
        embedder = (settings.embedding.model or "").strip() or "minilm"
        # Weaker evidence: this is what a new palace would use, which is not
        # necessarily what the one measured was built with.
        source = "configured"

    facts: dict[str, Any] = {
        "vector_backend": settings.mempalace.backend,
        "kg_backend": settings.kg.backend,
        "embedder": embedder,
        "embedder_source": source,
    }
    return facts


def build_manifest(
    *,
    suite: str,
    repo_root: Path,
    settings: Any,
    command: str,
    palace_path: Path | None = None,
    dataset: dict[str, Any] | None = None,
    scale: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "suite": suite,
        "started_at": utc_stamp(),
        "code": code_provenance(repo_root),
        "machine": machine_facts(),
        "storage": storage_facts(settings, palace_path=palace_path),
        "command": command,
    }
    if dataset:
        manifest["dataset"] = dataset
    if scale:
        manifest["scale"] = scale
    if notes:
        manifest["notes"] = notes
    return manifest


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def compare_to_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    p95_regression_ratio: float = 1.2,
    recall_drop_pp: float = 1.0,
) -> list[str]:
    """Regressions worth failing a run over.

    Thresholds are loose deliberately: they catch a change in kind, not the
    variation between two runs on the same laptop. A gate that fires on noise
    gets ignored, and then it catches nothing at all.
    """

    problems: list[str] = []

    for section, values in current.items():
        if not isinstance(values, dict):
            continue
        was = baseline.get(section)
        if not isinstance(was, dict):
            continue

        before, after = was.get("p95"), values.get("p95")
        if isinstance(before, int | float) and isinstance(after, int | float) and before > 0:
            if after > before * p95_regression_ratio:
                problems.append(
                    f"{section}: p95 {before:.4f} → {after:.4f} "
                    f"(+{(after / before - 1) * 100:.0f}%)"
                )

        before, after = was.get("recall_at_5"), values.get("recall_at_5")
        if isinstance(before, int | float) and isinstance(after, int | float):
            if after < before - recall_drop_pp / 100:
                problems.append(
                    f"{section}: R@5 {before:.3f} → {after:.3f} "
                    f"(-{(before - after) * 100:.1f}pp)"
                )

    return problems
