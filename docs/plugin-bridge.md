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

## Dataview (removed in v0.8.21)

`vault_dataview_query` ran DQL through Local REST API `POST /search/` using the
`application/vnd.olrapi.dataview.dql+txt` content type. **The tool no longer exists.**

Obsidian Local REST API 4.0 removed its Dataview dependency, and with it that content
type. Since then the endpoint answers `HTTP 400 / errorCode 40012` ("Unknown or invalid
Content-Type") for every DQL request. There is no successor content type: `/search/`
now speaks JsonLogic, which filters notes but returns whole `NoteJson` objects instead
of projected columns -- so it cannot serve the reason DQL existed here.

Pinning the plugin to a pre-4.0 release was rejected: the fix for the path-traversal
advisory GHSA-62gx-5q78-wrvx only landed in 4.1.3, so downgrading would trade a working
convenience feature for an unpatched vulnerability.

Field projection belongs in this server anyway -- it reads the vault straight from disk
and maintains its own frontmatter index, so no plugin round-trip is required.

## Error Codes

Common bridge errors:

- `capability_unavailable`: bridge URL not configured
- `plugin_unavailable`: Local REST API unreachable
- `rest_auth_failed`: API key rejected
- `rest_timeout`: Local REST request timed out
- `template_folder_missing`
- `template_not_found`
- `template_render_unavailable`
