# Benchmarks

A number here is worth something only if someone else can get it again. So every
run records what produced it — the commit, the machine, the embedder, which
backend — and writes that beside the measurements rather than in a commit
message.

## Layout

```
benchmarks/
  suites/          what to run, as YAML
  baselines/       selected runs kept in git, for regression comparison
  runs/            every run's output (gitignored)
    <suite>/<UTC timestamp>/
      manifest.json    what produced these numbers
      metrics.json     the measurements
      summary.md       readable form
  BENCHMARKS.md    the published results
```

## Running one

```bash
uv run python -m scripts.benchmark.run_suite --suite latency_chat
uv run python -m scripts.benchmark.run_suite --suite latency_chat --profile cloud
```

The suite says what to measure; `--profile` says against which storage. Both end
up in the manifest, because a latency number without the backend attached is
not comparable to anything.

## What a manifest has to record

Not bureaucracy — each field is one way a number can turn out to mean something
different than it appears:

| Field | Why it changes the result |
|---|---|
| `git_sha`, `dirty` | A dirty tree means the code is not the commit |
| `machine` (cpu, cores, memory) | Latency is mostly a property of the host |
| `embedder` | minilm and embeddinggemma retrieve differently; a quality number without it is meaningless |
| `vector_backend`, `kg_backend` | A file and a server have different latency floors |
| `dataset`, `split`, `seed` | Especially for quality: a number on the tuning split is not a number on held-out data |
| `command` | So the run can be repeated exactly |

The embedder is read from the palace rather than from configuration, because
what matters is the one the index was built with — that is the mistake that made
an earlier quality benchmark measure minilm while production ran
embeddinggemma.

## Regression comparison

`baselines/` holds selected runs. A suite compares against the matching baseline
and fails on a **>20% p95 regression** or a **>1pp drop in R@5**. The thresholds
are loose on purpose: they catch a change in kind, not machine-to-machine
variation, and a tighter gate would be ignored within a week.

## Quality suites and honesty

Retrieval recall and end-to-end answer accuracy are different measurements, and
putting one next to the other is how benchmark tables mislead. Both are reported,
labelled, never mixed.

Tuning happens on a small dev split; the published number comes from held-out
data. MemPalace does this and says why, and it is the right practice: a figure
reached by inspecting the failures is a figure about those failures.

See `BENCHMARKS.md` for results and their caveats.
