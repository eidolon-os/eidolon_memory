# Host profiles

Every latency and memory figure this project has published came from a 12-core
Apple Silicon laptop. `docs/ARCHITECTURE.md` says what that is worth: the ordering
between embedders transfers and the resident memory transfers almost exactly, but
the absolute milliseconds do not. The deployment target is a Raspberry Pi 5 — four
Cortex-A76 cores at 2.4 GHz sharing 4 GB with the rest of Eidolon.

`benchmarks/suites/probe_host.py` re-measures, on whatever machine it is run on,
exactly the numbers that do not transfer. A profile committed here is what a later
run compares against, so a board figure arrives as a ratio rather than as a number
with nothing behind it.

## Running it on the Pi

Nothing here needs NATS, an LLM, an API key or a network beyond the one-time model
download. That is deliberate: bring-up should not depend on the rest of the stack
being up.

```bash
uv sync
uv run python benchmarks/suites/probe_host.py \
    --out benchmarks/results/host-profile/pi5.json \
    --baseline benchmarks/results/host-profile/apple-m-12core.json
```

It builds throwaway palaces in a temp directory and removes nothing else. Give it
`--palaces-root /some/path` to keep them.

### Dependencies actually resolve on the board

Checked against PyPI rather than assumed, for the versions this repo pins:

- `onnxruntime 1.26.0` ships `cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64`.
- `tokenizers 0.23.1` ships `cp310-abi3-manylinux_2_17_aarch64`.

Raspberry Pi OS Bookworm is Debian 12, glibc 2.36, so `manylinux_2_28` is
satisfied and `uv sync` installs wheels rather than building from source. This
needs the **64-bit** OS; the 32-bit image is `armv7l` and no wheel matches it.

The first run downloads `bge-small-zh` (~130 MB) into the Hugging Face cache. On a
board with no network, populate `embedding.model_dir` instead — the ONNX embedder
reads it directly and falls back to the hub with a warning if it is incomplete.

## What moves between hosts, and what to look at

| Row | Why it is here |
|---|---|
| `ledger semaphore`, `executor threads` | Both are `cpu_count`-shaped: 12 and 16 here, **4 and 8 on the Pi**. Six ledgers times a few spaces reaches those, and that is the bound to watch under 1:N. |
| `RSS: + model loaded` | The per-process fixed cost, and the whole argument for one process serving several spaces. |
| `RSS: per extra space` | Near zero on empty palaces. A populated palace adds its own working set whatever the process layout, so this measures the code and the model, not the data. |
| `write turn (batched)` vs `(one by one)` | The batch write. Roughly 3.5x apart here; the ratio is what should hold on the board even though both numbers grow. |
| `recall p95` | The figure the voice path's 300 ms deadline is spent against. |
| `graph beside vector p95` | The readers-writer lock, on the case it was introduced for: a graph lookup issued alongside a vector search, against a 50 ms voice budget. Under the exclusive mutex it replaced, this was the vector search's whole duration. If it approaches the budget on the board, that is the number that says so — and the probe prints `fits` or `BLOWS` rather than leaving it to be read off. |

## Tuning worth trying on the board, in order

1. **`embedding.threads`.** `0` leaves ONNX Runtime at its default, which is the
   core count — on four cores shared with a NATS subscriber, the steward's HTTP
   calls and Chroma, that is a choice to re-examine rather than inherit. Compare
   `--threads 0`, `--threads 2`, `--threads 4`.
2. **`embedding.provider: http`.** If the board is the constraint rather than the
   network, moving the encoder off it removes ~100 MB resident and the whole
   embedding cost from every recall, at the price of a network hop. The palace
   stays local. Changing `provider` is the entire switch.
3. **A bigger model, only if 1 and 2 leave headroom.** `probe_embedders.py` scores
   quality; this probe only tells you what a model costs.
