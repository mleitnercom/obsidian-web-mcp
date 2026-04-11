"""CPU-first FAISS-based semantic retrieval engine."""

import json
import logging
import threading
from pathlib import Path

from .. import config
from .chunker import chunk_markdown_file
from .models import Chunk

logger = logging.getLogger(__name__)


class SemanticSearchEngine:
    """Optional semantic retrieval engine backed by FAISS and BM25."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._available = False
        self._unavailable_reason = ""
        self._chunks: list[Chunk] = []
        self._bm25 = None
        self._embedder = None
        self._faiss = None
        self._numpy = None
        self._index = None
        self._cache_dir = config.SEMANTIC_CACHE_PATH
        self._index_path = self._cache_dir / "faiss.index"
        self._chunk_path = self._cache_dir / "chunks.json"

    @property
    def enabled(self) -> bool:
        """Return whether semantic search is enabled via configuration."""
        return config.SEMANTIC_SEARCH_ENABLED

    @property
    def status(self) -> dict:
        """Return current engine status for tooling and diagnostics."""
        return {
            "enabled": self.enabled,
            "available": self._available,
            "initialized": self._initialized,
            "chunk_count": len(self._chunks),
            "cache_path": str(self._cache_dir),
            "reason": self._unavailable_reason,
        }

    def initialize(self) -> None:
        """Initialize the engine and load or build the persistent index."""
        with self._lock:
            if self._initialized:
                return

            if not self.enabled:
                self._unavailable_reason = "Semantic search is disabled"
                self._initialized = True
                return

            self._ensure_dependencies()
            self._cache_dir.mkdir(parents=True, exist_ok=True)

            if self._index_path.exists() and self._chunk_path.exists():
                self._load()
            else:
                self.reindex()

            self._initialized = True

    def reindex(self) -> dict:
        """Rebuild the FAISS and BM25 indices from the current vault."""
        self._ensure_dependencies()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        chunks: list[Chunk] = []
        for md_path in config.VAULT_PATH.rglob("*.md"):
            rel_parts = md_path.relative_to(config.VAULT_PATH).parts
            if set(rel_parts) & config.EXCLUDED_DIRS:
                continue
            try:
                chunks.extend(chunk_markdown_file(md_path))
            except Exception as e:
                logger.warning("Skipping semantic chunking for %s: %s", md_path, e)

        self._chunks = chunks
        self._bm25 = self._build_bm25(chunks)
        self._index = self._build_faiss_index(chunks)
        self._persist()
        self._available = True
        self._initialized = True
        self._unavailable_reason = ""
        return {
            "indexed_files": len({chunk.path for chunk in chunks}),
            "indexed_chunks": len(chunks),
            "cache_path": str(self._cache_dir),
        }

    def search(self, query: str, path_prefix: str | None = None, max_results: int = 10) -> dict:
        """Run hybrid semantic + keyword retrieval for a natural-language query."""
        self.initialize()
        if not self.enabled:
            return {"error": "Semantic search is disabled. Set VAULT_SEMANTIC_SEARCH_ENABLED=1 to enable it."}
        if not self._available or self._index is None or self._embedder is None:
            return {"error": self._unavailable_reason or "Semantic search is unavailable"}
        if not self._chunks:
            return {"results": [], "total": 0, "truncated": False}

        max_results = min(max_results, config.SEMANTIC_MAX_RESULTS)
        semantic_scores = self._semantic_scores(query, path_prefix, max_results * 4)
        keyword_scores = self._keyword_scores(query, path_prefix)
        merged = self._merge_scores(semantic_scores, keyword_scores)

        results = []
        for chunk_id, score in merged[:max_results]:
            chunk = next((item for item in self._chunks if item.id == chunk_id), None)
            if chunk is None:
                continue
            results.append({
                "path": chunk.path,
                "title": chunk.title,
                "section": chunk.section,
                "score": round(score, 4),
                "excerpt": chunk.text[:280],
            })

        return {
            "results": results,
            "total": len(results),
            "truncated": len(merged) > max_results,
        }

    def _ensure_dependencies(self) -> None:
        """Load optional semantic-search dependencies lazily."""
        if self._faiss is not None:
            return

        try:
            import numpy
            import faiss
            from fastembed import TextEmbedding
            from rank_bm25 import BM25Okapi
        except ImportError as e:
            self._available = False
            self._unavailable_reason = (
                "Semantic search dependencies are not installed. "
                "Install with: python -m pip install -e .[semantic]"
            )
            raise RuntimeError(self._unavailable_reason) from e

        self._numpy = numpy
        self._faiss = faiss
        self._embedder = TextEmbedding(model_name=config.SEMANTIC_EMBED_MODEL)
        self._bm25_type = BM25Okapi

    def _load(self) -> None:
        """Load cached FAISS index and chunk metadata from disk."""
        try:
            self._index = self._faiss.read_index(str(self._index_path))
            chunk_payload = json.loads(self._chunk_path.read_text(encoding="utf-8"))
            self._chunks = [Chunk.from_dict(item) for item in chunk_payload]
            self._bm25 = self._build_bm25(self._chunks)
            self._available = True
            self._unavailable_reason = ""
        except Exception as e:
            logger.warning("Semantic cache load failed; rebuilding: %s", e)
            self.reindex()

    def _persist(self) -> None:
        """Persist FAISS index and chunk metadata to disk."""
        if self._index is None:
            return
        self._faiss.write_index(self._index, str(self._index_path))
        self._chunk_path.write_text(
            json.dumps([chunk.to_dict() for chunk in self._chunks], ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _build_bm25(self, chunks: list[Chunk]):
        """Build the BM25 index from tokenized chunks."""
        corpus = [chunk.tokens for chunk in chunks if chunk.tokens]
        if not corpus:
            return None
        return self._bm25_type(corpus)

    def _build_faiss_index(self, chunks: list[Chunk]):
        """Embed chunks and build a FAISS inner-product index."""
        if not chunks:
            return None
        embeddings = list(self._embedder.embed([self._embedding_text(chunk) for chunk in chunks]))
        matrix = self._numpy.asarray(embeddings, dtype="float32")
        self._faiss.normalize_L2(matrix)
        index = self._faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)
        return index

    def _semantic_scores(self, query: str, path_prefix: str | None, top_k: int) -> dict[str, float]:
        """Return semantic similarity scores keyed by chunk id."""
        query_vec = list(self._embedder.embed([query]))
        matrix = self._numpy.asarray(query_vec, dtype="float32")
        self._faiss.normalize_L2(matrix)
        distances, indices = self._index.search(matrix, min(top_k, len(self._chunks)))

        scores: dict[str, float] = {}
        for score, index in zip(distances[0], indices[0], strict=False):
            if index < 0:
                continue
            chunk = self._chunks[index]
            if path_prefix and not chunk.path.startswith(path_prefix):
                continue
            scores[chunk.id] = float(max(score, 0.0))
        return scores

    def _keyword_scores(self, query: str, path_prefix: str | None) -> dict[str, float]:
        """Return BM25 keyword scores keyed by chunk id."""
        if self._bm25 is None:
            return {}
        tokens = [token.lower() for token in query.split() if token.strip()]
        if not tokens:
            return {}

        raw_scores = self._bm25.get_scores(tokens)
        if len(raw_scores) == 0:
            return {}
        max_score = max(float(score) for score in raw_scores) or 1.0

        scores: dict[str, float] = {}
        for chunk, raw_score in zip(self._chunks, raw_scores, strict=False):
            if path_prefix and not chunk.path.startswith(path_prefix):
                continue
            normalized = max(float(raw_score), 0.0) / max_score
            if normalized > 0.0:
                scores[chunk.id] = normalized
        return scores

    def _merge_scores(self, semantic_scores: dict[str, float], keyword_scores: dict[str, float]) -> list[tuple[str, float]]:
        """Merge semantic and keyword signals into a hybrid ranking."""
        combined_ids = set(semantic_scores) | set(keyword_scores)
        merged = [
            (chunk_id, semantic_scores.get(chunk_id, 0.0) * 0.7 + keyword_scores.get(chunk_id, 0.0) * 0.3)
            for chunk_id in combined_ids
        ]
        merged.sort(key=lambda item: item[1], reverse=True)
        return merged

    @staticmethod
    def _embedding_text(chunk: Chunk) -> str:
        """Build the semantic-search text payload for a chunk."""
        parts = [chunk.title]
        if chunk.section:
            parts.append(chunk.section)
        parts.append(chunk.text)
        return "\n".join(part for part in parts if part)

