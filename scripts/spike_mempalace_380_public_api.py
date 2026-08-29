"""Prove the MemPalace 3.8 public API needed by Eidolon Memory.

This deliberately avoids MemPalace private symbols.  It creates a fresh Palace,
uses the documented OpenAI-compatible embedder for collection identity, and
passes Eidolon's document/query vectors explicitly through BaseCollection.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIMENSION = 512
MODEL = "Xenova/bge-small-zh-v1.5"


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [0.0] * DIMENSION
    for index, value in enumerate(digest):
        vector[(index * 17 + value) % DIMENSION] += (value + 1) / 256.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class _EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        inputs = payload.get("input") or []
        rows = [
            {"object": "embedding", "index": index, "embedding": _vector(text)}
            for index, text in enumerate(inputs)
        ]
        body = json.dumps({"object": "list", "data": rows, "model": MODEL}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


def main() -> None:
    import mempalace
    from mempalace.backends.base import BaseCollection
    from mempalace.palace import get_backend_for_palace, get_collection

    if mempalace.__version__ != "3.8.0":
        raise RuntimeError(f"expected MemPalace 3.8.0, got {mempalace.__version__}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="eidolon-mempalace-380-") as root:
            palace = Path(root) / "fresh-palace"
            home = Path(root) / "home"
            home.mkdir()
            os.environ.update(
                {
                    "HOME": str(home),
                    "MEMPALACE_PALACE_PATH": str(palace),
                    "MEMPALACE_BACKEND": "chroma",
                    "MEMPALACE_EMBEDDING_MODEL": "openai-compat",
                    "MEMPALACE_EMBEDDING_API_URL": (
                        f"http://127.0.0.1:{server.server_address[1]}"
                    ),
                    "MEMPALACE_EMBEDDING_API_MODEL": MODEL,
                }
            )

            writer = get_collection(str(palace), create=True, backend="chroma")
            if not isinstance(writer, BaseCollection):
                raise AssertionError(type(writer))
            writer.upsert(
                ids=["drawer_public_contract"],
                documents=["用户喜欢草莓"],
                metadatas=[
                    {
                        "wing": "Wing_Profile",
                        "room": "preference",
                        "audience": "companion:Xiaoer",
                    }
                ],
                embeddings=[_vector("passage: 用户喜欢草莓")],
            )
            result = writer.query(
                query_embeddings=[_vector("query: 用户喜欢草莓")],
                n_results=1,
                where={"audience": "companion:Xiaoer"},
                include=["documents", "metadatas", "distances"],
            )
            if result.ids != [["drawer_public_contract"]]:
                raise AssertionError(result)
            get_backend_for_palace(str(palace), explicit="chroma").close_palace(str(palace))

            reader = get_collection(
                str(palace), create=False, backend="chroma", read_only=True
            )
            visible = reader.get(
                ids=["drawer_public_contract"], include=["documents", "metadatas"]
            )
            if visible.ids != ["drawer_public_contract"]:
                raise AssertionError(visible)
            get_backend_for_palace(str(palace), explicit="chroma").close_palace(str(palace))

            print(
                json.dumps(
                    {
                        "mempalace": mempalace.__version__,
                        "backend": "chroma",
                        "dimension": DIMENSION,
                        "explicit_document_embedding": True,
                        "explicit_query_embedding": True,
                        "read_only_open": True,
                        "fresh_palace": True,
                    },
                    sort_keys=True,
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
