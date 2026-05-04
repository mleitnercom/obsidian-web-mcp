"""Operational CLI for MCP tool-usage observability."""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_TOOL_START_RE = re.compile(r"Tool start:\s+(vault_[a-z_]+)(?:\s+\((.*)\))?")
_CONTEXT_RE = re.compile(r"([a-z_]+)=('(?:[^'\\]|\\.)*'|[^,]+)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vault-observe",
        description="Observability CLI for obsidian-web-mcp tool usage.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tool_usage = subparsers.add_parser("tool-usage", help="Summarize MCP tool usage from journalctl or a log file.")
    tool_usage.add_argument("--since", default="today", help="journalctl --since value when reading from systemd.")
    tool_usage.add_argument("--unit", default="obsidian-mcp", help="systemd unit name.")
    tool_usage.add_argument("--log-file", default="", help="Read log lines from a file instead of journalctl.")
    tool_usage.add_argument("--limit", type=int, default=200, help="Number of recent matching lines to analyze.")

    return parser


def _parse_context(context_blob: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key, raw_value in _CONTEXT_RE.findall(context_blob):
        value = raw_value.strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("\\'", "'").replace("\\n", "\n").replace("\\r", "\r")
        payload[key] = value
    return payload


def parse_tool_start_line(line: str) -> dict[str, Any] | None:
    """Parse one Tool start log line into structured fields."""
    match = _TOOL_START_RE.search(line)
    if not match:
        return None
    tool_name = match.group(1)
    context_blob = match.group(2) or ""
    context = _parse_context(context_blob)
    return {
        "tool": tool_name,
        "client_family": context.get("client_family", "unknown"),
        "client_ip": context.get("client_ip", ""),
        "user_agent": context.get("user_agent", ""),
        "mcp_protocol_version": context.get("mcp_protocol_version", ""),
        "request_path": context.get("request_path", ""),
        "raw": line.rstrip("\n"),
    }


def _read_log_lines(args: argparse.Namespace) -> list[str]:
    if args.log_file:
        return Path(args.log_file).read_text(encoding="utf-8").splitlines()

    cmd = [
        "journalctl",
        "-u",
        args.unit,
        "--since",
        args.since,
        "--no-pager",
    ]
    output = subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace")
    return output.splitlines()


def _usage_summary(events: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    if limit > 0:
        events = events[-limit:]

    tool_counts = Counter(event["tool"] for event in events)
    client_counts = Counter(event["client_family"] for event in events)
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        matrix[event["tool"]][event["client_family"]] += 1

    top_user_agents = Counter(
        event["user_agent"]
        for event in events
        if event["user_agent"]
    )
    semantic_events = [event for event in events if event["tool"] == "vault_semantic_search"]

    return {
        "event_count": len(events),
        "tool_counts": dict(tool_counts.most_common()),
        "client_family_counts": dict(client_counts.most_common()),
        "tool_by_client_family": {
            tool: dict(counter.most_common())
            for tool, counter in sorted(matrix.items())
        },
        "top_user_agents": dict(top_user_agents.most_common(10)),
        "semantic_search_count": len(semantic_events),
        "semantic_search_clients": dict(Counter(event["client_family"] for event in semantic_events).most_common()),
        "recent_semantic_examples": semantic_events[-5:],
    }


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "tool-usage":
        lines = _read_log_lines(args)
        events = [parsed for line in lines if (parsed := parse_tool_start_line(line))]
        print(json.dumps(_usage_summary(events, args.limit), indent=2, ensure_ascii=False))
        return

    parser.error("Unknown command")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
