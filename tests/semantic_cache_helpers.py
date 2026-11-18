"""Read a semantic cache as a reader does, and write the flat cache of 0.18 and earlier.

`flat_from_generation` turns the current generation into the five flat files exactly as
`_persist_unlocked` of 0.18 wrote them (json.dumps with indent=2 and ASCII escapes,
faiss.write_index, empty notes in the path index but not in the manifest, token lists in
every chunk) and removes the generation, so a test starts from a pre-0.19 cache.
"""

import json
import shutil
from pathlib import Path

import faiss

from obsidian_vault_mcp.retrieval import generations as gen
from obsidian_vault_mcp.retrieval.chunker import _tokenize


def current_dir(cache) -> Path:
    generation_id = gen.read_pointer(cache)
    assert generation_id, f"no CURRENT in {cache}"
    return gen.generation_dir(cache, generation_id)


def read_current(cache):
    """(chunks in row order, vectors, seal, path_index, manifest) of the generation CURRENT names."""
    gen_dir = current_dir(cache)
    verified = gen.verify(gen_dir, deep=True)
    chunks = [json.loads(line) for line in (gen_dir / gen.CHUNKS).read_text(encoding="utf-8").splitlines()]
    index = faiss.read_index(str(gen_dir / gen.VECTORS))
    vectors = index.reconstruct_n(0, index.ntotal) if index.ntotal else []
    return chunks, vectors, verified.seal, verified.path_index, verified.manifest


def by_id(cache):
    """Chunks by id with their vector: row i belongs to chunks[i]; compare builds by id only."""
    chunks, vectors, *_ = read_current(cache)
    assert len(vectors) == len(chunks)
    return {chunk["id"]: (chunk, vectors[row]) for row, chunk in enumerate(chunks)}


def flat_from_generation(cache) -> None:
    cache = Path(cache)
    chunks, vectors, seal, path_index, manifest = read_current(cache)
    index = faiss.IndexFlatIP(seal["dimension"])
    if len(chunks):
        index.add(vectors)
    faiss.write_index(index, str(cache / "faiss.index"))
    for chunk in chunks:
        chunk["tokens"] = _tokenize(f"{chunk['title']}\n{chunk['section']}\n{' '.join(chunk['tags'])}\n{chunk['text']}")
    (cache / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=True, indent=2), encoding="utf-8")
    flat_manifest = {path: digest for path, digest in manifest.items() if path_index[path]}
    (cache / "manifest.json").write_text(json.dumps(flat_manifest, ensure_ascii=True, indent=2), encoding="utf-8")
    (cache / "path_index.json").write_text(json.dumps(path_index, ensure_ascii=True, indent=2), encoding="utf-8")
    (cache / "index_meta.json").write_text(json.dumps(seal["embedder"], indent=2), encoding="utf-8")
    (cache / gen.POINTER).unlink()
    shutil.rmtree(cache / gen.GENERATIONS)
