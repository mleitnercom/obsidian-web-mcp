"""Tools for reading and updating Obsidian Canvas files."""

from __future__ import annotations

import json
import logging
import secrets
import string
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..vault import read_file, resolve_vault_path, vault_json_dumps
from .write import vault_write

logger = logging.getLogger(__name__)

CANVAS_SIDE_VALUES = {"top", "right", "bottom", "left"}
CANVAS_ID_ALPHABET = string.ascii_letters + string.digits


class CanvasError(ValueError):
    """Expected Canvas tool failure with a stable MCP-facing error code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class CanvasNodePayload(BaseModel):
    """Minimal Obsidian Canvas node schema while preserving extra node fields."""

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(
        default=None,
        description="Optional alphanumeric node id. Generated when omitted.",
    )
    type: str = Field(..., description="Canvas node type such as text, file, link, or group.")
    x: int | float = Field(..., description="Canvas x coordinate.")
    y: int | float = Field(..., description="Canvas y coordinate.")
    width: int | float = Field(..., gt=0, description="Canvas node width.")
    height: int | float = Field(..., gt=0, description="Canvas node height.")

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or not value.isalnum():
            raise ValueError("id must be alphanumeric when provided")
        return value


class CanvasEdgePayload(BaseModel):
    """Minimal Obsidian Canvas edge schema while preserving extra edge fields."""

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(
        default=None,
        description="Optional alphanumeric edge id. Generated when omitted.",
    )
    fromNode: str = Field(..., min_length=1, description="Existing source node id.")
    fromSide: Literal["top", "right", "bottom", "left"] = Field(..., description="Allowed values: top, right, bottom, left.")
    toNode: str = Field(..., min_length=1, description="Existing target node id.")
    toSide: Literal["top", "right", "bottom", "left"] = Field(..., description="Allowed values: top, right, bottom, left.")

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or not value.isalnum():
            raise ValueError("id must be alphanumeric when provided")
        return value

    @field_validator("fromSide", "toSide")
    @classmethod
    def validate_side(cls, value: str) -> str:
        if value not in CANVAS_SIDE_VALUES:
            raise ValueError("side must be one of: top, right, bottom, left")
        return value


def _error(error_code: str, message: str, **extra: Any) -> str:
    payload = {"error": message, "error_code": error_code}
    payload.update(extra)
    return vault_json_dumps(payload)


def _ensure_canvas_path(path: str) -> None:
    resolve_vault_path(path)
    if not path.lower().endswith(".canvas"):
        raise CanvasError("invalid_canvas_format", "Canvas path must end with .canvas")


def _load_canvas(path: str, *, must_exist: bool) -> dict[str, Any]:
    _ensure_canvas_path(path)
    try:
        content, _metadata = read_file(path)
    except FileNotFoundError:
        if must_exist:
            raise CanvasError("file_not_found", f"Canvas file not found: {path}") from None
        return {"nodes": [], "edges": []}

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CanvasError("invalid_canvas_format", f"Invalid Canvas JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise CanvasError("invalid_canvas_format", "Canvas file must contain a JSON object")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise CanvasError("invalid_canvas_format", "Canvas file must contain nodes and edges arrays")
    if not all(isinstance(item, dict) for item in nodes):
        raise CanvasError("invalid_canvas_format", "Canvas nodes must be JSON objects")
    if not all(isinstance(item, dict) for item in edges):
        raise CanvasError("invalid_canvas_format", "Canvas edges must be JSON objects")
    return payload


def _generate_canvas_id(existing_ids: set[str]) -> str:
    while True:
        candidate = "".join(secrets.choice(CANVAS_ID_ALPHABET) for _ in range(16))
        if candidate not in existing_ids:
            return candidate


def _existing_ids(items: list[dict[str, Any]]) -> set[str]:
    return {item["id"] for item in items if isinstance(item.get("id"), str)}


def _json_for_write(canvas: dict[str, Any]) -> str:
    return vault_json_dumps(canvas, ensure_ascii=False, indent=2) + "\n"


def _write_canvas(path: str, canvas: dict[str, Any]) -> dict[str, Any]:
    write_result = json.loads(vault_write(path, _json_for_write(canvas), create_dirs=True))
    if "error" in write_result:
        raise CanvasError("write_verification_failed", write_result["error"])
    return write_result


def vault_canvas_read(path: str) -> str:
    """Read and parse an Obsidian .canvas file."""
    try:
        canvas = _load_canvas(path, must_exist=True)
        return vault_json_dumps({"path": path, "nodes": canvas["nodes"], "edges": canvas["edges"]})
    except CanvasError as exc:
        return _error(exc.error_code, str(exc), path=path)
    except ValueError as exc:
        return _error("path_not_allowed", str(exc), path=path)
    except Exception as exc:
        logger.error("vault_canvas_read error for %s: %s", path, exc)
        return _error("invalid_canvas_format", str(exc), path=path)


def vault_canvas_add_node(path: str, node: dict[str, Any]) -> str:
    """Append a node to an Obsidian .canvas file, creating the file when missing."""
    try:
        canvas = _load_canvas(path, must_exist=False)
        try:
            parsed = CanvasNodePayload.model_validate(node)
        except ValidationError as exc:
            return _error("invalid_node_schema", str(exc), path=path)

        node_payload = parsed.model_dump(exclude_none=True, mode="json")
        node_ids = _existing_ids(canvas["nodes"])
        edge_ids = _existing_ids(canvas["edges"])
        if "id" not in node_payload:
            node_payload["id"] = _generate_canvas_id(node_ids | edge_ids)
        elif node_payload["id"] in node_ids or node_payload["id"] in edge_ids:
            return _error("invalid_node_schema", f"Node id already exists: {node_payload['id']}", path=path)

        canvas["nodes"].append(node_payload)
        write_result = _write_canvas(path, canvas)
        return vault_json_dumps(
            {
                "path": path,
                "node": node_payload,
                "nodes": canvas["nodes"],
                "edges": canvas["edges"],
                **write_result,
            }
        )
    except CanvasError as exc:
        return _error(exc.error_code, str(exc), path=path)
    except ValueError as exc:
        return _error("path_not_allowed", str(exc), path=path)
    except Exception as exc:
        logger.error("vault_canvas_add_node error for %s: %s", path, exc)
        return _error("write_verification_failed", str(exc), path=path)


def vault_canvas_add_edge(path: str, edge: dict[str, Any]) -> str:
    """Append an edge to an existing Obsidian .canvas file."""
    try:
        canvas = _load_canvas(path, must_exist=True)
        try:
            parsed = CanvasEdgePayload.model_validate(edge)
        except ValidationError as exc:
            return _error("invalid_edge_schema", str(exc), path=path)

        edge_payload = parsed.model_dump(exclude_none=True, mode="json")
        node_ids = _existing_ids(canvas["nodes"])
        if edge_payload["fromNode"] not in node_ids or edge_payload["toNode"] not in node_ids:
            return _error(
                "invalid_edge_reference",
                "fromNode and toNode must reference existing canvas node ids",
                path=path,
            )

        edge_ids = _existing_ids(canvas["edges"])
        if "id" not in edge_payload:
            edge_payload["id"] = _generate_canvas_id(node_ids | edge_ids)
        elif edge_payload["id"] in edge_ids or edge_payload["id"] in node_ids:
            return _error("invalid_edge_schema", f"Edge id already exists: {edge_payload['id']}", path=path)

        canvas["edges"].append(edge_payload)
        write_result = _write_canvas(path, canvas)
        return vault_json_dumps(
            {
                "path": path,
                "edge": edge_payload,
                "nodes": canvas["nodes"],
                "edges": canvas["edges"],
                **write_result,
            }
        )
    except CanvasError as exc:
        return _error(exc.error_code, str(exc), path=path)
    except ValueError as exc:
        return _error("path_not_allowed", str(exc), path=path)
    except Exception as exc:
        logger.error("vault_canvas_add_edge error for %s: %s", path, exc)
        return _error("write_verification_failed", str(exc), path=path)
