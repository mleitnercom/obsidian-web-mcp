"""Semantic search tools for the Obsidian vault MCP server."""

from ..vault import vault_json_dumps

_engine = None


def set_engine(engine) -> None:
    """Inject the shared semantic engine from server startup."""
    global _engine
    _engine = engine


def vault_semantic_search(
    query: str,
    path_prefix: str | None = None,
    filter_tags: list[str] | None = None,
    search_mode: str = "hybrid",
    max_results: int = 10,
    min_score: float = 0.0,
) -> str:
    """Run hybrid semantic + keyword search across vault markdown notes."""
    if _engine is None:
        return vault_json_dumps({"error": "Semantic search engine is unavailable"})
    return vault_json_dumps(
        _engine.search(
            query=query,
            path_prefix=path_prefix,
            filter_tags=filter_tags,
            search_mode=search_mode,
            max_results=max_results,
            min_score=min_score,
        )
    )


def vault_reindex(full: bool = True) -> str:
    """Rebuild the semantic-search cache from the current vault contents."""
    if _engine is None:
        return vault_json_dumps({"error": "Semantic search engine is unavailable"})
    return vault_json_dumps(_engine.reindex(full=full))
