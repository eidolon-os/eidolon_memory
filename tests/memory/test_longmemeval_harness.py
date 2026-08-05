"""The LongMemEval harness duplicates the model table, so the copies must agree.

``benchmarks/suites/bench_longmemeval.py`` carries its own encoder and its own copy
of the repo / pooling / prefix rules rather than importing
``domain/embedding_port.py``. That is deliberate and the reason is in its module
docstring: it compares *models* against another project's published figures, so its
numbers have to be attributable to a model and not to whatever shape our adapter had
that day. A cross-project comparison that moves when we rename a method is not a
comparison.

The cost is drift. Pooling and the two prefixes are exactly the fields whose being
wrong raises nothing and only ranks worse — a first probe of bge-small once measured
0.009 of separation between a relevant and an irrelevant fragment for that reason.
Drift there would silently change a number we published against MemPalace's 96.6%.

So the copies are compared here. This test is the whole justification for allowing
the duplication.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SUITES = _REPO_ROOT / "benchmarks" / "suites"
if str(_SUITES) not in sys.path:
    sys.path.insert(0, str(_SUITES))


def _harness_models() -> dict[str, dict]:
    from bench_longmemeval import MODELS

    return MODELS


def _production_models():
    from eidolon.memory.domain.embedding_port import LOCAL_EMBEDDING_MODELS

    return LOCAL_EMBEDDING_MODELS


def test_the_harness_knows_every_model_production_does() -> None:
    """A model added to production but not here cannot be benchmarked.

    Not an error in itself — but silently unbenchmarkable is worse than a failing
    test, because the gap only shows up as "we never measured that one".
    """

    missing = set(_production_models()) - set(_harness_models())

    assert not missing, f"bench_longmemeval.MODELS is missing: {sorted(missing)}"


def test_the_harness_invents_no_models() -> None:
    """A model here but not in production would be a figure for something we do
    not ship."""

    from bench_longmemeval import MEMPALACE_MODELS

    # MemPalace's own two are reachable through the harness but are not ours to
    # implement, so they live in their own tuple rather than in MODELS.
    extra = set(_harness_models()) - set(_production_models()) - set(MEMPALACE_MODELS)

    assert not extra, f"bench_longmemeval.MODELS has entries production lacks: {sorted(extra)}"


@pytest.mark.parametrize("field", ["repo", "pooling", "query_prefix", "document_prefix"])
def test_the_copies_agree_on(field: str) -> None:
    """The four fields that decide what a vector means.

    ``repo`` picks the weights. ``pooling`` picks which position of the hidden
    state is the sentence — CLS for BGE, mean for E5, the last real token for a
    decoder. The prefixes are mandatory for E5 and an instruction for Qwen3. Get any
    of them wrong and the model still returns a vector, of the right width, that
    ranks badly.
    """

    harness = _harness_models()
    disagreements = []
    for name, spec in _production_models().items():
        theirs = harness.get(name)
        if theirs is None:
            continue  # covered by the membership test above
        # The harness omits empty prefixes rather than spelling them out, so an
        # absent key and "" are the same statement.
        mine = theirs.get(field, "")
        production = getattr(spec, field)
        if mine != production:
            disagreements.append(f"{name}: harness={mine!r} production={production!r}")

    assert not disagreements, (
        f"{field} has drifted between bench_longmemeval.MODELS and "
        f"LOCAL_EMBEDDING_MODELS:\n  " + "\n  ".join(disagreements)
    )


def test_the_harness_does_not_import_the_service() -> None:
    """Self-contained is the point, so it is asserted rather than intended.

    An ``import eidolon.memory`` here would reattach the benchmark to code that is
    still being refactored, which is what this arrangement exists to avoid.
    """

    import ast

    source = (_REPO_ROOT / "benchmarks/suites/bench_longmemeval.py").read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        offenders += [f"line {node.lineno}: {m}" for m in modules if m.startswith("eidolon")]

    assert not offenders, (
        "bench_longmemeval imports the service, so its figures would depend on the "
        "adapter's current shape: " + "; ".join(offenders)
    )
