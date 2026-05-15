"""Tests for Obsidian Canvas tools."""

import json
import re
from contextlib import contextmanager

from obsidian_vault_mcp import config, server
from obsidian_vault_mcp.rate_limit import (
    reset_current_auth_principal,
    reset_current_request_metadata,
    set_current_auth_principal,
    set_current_request_metadata,
)
from obsidian_vault_mcp.tools.canvas import (
    CanvasNodePayload,
    vault_canvas_add_edge,
    vault_canvas_add_node,
    vault_canvas_read,
)


@contextmanager
def _authenticated_tool_context():
    principal = set_current_auth_principal("canvas-audit-token")
    metadata = set_current_request_metadata({"client_family": "pytest", "request_id": "canvas-req"})
    try:
        yield
    finally:
        reset_current_request_metadata(metadata)
        reset_current_auth_principal(principal)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_canvas_roundtrip_preserves_existing_order_and_fields(vault_dir):
    (vault_dir / "map.canvas").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "alpha",
                        "type": "text",
                        "text": "Alpha",
                        "x": 0,
                        "y": 0,
                        "width": 200,
                        "height": 120,
                        "color": "1",
                    },
                    {
                        "id": "beta",
                        "type": "text",
                        "text": "Beta",
                        "x": 300,
                        "y": 0,
                        "width": 200,
                        "height": 120,
                    },
                ],
                "edges": [
                    {
                        "id": "edge1",
                        "fromNode": "alpha",
                        "fromSide": "right",
                        "toNode": "beta",
                        "toSide": "left",
                        "label": "existing",
                    }
                ],
                "extraTopLevel": {"keep": True},
            }
        ),
        encoding="utf-8",
    )

    initial = json.loads(vault_canvas_read("map.canvas"))
    assert [node["id"] for node in initial["nodes"]] == ["alpha", "beta"]

    node_result = json.loads(
        vault_canvas_add_node(
            "map.canvas",
            {
                "type": "text",
                "text": "Gamma",
                "x": 600,
                "y": 0,
                "width": 200,
                "height": 120,
                "backgroundStyle": "cover",
            },
        )
    )
    assert "error" not in node_result
    generated_node_id = node_result["node"]["id"]
    assert re.fullmatch(r"[A-Za-z0-9]+", generated_node_id)

    edge_result = json.loads(
        vault_canvas_add_edge(
            "map.canvas",
            {
                "fromNode": "beta",
                "fromSide": "right",
                "toNode": generated_node_id,
                "toSide": "left",
                "label": "new",
            },
        )
    )
    assert "error" not in edge_result
    assert re.fullmatch(r"[A-Za-z0-9]+", edge_result["edge"]["id"])

    final = json.loads(vault_canvas_read("map.canvas"))
    assert [node["id"] for node in final["nodes"]] == ["alpha", "beta", generated_node_id]
    assert [edge["id"] for edge in final["edges"]] == ["edge1", edge_result["edge"]["id"]]
    assert final["nodes"][0]["color"] == "1"
    assert final["nodes"][2]["backgroundStyle"] == "cover"
    assert final["edges"][0]["label"] == "existing"
    assert final["edges"][1]["label"] == "new"

    raw = json.loads((vault_dir / "map.canvas").read_text(encoding="utf-8"))
    assert raw["extraTopLevel"] == {"keep": True}


def test_canvas_add_node_creates_new_canvas(vault_dir):
    result = json.loads(
        vault_canvas_add_node(
            "new.canvas",
            {"type": "text", "text": "Hello", "x": 0, "y": 0, "width": 100, "height": 80},
        )
    )

    assert result["created"] is True
    assert result["nodes"][0]["text"] == "Hello"
    assert json.loads(vault_canvas_read("new.canvas"))["edges"] == []


def test_canvas_read_invalid_json_has_error_code(vault_dir):
    (vault_dir / "broken.canvas").write_text("{nope", encoding="utf-8")

    result = json.loads(vault_canvas_read("broken.canvas"))

    assert result["error_code"] == "invalid_canvas_format"


def test_canvas_read_missing_and_path_policy_error_codes(vault_dir, monkeypatch):
    missing = json.loads(vault_canvas_read("missing.canvas"))
    assert missing["error_code"] == "file_not_found"

    monkeypatch.setattr(server.config, "INCLUDED_ROOTS", ["Allowed"])
    result = json.loads(vault_canvas_read("Blocked/map.canvas"))
    assert result["error_code"] == "path_not_allowed"


def test_canvas_add_edge_rejects_missing_file_and_invalid_reference(vault_dir):
    missing = json.loads(
        vault_canvas_add_edge(
            "missing.canvas",
            {"fromNode": "a", "fromSide": "right", "toNode": "b", "toSide": "left"},
        )
    )
    assert missing["error_code"] == "file_not_found"

    (vault_dir / "map.canvas").write_text(
        json.dumps(
            {
                "nodes": [{"id": "a", "type": "text", "x": 0, "y": 0, "width": 100, "height": 80}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    bad_ref = json.loads(
        vault_canvas_add_edge(
            "map.canvas",
            {"fromNode": "a", "fromSide": "right", "toNode": "missing", "toSide": "left"},
        )
    )
    assert bad_ref["error_code"] == "invalid_edge_reference"


def test_canvas_schema_validation_error_codes(vault_dir):
    bad_node = json.loads(vault_canvas_add_node("map.canvas", {"type": "text", "x": 0}))
    assert bad_node["error_code"] == "invalid_node_schema"

    (vault_dir / "map.canvas").write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "a", "type": "text", "x": 0, "y": 0, "width": 100, "height": 80},
                    {"id": "b", "type": "text", "x": 200, "y": 0, "width": 100, "height": 80},
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    bad_edge = json.loads(
        vault_canvas_add_edge(
            "map.canvas",
            {"fromNode": "a", "fromSide": "diagonal", "toNode": "b", "toSide": "left"},
        )
    )
    assert bad_edge["error_code"] == "invalid_edge_schema"


def test_canvas_tools_are_discoverable_with_schema_hints():
    read_tool = server.mcp._tool_manager.get_tool("vault_canvas_read")
    node_tool = server.mcp._tool_manager.get_tool("vault_canvas_add_node")
    edge_tool = server.mcp._tool_manager.get_tool("vault_canvas_add_edge")

    assert read_tool is not None
    assert node_tool is not None
    assert edge_tool is not None
    assert "invalid_canvas_format" in read_tool.description
    assert "invalid_node_schema" in node_tool.description
    assert "invalid_edge_reference" in edge_tool.description

    edge_schema = edge_tool.parameters
    edge_ref = edge_schema["properties"]["edge"]["$ref"]
    edge_def = edge_schema["$defs"][edge_ref.rsplit("/", 1)[-1]]
    assert edge_def["properties"]["fromSide"]["enum"] == ["top", "right", "bottom", "left"]
    assert edge_def["properties"]["toSide"]["enum"] == ["top", "right", "bottom", "left"]
    assert "top, right, bottom, left" in edge_def["properties"]["fromSide"]["description"]
    assert "top, right, bottom, left" in edge_def["properties"]["toSide"]["description"]


def test_canvas_write_tools_emit_audit_records(vault_dir, monkeypatch, tmp_path):
    """Canvas write wrappers should participate in the v0.6.8 audit pipeline."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))

    with _authenticated_tool_context():
        result = json.loads(
            server.vault_canvas_add_node(
                "audit.canvas",
                CanvasNodePayload(type="text", text="Audited", x=0, y=0, width=200, height=120),
            )
        )

    assert "error" not in result
    records = _read_jsonl(audit_path)
    assert len(records) == 1
    assert records[0]["operation"] == "vault_canvas_add_node"
    assert records[0]["target_path"] == "audit.canvas"
    assert records[0]["operation_status"] == "success"
    assert records[0]["client_id"] == "pytest"
