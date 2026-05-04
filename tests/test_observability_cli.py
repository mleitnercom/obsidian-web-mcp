"""Tests for MCP tool-usage observability parsing and summarization."""

from obsidian_vault_mcp.observability_cli import parse_tool_start_line, _usage_summary


def test_parse_tool_start_line_extracts_client_metadata():
    line = (
        "INFO Tool start: vault_semantic_search "
        "(client_family='claude', client_ip='203.0.113.8', "
        "mcp_protocol_version='2025-06-18', user_agent='Claude-Connector/1.0', "
        "request_path='/mcp', query='semantic indexing')"
    )

    parsed = parse_tool_start_line(line)

    assert parsed is not None
    assert parsed["tool"] == "vault_semantic_search"
    assert parsed["client_family"] == "claude"
    assert parsed["client_ip"] == "203.0.113.8"
    assert parsed["mcp_protocol_version"] == "2025-06-18"
    assert parsed["user_agent"] == "Claude-Connector/1.0"
    assert parsed["request_path"] == "/mcp"


def test_parse_tool_start_line_handles_wrapped_journal_message():
    line = """INFO Tool start: vault_read
    (client_family='claude',
    client_ip='160.79.106.35',
    mcp_protocol_version='2025-11-25',
    user_agent='Claude-User',
    request_path='/mcp',
    path='99_meta/Task-OS/Projekt-Register.md')"""

    parsed = parse_tool_start_line(line)

    assert parsed is not None
    assert parsed["tool"] == "vault_read"
    assert parsed["client_family"] == "claude"
    assert parsed["client_ip"] == "160.79.106.35"
    assert parsed["mcp_protocol_version"] == "2025-11-25"
    assert parsed["user_agent"] == "Claude-User"
    assert parsed["request_path"] == "/mcp"


def test_usage_summary_groups_by_tool_and_client_family():
    events = [
        {"tool": "vault_search", "client_family": "claude", "user_agent": "Claude", "client_ip": "", "mcp_protocol_version": "", "request_path": ""},
        {"tool": "vault_search", "client_family": "chatgpt", "user_agent": "ChatGPT", "client_ip": "", "mcp_protocol_version": "", "request_path": ""},
        {"tool": "vault_semantic_search", "client_family": "claude", "user_agent": "Claude", "client_ip": "", "mcp_protocol_version": "", "request_path": ""},
    ]

    summary = _usage_summary(events, limit=200)

    assert summary["event_count"] == 3
    assert summary["tool_counts"]["vault_search"] == 2
    assert summary["tool_counts"]["vault_semantic_search"] == 1
    assert summary["client_family_counts"]["claude"] == 2
    assert summary["tool_by_client_family"]["vault_search"]["chatgpt"] == 1
    assert summary["semantic_search_count"] == 1
    assert summary["semantic_search_clients"]["claude"] == 1
