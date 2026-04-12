"""Operational CLI for semantic-search maintenance and diagnostics."""

import argparse
import json
import logging
import sys

from .retrieval import SemanticSearchEngine


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vault-semantic",
        description="Semantic-search maintenance CLI for obsidian-web-mcp.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show semantic engine status.")
    status.add_argument("--init", action="store_true", help="Initialize the engine before reporting status.")

    reindex = subparsers.add_parser("reindex", help="Rebuild the semantic cache.")
    reindex.add_argument(
        "--mode",
        choices=("full", "incremental"),
        default="full",
        help="Choose a full rebuild or incremental refresh.",
    )

    search = subparsers.add_parser("search", help="Run a semantic/keyword/hybrid query.")
    search.add_argument("query", help="Natural-language query.")
    search.add_argument("--path-prefix", default=None, help="Restrict results to a path prefix.")
    search.add_argument("--tag", action="append", dest="tags", default=None, help="Filter by tag. Repeatable.")
    search.add_argument(
        "--mode",
        choices=("hybrid", "semantic", "keyword"),
        default="hybrid",
        help="Ranking mode to use.",
    )
    search.add_argument("--max-results", type=int, default=10, help="Maximum results to return.")
    search.add_argument("--min-score", type=float, default=0.0, help="Minimum score threshold.")

    doctor = subparsers.add_parser("doctor", help="Run basic semantic diagnostics.")
    doctor.add_argument("--init", action="store_true", help="Initialize the engine before running checks.")

    return parser


def _make_engine() -> SemanticSearchEngine:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return SemanticSearchEngine()


def _status_payload(engine: SemanticSearchEngine) -> dict:
    payload = dict(engine.status)
    cache_path = engine.status["cache_path"]
    payload["cache_files"] = {
        "faiss_index": f"{cache_path}/faiss.index",
        "chunks": f"{cache_path}/chunks.json",
        "manifest": f"{cache_path}/manifest.json",
        "path_index": f"{cache_path}/path_index.json",
    }
    return payload


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    engine = _make_engine()

    if args.command == "status":
        if args.init:
            engine.initialize()
        print(json.dumps(_status_payload(engine), indent=2))
        return

    if args.command == "reindex":
        result = engine.reindex(full=(args.mode == "full"))
        print(json.dumps(result, indent=2))
        return

    if args.command == "search":
        result = engine.search(
            query=args.query,
            path_prefix=args.path_prefix,
            filter_tags=args.tags,
            search_mode=args.mode,
            max_results=args.max_results,
            min_score=args.min_score,
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "doctor":
        payload = {
            "status_before_init": _status_payload(engine),
        }
        if args.init:
            try:
                engine.initialize()
            except Exception as exc:  # pragma: no cover - defensive CLI path
                payload["initialize_error"] = str(exc)
            payload["status_after_init"] = _status_payload(engine)
        print(json.dumps(payload, indent=2))
        return


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
