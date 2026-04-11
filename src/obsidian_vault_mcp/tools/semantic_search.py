"""Semantic search tools for the Obsidian vault MCP server."""

from ..vault import vault_json_dumps

_engine = None


def set_engine(engine) -> None:
    """Inject the shared semantic engine from server startup."""
    global _engine
    _engine = engine


def vault_semantic_search(query: str, path_prefix: str | None = None, max_results: int = 10) -> str:
    """Run hybrid semantic + keyword search across vault markdown notes."""
    if _engine is None:
        return vault_json_dumps({"error": "Semantic search engine is unavailable"})
    return vault_json_dumps(_engine.search(query=query, path_prefix=path_prefix, max_results=max_results))


def vault_reindex(full: bool = True) -> str:
    """Rebuild the semantic-search cache from the current vault contents."""
    if _engine is None:
        return vault_json_dumps({"error": "Semantic search engine is unavailable"})
    if not full:
        return vault_json_dumps({"error": "Incremental reindex is not implemented yet; use full=true"})
    return vault_json_dumps(_engine.reindex())

