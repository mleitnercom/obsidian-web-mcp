"""The hybrid ranking: the keyword half must see the query's words, and the ranking must
not depend on how many results are asked for.

Found by the comparison of 16.10.2026 (20 known-item questions against the production
index): keyword-style queries with rare exact terms ("Netscaler-Forensik", "Prüferbank",
"Luigi's Box") were not found, and asking for 20 instead of 10 results moved notes up.
The causes, each with a test here that fails on 0.17.0:

- the query was split on whitespace only while the chunks were tokenized on
  [A-Za-z0-9_]+, so "netscaler-forensik", "prüferbank" or "verlieren?" matched nothing;
- that tokenizer also cut every word with an umlaut in two;
- the semantic candidates were max_results * 4, and a keyword match outside them scored
  0 for meaning, so its rank changed with max_results;
- BM25 left chunks without tokens out of its corpus but its scores were matched to
  chunks by position, so one empty chunk shifted every later score onto its neighbour.

Real FAISS and BM25; only the model is replaced, by a bag-of-words embedder that, like a
real model, knows ordinary words and not rare codes.
"""

import json
import os
import random
import re
import zlib

import pytest

from obsidian_vault_mcp import config
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine
from obsidian_vault_mcp.retrieval.models import Chunk

REQUIRE = os.environ.get("VAULT_TEST_REQUIRE_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
try:
    import faiss  # noqa: F401
    import numpy  # noqa: F401
    import rank_bm25  # noqa: F401
except ImportError as exc:  # the dev machine and CI install no [semantic] extra
    if REQUIRE:
        raise AssertionError(f"VAULT_TEST_REQUIRE_TOOLS=1 but semantic dependencies are missing: {exc}")
    pytest.skip("semantic dependencies (faiss, numpy, rank_bm25) not installed", allow_module_level=True)

DIM = 64
FILLER = [
    "Rasen mähen am Samstag", "Einkaufsliste Milch Brot", "Urlaub im Sommer planen",
    "Rechnung für den Installateur", "Termin beim Zahnarzt", "Reifenwechsel im Herbst",
    "Geburtstag der Nachbarin", "Fahrrad reparieren lassen", "Bücher zurückgeben",
    "Schuhe für den Winter", "Pflanzen gießen", "Steuererklärung vorbereiten",
]


class WordEmbedder:
    """Normalized bag of words over plain lowercase words; anything with a digit is unknown."""

    def embed(self, texts):
        for text in texts:
            vec = [0.0] * DIM
            for word in re.findall(r"\w+", text.lower()):
                if not any(ch.isdigit() for ch in word):
                    vec[zlib.crc32(word.encode()) % DIM] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            yield [v / norm for v in vec]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", root)
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", tmp_path / "cache")
    monkeypatch.setattr(config, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_BUILD_ON_DEMAND", True)

    def build_embedder(self):
        self._embed_backend = "fastembed"
        return WordEmbedder()

    monkeypatch.setattr(SemanticSearchEngine, "_build_embedder", build_embedder)
    # BM25 gives a word in half of a two-note corpus an idf of 0; real vaults are large.
    for n, words in enumerate(FILLER):
        (root / f"fuell{n:02d}.md").write_text(f"# Füllung {n}\n\n{words}\n", encoding="utf-8")
    return root


def paths(result):
    return [hit["path"] for hit in result["results"]]


# --- the keyword half sees the query's words ---------------------------------------------

@pytest.mark.parametrize("query", ["Netscaler-Forensik?", "prüferbank", "Luigi's", "Prüferbank, Kapazität"])
def test_query_words_with_hyphens_umlauts_and_punctuation_find_the_note(vault, query):
    (vault / "befund.md").write_text(
        "# Befund\n\nDie Netscaler-Forensik liegt vor. Die Prüferbank hat keine Kapazität. Luigi's Box.\n",
        encoding="utf-8",
    )
    (vault / "anderes.md").write_text("# Anderes\n\nEin Text über Heizung und Garten.\n", encoding="utf-8")

    result = SemanticSearchEngine().search(query, search_mode="keyword", max_results=5)

    assert paths(result)[:1] == ["befund.md"], result
    assert result["results"][0]["keyword_score"] > 0


def test_a_word_with_an_umlaut_is_one_word():
    from obsidian_vault_mcp.retrieval.chunker import _tokenize

    assert _tokenize("Prüferbank, Straße! snake_case 2026-09") == ["prüferbank", "straße", "snake_case", "2026", "09"]


def test_a_fragment_of_an_umlaut_word_no_longer_matches(vault):
    """Under the old tokenizer "prüferbank" was indexed as "pr" and "ferbank"."""
    (vault / "befund.md").write_text("# Befund\n\nDie Prüferbank ist knapp.\n", encoding="utf-8")

    result = SemanticSearchEngine().search("ferbank", search_mode="keyword", max_results=5)

    assert paths(result) == [], result


def test_a_cache_written_by_the_old_tokenizer_matches_without_a_reindex(vault):
    (vault / "befund.md").write_text("# Befund\n\nDie Prüferbank ist knapp.\n", encoding="utf-8")
    SemanticSearchEngine().search("x", max_results=1)  # builds the cache
    cache = config.SEMANTIC_CACHE_PATH / "chunks.json"
    chunks = json.loads(cache.read_text(encoding="utf-8"))
    old = re.compile(r"[A-Za-z0-9_]+")
    for chunk in chunks:
        chunk["tokens"] = [m.group(0).lower() for m in old.finditer(f"{chunk['title']}\n{chunk['text']}")]
    assert any("ferbank" in c["tokens"] for c in chunks), "negative control: the cache holds old tokens"
    cache.write_text(json.dumps(chunks), encoding="utf-8")

    result = SemanticSearchEngine().search("Prüferbank", search_mode="keyword", max_results=5)

    assert paths(result) == ["befund.md"], result


def test_chunk_tokens_are_derived_from_the_fields():
    from obsidian_vault_mcp.retrieval.chunker import chunk_tokens

    chunk = Chunk(id="a::0", path="a.md", title="Titel", section="Abschnitt", tags=["tag"],
                  text="Prüferbank", tokens=["stale"], source_hash="h")

    assert chunk_tokens(chunk) == ["titel", "abschnitt", "tag", "prüferbank"]


def test_an_empty_chunk_does_not_shift_keyword_scores(vault):
    engine = SemanticSearchEngine()
    engine._ensure_dependencies()
    empty = Chunk(id="leer.md::0", path="leer.md", title="", section="", tags=[], text="—", tokens=[], source_hash="h")
    target = Chunk(id="ziel.md::0", path="ziel.md", title="", section="", tags=[], text="netscaler forensik",
                   tokens=[], source_hash="h")
    other = Chunk(id="anderes.md::0", path="anderes.md", title="", section="", tags=[], text="heizung garten",
                  tokens=[], source_hash="h")
    engine._chunks = [empty, target, other]
    engine._bm25 = engine._build_bm25(engine._chunks)

    result = engine._keyword_scores("netscaler", None, None)
    scores = result[0] if isinstance(result, tuple) else result

    assert set(scores) == {"ziel.md::0"}, scores


# --- the ranking does not depend on max_results -------------------------------------------

WORDS = ("heizung garten keller dach fenster strom wasser zaehler router firewall tunnel backup "
         "server netz kabel lampe tuer schloss alarm sensor pumpe filter tank regen").split()


def test_more_results_extend_fewer_results(vault):
    rng = random.Random(7)
    for n in range(80):
        body = " ".join(rng.choice(WORDS) for _ in range(12))
        code = f" code{n:03d}" if n % 5 == 0 else ""
        (vault / f"n{n:03d}.md").write_text(f"# Notiz {n}\n\n{body}{code}\n", encoding="utf-8")
    engine = SemanticSearchEngine()
    queries = [" ".join(rng.sample(WORDS, 2)) + f" code{5 * rng.randrange(16):03d}" for _ in range(25)]

    differing = []
    for query in queries:
        few = paths(engine.search(query, max_results=3))
        many = paths(engine.search(query, max_results=20))
        if many[:3] != few:
            differing.append((query, few, many[:3]))

    assert differing == [], differing


def test_a_keyword_match_outside_the_semantic_candidates_keeps_its_similarity(vault, monkeypatch):
    """The exact similarity is read from the index for keyword candidates the semantic pool missed."""
    import obsidian_vault_mcp.retrieval.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_CANDIDATES", 1)
    (vault / "a.md").write_text("# A\n\nheizung heizung heizung\n", encoding="utf-8")
    (vault / "b.md").write_text("# B\n\nheizung garten code123\n", encoding="utf-8")

    hits = {hit["path"]: hit for hit in SemanticSearchEngine().search("heizung code123", max_results=5)["results"]}

    assert hits["b.md"]["keyword_score"] > 0
    assert hits["b.md"]["semantic_score"] > 0, hits
