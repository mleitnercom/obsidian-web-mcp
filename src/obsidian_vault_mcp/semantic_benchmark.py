"""Benchmark semantic, keyword, and hybrid query modes."""

import argparse
import json
import logging
import statistics
import sys
import time

from .retrieval import SemanticSearchEngine


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vault-semantic-benchmark",
        description="Benchmark semantic-search query modes.",
    )
    parser.add_argument("query", help="Query to benchmark.")
    parser.add_argument(
        "--mode",
        choices=("hybrid", "semantic", "keyword"),
        action="append",
        dest="modes",
        help="Mode(s) to benchmark. Repeat to limit the set.",
    )
    parser.add_argument("--iterations", type=int, default=5, help="Runs per mode.")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup runs per mode.")
    parser.add_argument("--max-results", type=int, default=10, help="Maximum results to request.")
    parser.add_argument("--path-prefix", default=None, help="Restrict search to a path prefix.")
    parser.add_argument("--tag", action="append", dest="tags", default=None, help="Filter by tag. Repeatable.")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    modes = args.modes or ["hybrid", "semantic", "keyword"]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    engine = SemanticSearchEngine()
    engine.initialize()

    summary: dict[str, object] = {
        "query": args.query,
        "iterations": args.iterations,
        "warmup": args.warmup,
        "results": [],
    }

    for mode in modes:
        for _ in range(max(args.warmup, 0)):
            engine.search(
                query=args.query,
                path_prefix=args.path_prefix,
                filter_tags=args.tags,
                search_mode=mode,
                max_results=args.max_results,
            )

        timings: list[float] = []
        last_result: dict | None = None
        for _ in range(max(args.iterations, 1)):
            started = time.perf_counter()
            last_result = engine.search(
                query=args.query,
                path_prefix=args.path_prefix,
                filter_tags=args.tags,
                search_mode=mode,
                max_results=args.max_results,
            )
            timings.append(time.perf_counter() - started)

        assert last_result is not None
        summary["results"].append(
            {
                "mode": mode,
                "mean_ms": round(statistics.fmean(timings) * 1000, 3),
                "min_ms": round(min(timings) * 1000, 3),
                "max_ms": round(max(timings) * 1000, 3),
                "total_results": last_result.get("total", 0),
                "top_result": (last_result.get("results") or [{}])[0],
            }
        )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
