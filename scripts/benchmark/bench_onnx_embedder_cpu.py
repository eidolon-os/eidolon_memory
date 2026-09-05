"""bge-small-zh CPU baseline, replicating OnnxSentenceEmbedder exactly.

CLS pooling, no query prefix, padding+truncation to 512, L2 normalise,
batch 32 by default -- the same shape eidolon-memory-embedder runs.

Measures how the embedder responds to CPU allocation, which is what decides
where it runs on a big.LITTLE host. Pin with taskset; the numbers below came
from an RK3588 (4x A55 @1.8GHz + 4x A76 @2.256GHz):

    taskset -c 4-7 python bench_onnx_embedder_cpu.py --threads 4 --tag a76x4

    A76x4  14.27 ms/doc      A55x4  56.76 ms/doc
    A76x1  51.65 ms/doc      A55x1 261.5  ms/doc
    all 8  15.81 ms/doc  -- slower than A76x4 alone: ORT splits work evenly and
                            the batch waits on the A55 threads, ~5x behind.

So the embedder belongs on the big cores, and the little cores must be
excluded rather than merely added. Pass --threads equal to the number of
pinned cores; leaving it at 0 lets ORT size its own pool, which does not
respect the affinity mask on every runtime version.

Rescued from a hand-written script that lived only on the board.
"""
import argparse, json, os, statistics, time
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

p = argparse.ArgumentParser()
p.add_argument("--model-dir",
               default=os.environ.get("BGE_MODEL_DIR",
                                      os.path.expanduser("~/bge/bge-small-zh")),
               help="dir holding onnx/model_quantized.onnx and tokenizer.json")
p.add_argument("--threads", type=int, default=0)
p.add_argument("--batch", type=int, default=32)
p.add_argument("--docs", type=int, default=512)
p.add_argument("--chars", type=int, default=120, help="approx chars per doc")
p.add_argument("--warmup", type=int, default=2)
p.add_argument("--repeat", type=int, default=5)
p.add_argument("--tag", default="")
a = p.parse_args()

opts = ort.SessionOptions()
if a.threads > 0:
    opts.intra_op_num_threads = a.threads
t0 = time.perf_counter()
sess = ort.InferenceSession(
    os.path.join(a.model_dir, "onnx", "model_quantized.onnx"),
    sess_options=opts, providers=["CPUExecutionProvider"])
load_ms = (time.perf_counter() - t0) * 1000
names = frozenset(i.name for i in sess.get_inputs())

tok = Tokenizer.from_file(os.path.join(a.model_dir, "tokenizer.json"))
tok.enable_padding()
tok.enable_truncation(max_length=512)

# Chinese fragments of realistic memory length.
base = ("我今天下午三点和张老师约了在图书馆二楼见面，讨论下学期的课程安排以及"
        "毕业设计的选题方向，他说要我先准备一份提纲。")
docs = [(base * 4)[: a.chars] + f"（第{i}条）" for i in range(a.docs)]

def run(texts):
    out = []
    for s in range(0, len(texts), a.batch):
        chunk = texts[s : s + a.batch]
        enc = tok.encode_batch(chunk)
        ids = np.asarray([e.ids for e in enc], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = sess.run(None, feed)[0]
        pooled = hidden[:, 0]                      # CLS
        pooled = pooled / (np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-12)
        out.append(pooled)
    return np.concatenate(out)

for _ in range(a.warmup):
    run(docs[: a.batch])

per_doc, wall = [], []
for _ in range(a.repeat):
    t = time.perf_counter()
    v = run(docs)
    d = time.perf_counter() - t
    wall.append(d * 1000)
    per_doc.append(d * 1000 / len(docs))

# single-query latency, the recall path
q = ["我上次和张老师约在哪里见面？"]
for _ in range(3):
    run(q)
ql = []
for _ in range(20):
    t = time.perf_counter(); run(q); ql.append((time.perf_counter() - t) * 1000)

seq = int(np.asarray([e.ids for e in tok.encode_batch(docs[:1])]).shape[1])
print(json.dumps({
    "tag": a.tag, "threads": a.threads or "auto", "batch": a.batch,
    "docs": a.docs, "chars": a.chars, "seq_len": seq, "dim": int(v.shape[1]),
    "load_ms": round(load_ms, 1),
    "ms_per_doc": round(statistics.median(per_doc), 2),
    "ms_per_doc_min": round(min(per_doc), 2),
    "batch_wall_ms": round(statistics.median(wall), 1),
    "query_p50_ms": round(statistics.median(ql), 2),
    "query_p95_ms": round(sorted(ql)[int(len(ql) * 0.95) - 1], 2),
}, ensure_ascii=False))
