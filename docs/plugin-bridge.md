# Plugin Bridge

Back to [README](../README.md).

The Plugin Bridge is optional. It lets the MCP server call an already-running Obsidian instance through the Local REST API community plugin.

## Tested Baseline

Research for this fork used Local REST API `3.6.x` as the baseline. The plugin exposes:

- `GET /openapi.yaml`
- `GET /commands/`
- `POST /commands/{commandId}/`
- `POST /search/`

Authentication uses:

```http
Authorization: Bearer <api_key>
```

Default local endpoints:

- HTTPS: `https://127.0.0.1:27124`
- HTTP: `http://127.0.0.1:27123` when enabled in the plugin

The HTTPS endpoint commonly uses a self-signed certificate.

## Configuration

```env
VAULT_OBSIDIAN_REST_URL=https://127.0.0.1:27124
VAULT_OBSIDIAN_REST_API_KEY=REPLACE_WITH_LOCAL_REST_API_KEY
VAULT_OBSIDIAN_REST_VERIFY_TLS=false
VAULT_OBSIDIAN_REST_TIMEOUT=15
VAULT_TEMPLATER_FOLDER=Templates
VAULT_DATAVIEW_TIMEOUT=15
```

If `VAULT_OBSIDIAN_REST_URL` is empty, Plugin Bridge tools return `capability_unavailable`.

## Headless Obsidian Notes

For a headless Linux deployment, Obsidian usually runs under Xvfb. The Local REST API plugin must be installed and enabled inside the vault profile used by that headless Obsidian instance. If the plugin files exist but port `27124` does not listen, the plugin likely still needs UI activation.

## Templater

Templater does not provide a stable REST endpoint that renders a template and returns content. The Local REST command API can trigger commands, but it does not accept structured render parameters and does not return rendered text.

This server therefore implements:

- `vault_template_list`: lists markdown templates from `VAULT_TEMPLATER_FOLDER`.
- `vault_template_render`: simple `{{token}}` substitution only.
- `vault_template_apply`: simple substitution plus verified vault write.

Templates containing `<%`, `<%-`, `<%*`, `<%~`, or `<%+` are rejected with `template_render_unavailable`.

## Dataview

`vault_dataview_query` uses Local REST API `POST /search/` and supports DQL TABLE queries only.

Not supported:

- DataviewJS
- `TABLE WITHOUT ID`
- Non-table result types

The response is normalized to:

```json
{
  "type": "table",
  "columns": ["filename", "..."],
  "rows": [],
  "duration_ms": 12
}
```

## Error Codes

Common bridge errors:

- `capability_unavailable`: bridge URL not configured
- `plugin_unavailable`: Local REST API unreachable
- `rest_auth_failed`: API key rejected
- `rest_timeout`: Local REST request timed out
- `template_folder_missing`
- `template_not_found`
- `template_render_unavailable`
- `dataview_unavailable`
- `dataview_query_failed`
