"""A deterministic stand-in for the embedding model, shared by tests and their child processes.

Vectors depend on model name and text, so a vector reused for the wrong chunk, or from
another model, differs from a fresh one. Everything else in the engine is real.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIM = 16


class RecordingEmbedder:
    def __init__(self, model: str, record_to: str | None = None):
        self.model = model
        self.texts: list[str] = []
        self._record_to = record_to

    def embed(self, texts):
        for text in texts:
            self.texts.append(text)
            if self._record_to:
                with open(self._record_to, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(text) + "\n")
            digest = hashlib.sha256(f"{self.model}\n{text}".encode("utf-8")).digest()
            yield [byte / 255.0 - 0.5 for byte in digest[:DIM]]


def install(engine_cls, config, embedders: list | None = None, record_to: str | None = None):
    """Replace only the model of `engine_cls`; the backend name stays fastembed."""

    def build_embedder(self):
        self._embed_backend = "fastembed"
        embedder = RecordingEmbedder(config.SEMANTIC_EMBED_MODEL, record_to)
        if embedders is not None:
            embedders.append(embedder)
        return embedder

    engine_cls._build_embedder = build_embedder


def die() -> None:
    """End this process the way the OOM killer does: no cleanup, no finally."""
    if hasattr(signal, "SIGKILL"):
        os.kill(os.getpid(), signal.SIGKILL)
    os._exit(137)  # pragma: no cover - Windows


CHILD = r'''
import json, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from obsidian_vault_mcp import config
from obsidian_vault_mcp.retrieval import generations as gen
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine
from tests import semantic_fakes

spec = json.loads(sys.argv[1])
config.VAULT_PATH = Path(spec["vault"])
config.SEMANTIC_CACHE_PATH = Path(spec["cache"])
config.SEMANTIC_SEARCH_ENABLED = True
config.SEMANTIC_EMBED_MODEL = spec.get("model", "model-a")
config.SEMANTIC_EMBED_BATCH_SIZE = 4
semantic_fakes.install(SemanticSearchEngine, config, record_to=spec.get("record_to"))

kill_at = spec.get("kill_at")
if kill_at:
    def hook(step):
        if step == kill_at:
            semantic_fakes.die()
    gen._fault_hook = hook
kill_after_texts = spec.get("kill_after_texts")
if kill_after_texts:
    original = semantic_fakes.RecordingEmbedder.embed
    def embed(self, texts):
        for vector in original(self, texts):
            if len(self.texts) > kill_after_texts:
                semantic_fakes.die()
            yield vector
    semantic_fakes.RecordingEmbedder.embed = embed
if spec.get("hold_lock_seconds"):
    import time
    with gen.writer_lock(config.SEMANTIC_CACHE_PATH):
        print("locked", flush=True)
        time.sleep(spec["hold_lock_seconds"])
    sys.exit(0)

engine = SemanticSearchEngine(maintenance=True)
try:
    result = engine.reindex(full=spec["full"], lock_timeout=spec.get("lock_timeout", 0))
except gen.WriterBusy as exc:
    print(json.dumps({{"busy": str(exc)}}))
    sys.exit(3)
print(json.dumps(result))
'''


def run_child(spec: dict, wait: bool = True):
    """Run one maintenance reindex in a fresh interpreter, as the nightly job does."""
    args = [sys.executable, "-c", CHILD.format(repo=str(REPO)), json.dumps(spec)]
    env = {key: value for key, value in os.environ.items() if not key.startswith("VAULT_")}
    if not wait:
        return subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)
