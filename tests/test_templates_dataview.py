"""Tests for P2 template and Dataview tools."""

import json
import urllib.error

from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import config, obsidian_rest, server
from obsidian_vault_mcp.obsidian_rest import ObsidianRestError
from obsidian_vault_mcp.tools import templates


def test_template_list_uses_configured_folder(vault_dir, monkeypatch):
    folder = vault_dir / "Templates"
    folder.mkdir()
    (folder / "Daily.md").write_text("Hello {{title}}\n", encoding="utf-8")
    nested = folder / "Nested"
    nested.mkdir()
    (nested / "Project.md").write_text("Project {{variables.client}}\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "Templates")

    result = json.loads(templates.vault_template_list())

    assert result["total"] == 2
    assert [item["path"] for item in result["templates"]] == [
        "Templates/Daily.md",
        "Templates/Nested/Project.md",
    ]


def test_template_list_missing_folder_is_graceful(vault_dir, monkeypatch):
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "")

    result = json.loads(templates.vault_template_list())

    assert result["error_code"] == "template_folder_missing"
    assert "VAULT_TEMPLATER_FOLDER" in result["error"]


def test_template_render_supports_builtin_and_variable_tokens(vault_dir, monkeypatch):
    folder = vault_dir / "Templates"
    folder.mkdir()
    (folder / "Note.md").write_text(
        "# {{title}}\nPath: {{target_path}}\nClient: {{variables.client}}\nEmpty: '{{empty}}'\nNone: '{{none_value}}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "Templates")

    result = json.loads(
        templates.vault_template_render(
            "Note.md",
            target_path_hint="Projects/Acme.md",
            variables={"client": "ACME", "empty": "", "none_value": None},
        )
    )

    assert result["engine"] == "simple"
    assert "# Acme" in result["content"]
    assert "Path: Projects/Acme.md" in result["content"]
    assert "Client: ACME" in result["content"]
    assert "Empty: ''" in result["content"]
    assert "None: ''" in result["content"]


def test_template_render_missing_variable_fails_hard(vault_dir, monkeypatch):
    folder = vault_dir / "Templates"
    folder.mkdir()
    (folder / "Note.md").write_text("Hello {{missing}}\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "Templates")

    result = json.loads(templates.vault_template_render("Note.md"))

    assert result["error_code"] == "template_render_failed"
    assert "Missing template variable" in result["error"]


def test_template_render_rejects_templater_syntax(vault_dir, monkeypatch):
    folder = vault_dir / "Templates"
    folder.mkdir()
    (folder / "Templater.md").write_text("<% tp.date.now() %>\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "Templates")

    result = json.loads(templates.vault_template_render("Templater.md"))

    assert result["error_code"] == "template_render_unavailable"
    assert result["error"] == "Templater syntax detected; this server supports {{ }} substitution only."


def test_template_apply_respects_overwrite_and_uses_verified_write(vault_dir, monkeypatch):
    folder = vault_dir / "Templates"
    folder.mkdir()
    (folder / "Note.md").write_text("# {{title}}\n{{variables.body}}\n", encoding="utf-8")
    target = vault_dir / "Generated" / "Note.md"
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "Templates")

    created = json.loads(
        templates.vault_template_apply(
            "Note.md",
            "Generated/Note.md",
            variables={"body": "First"},
        )
    )
    blocked = json.loads(
        templates.vault_template_apply(
            "Note.md",
            "Generated/Note.md",
            variables={"body": "Second"},
        )
    )
    overwritten = json.loads(
        templates.vault_template_apply(
            "Note.md",
            "Generated/Note.md",
            variables={"body": "Second"},
            overwrite=True,
        )
    )

    assert created["created"] is True
    assert blocked["error_code"] == "target_exists"
    assert overwritten["created"] is False
    assert target.read_text(encoding="utf-8") == "# Note\nSecond\n"


def test_template_apply_rejects_target_outside_policy(vault_dir, monkeypatch):
    folder = vault_dir / "Templates"
    folder.mkdir()
    (folder / "Note.md").write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "Templates")
    monkeypatch.setattr(config, "INCLUDED_ROOTS", ["Allowed"])

    result = json.loads(templates.vault_template_apply("Note.md", "Blocked/Note.md"))

    assert result["error_code"] == "path_not_allowed"
    assert "VAULT_INCLUDED_ROOTS" in result["error"]


def test_dataview_query_success_maps_columns_rows_and_duration(monkeypatch):
    def fake_request(path, *, method, body, content_type, timeout):
        assert path == "/search/"
        assert method == "POST"
        assert body == 'TABLE file.mtime, status FROM "" LIMIT 3'
        assert content_type == templates.DATAVIEW_DQL_CONTENT_TYPE
        return 200, json.dumps(
            [
                {"filename": "a.md", "result": {"mtime": "2026-05-01", "status": "open"}},
                {"filename": "b.md", "result": {"mtime": "2026-05-02", "status": "done"}},
                {"filename": "c.md", "result": {"mtime": "2026-05-03", "status": "next"}},
            ]
        ).encode("utf-8")

    monkeypatch.setattr(templates, "obsidian_rest_request", fake_request)

    result = json.loads(templates.vault_dataview_query('TABLE file.mtime, status FROM "" LIMIT 3'))

    assert result["type"] == "table"
    assert result["columns"] == ["filename", "mtime", "status"]
    assert [row["filename"] for row in result["rows"]] == ["a.md", "b.md", "c.md"]
    assert result["duration_ms"] >= 0


def test_dataview_query_empty_url_is_capability_unavailable(monkeypatch):
    def fake_request(*args, **kwargs):
        raise ObsidianRestError(
            "capability_unavailable",
            "VAULT_OBSIDIAN_REST_URL is not configured; Obsidian Local REST API features are disabled.",
        )

    monkeypatch.setattr(templates, "obsidian_rest_request", fake_request)

    result = json.loads(templates.vault_dataview_query('TABLE file.mtime FROM "" LIMIT 1'))

    assert result["error_code"] == "capability_unavailable"


def test_dataview_query_wrong_auth_is_graceful(monkeypatch):
    def fake_request(*args, **kwargs):
        raise ObsidianRestError("rest_auth_failed", "Obsidian Local REST API rejected the API key.", 401)

    monkeypatch.setattr(templates, "obsidian_rest_request", fake_request)

    result = json.loads(templates.vault_dataview_query('TABLE file.mtime FROM "" LIMIT 1'))

    assert result["error_code"] == "rest_auth_failed"
    assert "API key" in result["error"]


def test_dataview_query_table_without_id_has_clear_message(monkeypatch):
    def fake_request(*args, **kwargs):
        raise ObsidianRestError("rest_bad_request", "TABLE WITHOUT ID queries are not supported.", 400)

    monkeypatch.setattr(templates, "obsidian_rest_request", fake_request)

    result = json.loads(templates.vault_dataview_query('TABLE WITHOUT ID status FROM ""'))

    assert result["error_code"] == "dataview_query_failed"
    assert result["error"] == "TABLE WITHOUT ID is not supported by Local REST API."


def test_rest_client_maps_http_401_to_rest_auth_failed(monkeypatch):
    class FakeHttpError(urllib.error.HTTPError):
        def read(self):
            return b"Unauthorized"

    def fake_urlopen(*args, **kwargs):
        raise FakeHttpError("https://127.0.0.1:27124/search/", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(config, "VAULT_OBSIDIAN_REST_URL", "https://127.0.0.1:27124")
    monkeypatch.setattr(config, "VAULT_OBSIDIAN_REST_API_KEY", "wrong")
    monkeypatch.setattr(obsidian_rest.urllib.request, "urlopen", fake_urlopen)

    try:
        obsidian_rest.obsidian_rest_request("/search/")
    except ObsidianRestError as exc:
        assert exc.error_code == "rest_auth_failed"
    else:
        raise AssertionError("expected ObsidianRestError")


def test_obsidian_rest_tls_warning_heuristic():
    assert obsidian_rest.obsidian_rest_tls_warning("https://127.0.0.1:27124", False) is False
    assert obsidian_rest.obsidian_rest_tls_warning("https://localhost:27124", False) is False
    assert obsidian_rest.obsidian_rest_tls_warning("https://vault.example.com", False) is True
    assert obsidian_rest.obsidian_rest_tls_warning("https://vault.example.com", True) is False


def test_health_reports_obsidian_rest_status(vault_dir, monkeypatch):
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(server.config, "VAULT_OBSIDIAN_REST_URL", "https://vault.example.com")
    monkeypatch.setattr(server.config, "VAULT_OBSIDIAN_REST_VERIFY_TLS", False)
    monkeypatch.setattr(server, "obsidian_rest_reachable", lambda timeout=2: True)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    body = response.json()
    assert body["obsidian_rest"] == {
        "url_configured": True,
        "verify_tls": False,
        "tls_warning": True,
        "reachable": True,
    }


def test_health_omits_obsidian_rest_details_when_url_empty(vault_dir, monkeypatch):
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(server.config, "VAULT_OBSIDIAN_REST_URL", "")

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.json()["obsidian_rest"] == {"url_configured": False}


def test_p2_tool_schemas_are_discoverable_and_strict():
    tool_names = [
        "vault_template_list",
        "vault_template_render",
        "vault_template_apply",
        "vault_dataview_query",
    ]

    for tool_name in tool_names:
        assert server.mcp._tool_manager.get_tool(tool_name) is not None

    dataview_schema = server.mcp._tool_manager.get_tool("vault_dataview_query").parameters
    assert dataview_schema["properties"]["query_type"]["enum"] == ["dql"]
    assert "js" not in json.dumps(dataview_schema).lower()

    render_tool = server.mcp._tool_manager.get_tool("vault_template_render")
    apply_tool = server.mcp._tool_manager.get_tool("vault_template_apply")
    assert "Simple variable substitution, not full Templater execution." in render_tool.description
    assert "Simple variable substitution, not full Templater execution." in apply_tool.description
    assert render_tool.parameters["properties"]["engine"]["enum"] == ["simple"]
    assert apply_tool.parameters["properties"]["engine"]["enum"] == ["simple"]
