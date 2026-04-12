"""CPU-first FAISS-based semantic retrieval engine."""

import hashlib
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
        self._chunk_map: dict[str, Chunk] = {}
        self._path_index: dict[str, list[str]] = {}
        self._manifest: dict[str, str] = {}
        self._bm25 = None
        self._embedder = None
        self._embed_backend = ""
        self._faiss = None
        self._numpy = None
        self._index = None
        self._bm25_type = None
        self._cache_dir = config.SEMANTIC_CACHE_PATH
        self._index_path = self._cache_dir / "faiss.index"
        self._chunk_path = self._cache_dir / "chunks.json"
        self._manifest_path = self._cache_dir / "manifest.json"
        self._path_index_path = self._cache_dir / "path_index.json"
        self._pending_updates: dict[str, str] = {}
        self._update_timer: threading.Timer | None = None

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
            "embed_backend_config": config.SEMANTIC_EMBED_BACKEND,
            "embed_backend": self._embed_backend,
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

            if (
                self._index_path.exists()
                and self._chunk_path.exists()
                and self._manifest_path.exists()
                and self._path_index_path.exists()
            ):
                self._load_unlocked()
            else:
                self._full_reindex_unlocked()

            self._initialized = True

    def reindex(self, full: bool = True, paths: list[str] | None = None) -> dict:
        """Rebuild the FAISS/BM25 indices from all or selected vault files."""
        with self._lock:
            self._ensure_dependencies()
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            if full or not self._chunks:
                result = self._full_reindex_unlocked()
            else:
                updates = self._updates_from_paths_unlocked(paths) if paths else self._detect_updates_unlocked()
                result = self._incremental_reindex_unlocked(updates)
            self._initialized = True
            return result

    def handle_vault_change(self, rel_path: str, action: str) -> None:
        """Queue a vault path for debounced incremental semantic index updates."""
        if not self.enabled:
            return

        with self._lock:
            previous = self._pending_updates.get(rel_path)
            if previous == "delete":
                action = "delete"
            self._pending_updates[rel_path] = action

            if self._update_timer is not None:
                self._update_timer.cancel()
            self._update_timer = threading.Timer(
                config.SEMANTIC_UPDATE_DEBOUNCE_SECONDS,
                self._flush_pending_updates,
            )
            self._update_timer.start()

    def search(
        self,
        query: str,
        path_prefix: str | None = None,
        filter_tags: list[str] | None = None,
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> dict:
        """Run hybrid semantic + keyword retrieval for a natural-language query."""
        self.initialize()
        with self._lock:
            if not self.enabled:
                return {"error": "Semantic search is disabled. Set VAULT_SEMANTIC_SEARCH_ENABLED=1 to enable it."}
            if not self._available or self._index is None or self._embedder is None:
                return {"error": self._unavailable_reason or "Semantic search is unavailable"}
            if not self._chunks:
                return {"results": [], "total": 0, "truncated": False}

            max_results = min(max_results, config.SEMANTIC_MAX_RESULTS)
            semantic_scores = self._semantic_scores(query, path_prefix, filter_tags, max_results * 4)
            keyword_scores = self._keyword_scores(query, path_prefix, filter_tags)
            merged = self._merge_scores(semantic_scores, keyword_scores)

            results = []
            for chunk_id, score, sem_score, kw_score in merged:
                if score < min_score:
                    continue
                chunk = self._chunk_map.get(chunk_id)
                if chunk is None:
                    continue
                results.append({
                    "path": chunk.path,
                    "title": chunk.title,
                    "section": chunk.section,
                    "tags": chunk.tags,
                    "score": round(score, 4),
                    "semantic_score": round(sem_score, 4),
                    "keyword_score": round(kw_score, 4),
                    "excerpt": chunk.text[:280],
                })
                if len(results) >= max_results:
                    break

            return {
                "results": results,
                "total": len(results),
                "truncated": len(merged) > len(results),
            }

    def _full_reindex_unlocked(self) -> dict:
        """Rebuild all semantic data from scratch."""
        chunks: list[Chunk] = []
        path_index: dict[str, list[str]] = {}
        manifest: dict[str, str] = {}

        for md_path in config.VAULT_PATH.rglob("*.md"):
            rel_path = md_path.relative_to(config.VAULT_PATH).as_posix()
            if not self._is_indexable_path(rel_path):
                continue
            try:
                file_chunks = chunk_markdown_file(md_path)
            except Exception as e:
                logger.warning("Skipping semantic chunking for %s: %s", md_path, e)
                continue

            chunks.extend(file_chunks)
            path_index[rel_path] = [chunk.id for chunk in file_chunks]
            if file_chunks:
                manifest[rel_path] = file_chunks[0].source_hash

        self._chunks = chunks
        self._manifest = manifest
        self._path_index = path_index
        self._chunk_map = {chunk.id: chunk for chunk in chunks}
        self._rebuild_indices_unlocked()
        self._persist_unlocked()
        self._available = True
        self._unavailable_reason = ""
        return {
            "mode": "full",
            "indexed_files": len(path_index),
            "indexed_chunks": len(chunks),
            "cache_path": str(self._cache_dir),
        }

    def _incremental_reindex_unlocked(self, updates: dict[str, str]) -> dict:
        """Apply file-level updates and rebuild in-memory retrieval structures."""
        if not updates:
            return {
                "mode": "incremental",
                "updated_files": 0,
                "removed_files": 0,
                "indexed_files": len(self._path_index),
                "indexed_chunks": len(self._chunks),
                "cache_path": str(self._cache_dir),
            }

        updated_files = 0
        removed_files = 0

        for rel_path, action in updates.items():
            self._remove_file_chunks_unlocked(rel_path)
            if action == "delete":
                removed_files += 1
                continue

            abs_path = config.VAULT_PATH / rel_path
            if not abs_path.exists() or not abs_path.is_file() or not self._is_indexable_path(rel_path):
                self._manifest.pop(rel_path, None)
                removed_files += 1
                continue

            try:
                file_chunks = chunk_markdown_file(abs_path)
            except Exception as e:
                logger.warning("Failed incremental semantic chunking for %s: %s", rel_path, e)
                continue

            if not file_chunks:
                self._manifest.pop(rel_path, None)
                continue

            self._chunks.extend(file_chunks)
            self._path_index[rel_path] = [chunk.id for chunk in file_chunks]
            self._manifest[rel_path] = file_chunks[0].source_hash
            updated_files += 1

        self._chunk_map = {chunk.id: chunk for chunk in self._chunks}
        self._rebuild_indices_unlocked()
        self._persist_unlocked()
        self._available = True
        self._unavailable_reason = ""
        return {
            "mode": "incremental",
            "updated_files": updated_files,
            "removed_files": removed_files,
            "indexed_files": len(self._path_index),
            "indexed_chunks": len(self._chunks),
            "cache_path": str(self._cache_dir),
        }

    def _flush_pending_updates(self) -> None:
        """Flush queued filesystem changes into an incremental reindex run."""
        with self._lock:
            updates = dict(self._pending_updates)
            self._pending_updates.clear()
            self._update_timer = None
            if not updates:
                return

        try:
            self.reindex(full=False, paths=list(updates.keys()))
        except Exception as e:
            logger.warning("Incremental semantic reindex failed: %s", e)

    def _updates_from_paths_unlocked(self, paths: list[str]) -> dict[str, str]:
        """Build path actions from explicit relative paths."""
        updates: dict[str, str] = {}
        for rel_path in paths:
            abs_path = config.VAULT_PATH / rel_path
            if abs_path.exists() and abs_path.is_file() and rel_path.endswith(".md"):
                current_hash = self._hash_file(abs_path)
                if current_hash is None:
                    continue
                if self._manifest.get(rel_path) != current_hash:
                    updates[rel_path] = "modify"
            else:
                if rel_path in self._manifest:
                    updates[rel_path] = "delete"
        return updates

    def _detect_updates_unlocked(self) -> dict[str, str]:
        """Detect modified/new/deleted files by comparing against manifest hashes."""
        updates: dict[str, str] = {}
        current_paths: set[str] = set()

        for md_path in config.VAULT_PATH.rglob("*.md"):
            rel_path = md_path.relative_to(config.VAULT_PATH).as_posix()
            if not self._is_indexable_path(rel_path):
                continue
            current_paths.add(rel_path)
            current_hash = self._hash_file(md_path)
            if current_hash is None:
                continue
            if self._manifest.get(rel_path) != current_hash:
                updates[rel_path] = "modify"

        for rel_path in list(self._manifest.keys()):
            if rel_path not in current_paths:
                updates[rel_path] = "delete"

        return updates

    def _remove_file_chunks_unlocked(self, rel_path: str) -> None:
        """Remove all currently indexed chunks belonging to one file path."""
        chunk_ids = set(self._path_index.get(rel_path, []))
        if chunk_ids:
            self._chunks = [chunk for chunk in self._chunks if chunk.id not in chunk_ids]
        self._path_index.pop(rel_path, None)
        self._manifest.pop(rel_path, None)

    def _ensure_dependencies(self) -> None:
        """Load optional semantic-search dependencies lazily."""
        if self._faiss is not None:
            return

        try:
            import numpy
            import faiss
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
        self._bm25_type = BM25Okapi
        self._embedder = self._build_embedder()

    def _load_unlocked(self) -> None:
        """Load cached FAISS index and metadata from disk."""
        try:
            self._index = self._faiss.read_index(str(self._index_path))
            chunk_payload = json.loads(self._chunk_path.read_text(encoding="utf-8"))
            self._chunks = [Chunk.from_dict(item) for item in chunk_payload]
            self._chunk_map = {chunk.id: chunk for chunk in self._chunks}
            self._manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            self._path_index = json.loads(self._path_index_path.read_text(encoding="utf-8"))
            self._bm25 = self._build_bm25(self._chunks)
            self._available = True
            self._unavailable_reason = ""
        except Exception as e:
            logger.warning("Semantic cache load failed; rebuilding: %s", e)
            self._full_reindex_unlocked()

    def _persist_unlocked(self) -> None:
        """Persist FAISS index and retrieval metadata to disk."""
        if self._index is None:
            return
        self._faiss.write_index(self._index, str(self._index_path))
        self._chunk_path.write_text(
            json.dumps([chunk.to_dict() for chunk in self._chunks], ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        self._manifest_path.write_text(json.dumps(self._manifest, ensure_ascii=True, indent=2), encoding="utf-8")
        self._path_index_path.write_text(json.dumps(self._path_index, ensure_ascii=True, indent=2), encoding="utf-8")

    def _rebuild_indices_unlocked(self) -> None:
        """Rebuild BM25 and FAISS search structures from in-memory chunks."""
        self._bm25 = self._build_bm25(self._chunks)
        self._index = self._build_faiss_index(self._chunks)

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
        if self._embed_backend.startswith("sentence-transformers"):
            embeddings = self._embedder.encode(
                [self._embedding_text(chunk) for chunk in chunks],
                normalize_embeddings=False,
                convert_to_numpy=True,
            )
        else:
            embeddings = list(self._embedder.embed([self._embedding_text(chunk) for chunk in chunks]))
        matrix = self._numpy.asarray(embeddings, dtype="float32")
        self._faiss.normalize_L2(matrix)
        index = self._faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)
        return index

    def _semantic_scores(
        self,
        query: str,
        path_prefix: str | None,
        filter_tags: list[str] | None,
        top_k: int,
    ) -> dict[str, float]:
        """Return semantic similarity scores keyed by chunk id."""
        if self._embed_backend.startswith("sentence-transformers"):
            query_vec = self._embedder.encode(
                [query],
                normalize_embeddings=False,
                convert_to_numpy=True,
            )
        else:
            query_vec = list(self._embedder.embed([query]))
        matrix = self._numpy.asarray(query_vec, dtype="float32")
        self._faiss.normalize_L2(matrix)
        distances, indices = self._index.search(matrix, min(top_k, len(self._chunks)))

        expected_tags = {tag.lower() for tag in (filter_tags or [])}
        scores: dict[str, float] = {}
        for score, idx in zip(distances[0], indices[0], strict=False):
            if idx < 0:
                continue
            chunk = self._chunks[idx]
            if path_prefix and not chunk.path.startswith(path_prefix):
                continue
            if expected_tags and not expected_tags.issubset({tag.lower() for tag in chunk.tags}):
                continue
            scores[chunk.id] = float(max(score, 0.0))
        return scores

    def _keyword_scores(
        self,
        query: str,
        path_prefix: str | None,
        filter_tags: list[str] | None,
    ) -> dict[str, float]:
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
        expected_tags = {tag.lower() for tag in (filter_tags or [])}

        scores: dict[str, float] = {}
        for chunk, raw_score in zip(self._chunks, raw_scores, strict=False):
            if path_prefix and not chunk.path.startswith(path_prefix):
                continue
            if expected_tags and not expected_tags.issubset({tag.lower() for tag in chunk.tags}):
                continue
            normalized = max(float(raw_score), 0.0) / max_score
            if normalized > 0.0:
                scores[chunk.id] = normalized
        return scores

    def _merge_scores(
        self,
        semantic_scores: dict[str, float],
        keyword_scores: dict[str, float],
    ) -> list[tuple[str, float, float, float]]:
        """Merge semantic and keyword signals into a hybrid ranking."""
        combined_ids = set(semantic_scores) | set(keyword_scores)
        merged: list[tuple[str, float, float, float]] = []
        for chunk_id in combined_ids:
            sem_score = semantic_scores.get(chunk_id, 0.0)
            kw_score = keyword_scores.get(chunk_id, 0.0)
            total = sem_score * 0.75 + kw_score * 0.25
            merged.append((chunk_id, total, sem_score, kw_score))
        merged.sort(key=lambda item: item[1], reverse=True)
        return merged

    @staticmethod
    def _embedding_text(chunk: Chunk) -> str:
        """Build the semantic-search text payload for a chunk."""
        parts = [chunk.title]
        if chunk.section:
            parts.append(chunk.section)
        if chunk.tags:
            parts.append(" ".join(chunk.tags))
        parts.append(chunk.text)
        return "\n".join(part for part in parts if part)

    def _build_embedder(self):
        """Create an embedding backend honoring VAULT_SEMANTIC_EMBED_BACKEND."""
        preferred_backend = config.SEMANTIC_EMBED_BACKEND

        if preferred_backend in {"auto", "fastembed"}:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                if preferred_backend == "fastembed":
                    self._available = False
                    self._unavailable_reason = (
                        "Fastembed backend was forced but is not installed. "
                        "Install with: python -m pip install -e .[semantic]"
                    )
                    raise RuntimeError(self._unavailable_reason) from e
            else:
                self._embed_backend = "fastembed"
                return TextEmbedding(model_name=config.SEMANTIC_EMBED_MODEL)

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            self._available = False
            if preferred_backend == "sentence":
                self._unavailable_reason = (
                    "Sentence-transformers backend was forced but is not installed. "
                    "Install with: python -m pip install -e .[semantic-sentence]"
                )
            else:
                self._unavailable_reason = (
                    "No supported embedding backend installed. "
                    "Install with: python -m pip install -e .[semantic] "
                    "or python -m pip install -e .[semantic-sentence]."
                )
            raise RuntimeError(self._unavailable_reason) from e

        self._embed_backend = "sentence-transformers"
        return SentenceTransformer(config.SEMANTIC_EMBED_MODEL, device="cpu")

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        """Compute SHA256 hash for a file, returning None on read failure."""
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_indexable_path(rel_path: str) -> bool:
        """Return whether a relative path should be included in semantic indexing."""
        if not rel_path.endswith(".md"):
            return False
        parts = Path(rel_path).parts
        return not bool(set(parts) & config.EXCLUDED_DIRS)
