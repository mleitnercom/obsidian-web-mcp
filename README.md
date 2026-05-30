# obsidian-web-mcp

Production-hardened fork of [`jimprosser/obsidian-web-mcp`](https://github.com/jimprosser/obsidian-web-mcp): an HTTP-based MCP server that exposes an Obsidian vault to LLM clients such as Claude, ChatGPT, and Codex over OAuth 2.0.

**Latest release:** [v0.8.0](https://github.com/mleitnercom/obsidian-web-mcp/releases/tag/v0.8.0) (2026-05-25)

## At a Glance

### Why this fork

This fork turns the upstream "MCP over HTTP" server into a vault-aware workflow substrate. It keeps the filesystem as the source of truth, adds production guardrails for Obsidian Sync, and exposes higher-level workflows for tasks, daily notes, templates, Dataview, Canvas, audit, and binary uploads.

### What's different from upstream

- **Atomic writes with read-back verification.** Text writes fail loudly if the bytes read back do not match the intended content.
- **Synchronous frontmatter index refresh.** Edits, moves, deletes, and renames refresh the index immediately instead of waiting on eventually-consistent watcher state.
- **Extended `vault_search_frontmatter`.** Supports comparison operators, list-membership operators, and multi-field AND filters.
- **Restart-stable OAuth state.** Dynamic client registrations can persist across service restarts.
- **Vault scope policy.** `VAULT_INCLUDED_ROOTS` and `VAULT_EXCLUDED_PATH_PREFIXES` enforce a no-leak boundary across reads, writes, search, analytics, frontmatter indexing, and semantic indexing.
- **Format-stable frontmatter updates.** `ruamel.yaml` preserves quote style, key order, comments, and flow-style lists where possible.

### What's additional

- **Daily Notes tools.** `vault_daily_note_path`, `vault_daily_note_read`, and `vault_daily_note_append`.
- **Audit JSON Lines.** Mutation audit with hashed token id, client id, operation, target path, sizes, checksums, and optional read audit.
- **Optional Plugin Bridge.** Templater-style simple rendering and Dataview TABLE DQL via Obsidian Local REST API.
- **Canvas tools.** Read `.canvas` JSON and append nodes or edges with validation and write verification.
- **Direct binary upload.** `vault_request_upload_url` plus signed single-use `POST /upload/{id}` for real agent/local files.
- **PDF extraction and OCR sidecars.** `vault_read` extracts PDF text and can cache external OCR output as `*.pdf.ocr.txt`.
- **Vault analytics and hygiene.** Broken links, missing frontmatter, tag variants, and encoding issues.
- **Optional semantic search.** CPU-first `fastembed` backend with persistent FAISS cache.
- **Health endpoint.** Local detailed health includes OAuth, audit, and Plugin Bridge reachability; remote callers get minimal liveness unless explicitly opted in.

## Migration from v0.6.x

`v0.7.0` removes the legacy resumable MCP upload tools:

- `vault_upload_init`
- `vault_upload_part`
- `vault_upload_status`
- `vault_upload_commit`
- `vault_upload_abort`

Use `vault_request_upload_url` and then `POST` the file bytes to the returned signed `/upload/{id}` URL. This is the recommended path for binary files because it avoids MCP argument-size limits and base64 overhead.

## Application Scenarios

### Tasks and TODO Tracking

Treat tasks as markdown files with structured YAML frontmatter:

```yaml
---
id: 2026-05-fix-broken-link-logic
title: Fix broken link classification
status: next
priority: 3
due: 2026-05-20
project: vault-tooling
---
```

Then query them with `vault_search_frontmatter`, including AND filters and list operators.

### Daily Notes Capture

Use `vault_daily_note_append` to add structured notes to today's daily note. The path is controlled by `VAULT_DAILY_NOTES_FOLDER` and `VAULT_DAILY_NOTES_FORMAT`; new files can start with `VAULT_DAILY_NOTES_TEMPLATE`.

### Audit Trail

Set `VAULT_AUDIT_LOG_PATH` to write append-only JSONL records for mutations. `VAULT_AUDIT_LOG_INCLUDE_READS=true` optionally records reads, search, list, analytics, and Canvas reads. See [docs/audit.md](docs/audit.md).

### Template-Driven Capture

The template tools intentionally implement simple `{{token}}` substitution, not full Templater execution. Templates containing `<% %>` are rejected with `template_render_unavailable` instead of being half-rendered.

### Vault Hygiene

Use `vault_analytics_summary`, `vault_analytics_findings`, and the `vault-semantic doctor` CLI to inspect broken wikilinks, tag variants, missing frontmatter, and markdown encoding issues.

## Architecture

```text
LLM client
  | OAuth 2.0 / MCP over HTTPS
  v
obsidian-web-mcp
  | main path: atomic filesystem reads/writes
  v
Obsidian vault files

Optional:
obsidian-web-mcp <-> Obsidian Local REST API <-> Obsidian plugins
```

The filesystem path is the primary path. The Plugin Bridge is additive and optional. If Local REST API is not configured, Plugin Bridge tools return capability errors instead of stack traces.

## Capabilities

| Tool | Purpose |
|---|---|
| `vault_analytics_summary` | Compact vault hygiene summary |
| `vault_analytics_findings` | Detailed analytics findings by category |
| `vault_read` | Read text, markdown, or PDF content with metadata and frontmatter |
| `vault_batch_read` | Read multiple files in one call |
| `vault_canvas_read` | Read parsed Obsidian `.canvas` JSON |
| `vault_canvas_add_node` | Add a node to a Canvas file |
| `vault_canvas_add_edge` | Add an edge to a Canvas file |
| `vault_write` | Write text with optional frontmatter merge and verification |
| `vault_write_binary` | Write smaller base64 binary payloads |
| `vault_request_upload_url` | Create a signed direct upload URL for binary files |
| `vault_import_url` | Import an allowed binary file from HTTP(S) |
| `vault_import_file` | Import an allowed local/mounted file into the vault |
| `vault_batch_frontmatter_update` | Update frontmatter fields across files |
| `vault_batch_replace` | Replace exact strings across files |
| `vault_str_replace` | Replace exact strings in one file |
| `vault_patch` | Replace one unique exact occurrence |
| `vault_append` | Append content to a file |
| `vault_daily_note_path` | Resolve today's daily-note path |
| `vault_daily_note_read` | Read today's daily note without creating it |
| `vault_daily_note_append` | Append to today's daily note, creating it if needed |
| `vault_template_list` | List markdown templates |
| `vault_template_render` | Render a template with simple substitution |
| `vault_template_apply` | Render and write a new note |
| `vault_dataview_query` | Run Dataview TABLE DQL through Local REST API |
| `vault_search` | Full-text search with context; includes OCR sidecars by default |
| `vault_search_frontmatter` | Frontmatter search with comparison/list/AND filters |
| `vault_semantic_search` | Optional hybrid semantic and keyword search |
| `vault_list` | List directory contents; OCR sidecars hidden by default |
| `vault_tree` | Compact nested directory tree |
| `vault_reindex` | Rebuild optional semantic-search cache when enabled |
| `vault_move` | Move or rename files/directories |
| `vault_delete` | Soft-delete a file into `.trash/` |
| `vault_delete_directory` | Soft-delete an empty directory into `.trash/` |
| `recurring_materialize` | Materialize pending recurring-task instances from templates (see [docs/recurring.md](docs/recurring.md)) |

## Optional Plugin Bridge

The Plugin Bridge uses the Obsidian Local REST API plugin. Configure:

```env
VAULT_OBSIDIAN_REST_URL=https://127.0.0.1:27124
VAULT_OBSIDIAN_REST_API_KEY=REPLACE_WITH_LOCAL_REST_API_KEY
VAULT_OBSIDIAN_REST_VERIFY_TLS=false
VAULT_TEMPLATER_FOLDER=Templates
```

Constraints are deliberate:

- Templater tools use server-side simple substitution, not full Templater execution.
- `vault_dataview_query` supports DQL TABLE queries only.
- DataviewJS is not exposed.
- `TABLE WITHOUT ID` is not supported by Local REST API.

See [docs/plugin-bridge.md](docs/plugin-bridge.md).

## Configuration

All configuration is via environment variables. See [docs/configuration.md](docs/configuration.md) for the complete table.

Minimal local example:

```bash
export VAULT_PATH=/path/to/ObsidianVault
export VAULT_MCP_TOKEN=REPLACE_WITH_RANDOM_TOKEN
export VAULT_OAUTH_CLIENT_SECRET=REPLACE_WITH_RANDOM_SECRET
vault-mcp
```

## Security Model

The server blocks path traversal, dotfile access, symlink escape, null bytes, and configured excluded subtrees. Writes are atomic and verified. Deletes are soft by default. Binary writes and uploads enforce media-type and size limits. See [docs/security.md](docs/security.md) and [docs/oauth.md](docs/oauth.md).

OAuth client authentication is strictly enforced. PKCE with S256 is mandatory for every authorize request, regardless of client type. The authorization_code grant validates client_id and client_secret before any other check; PKCE alone does not satisfy client authentication for confidential clients. Startup failures crash the process rather than falling back to an unauthenticated server.

## Audit

Audit records are append-only JSON Lines when `VAULT_AUDIT_LOG_PATH` is set. A post-write hook can receive the exact same JSON record on stdin, but the server itself does not forward audit data anywhere by default. See [docs/audit.md](docs/audit.md).

## Deployment

For a sanitized headless Linux + Proxmox + Obsidian Sync deployment pattern, see [docs/deploy/headless-linux-proxmox.md](docs/deploy/headless-linux-proxmox.md).

## Development

```bash
python -m pip install -e .
python -m pytest -q
```

Optional semantic dependencies:

```bash
python -m pip install -e .[semantic]
```

### Project structure

```text
src/obsidian_vault_mcp/
    audit.py              # JSONL audit pipeline and health counters
    auth.py               # Bearer-token middleware
    config.py             # Environment configuration
    frontmatter_index.py  # In-memory frontmatter index and watcher
    frontmatter_io.py     # ruamel.yaml round-trip helpers
    hooks.py              # Post-write hook dispatcher
    models.py             # Pydantic input models
    oauth.py              # OAuth 2.0 endpoints and discovery
    obsidian_rest.py      # Local REST API client
    rate_limit.py         # Per-token in-memory rate limiting
    server.py             # FastMCP setup, routes, and tool registration
    vault.py              # Filesystem operations and path policy
    retrieval/            # Optional semantic search engine
    tools/                # Tool implementations
tests/                    # Unit and integration tests
docs/                     # Detailed guides
```

## Release Notes

See [CHANGELOG.md](CHANGELOG.md) and the [GitHub Releases](https://github.com/mleitnercom/obsidian-web-mcp/releases).

## Credits

### OAuth hardening alignment

The OAuth client-authentication tightening in this fork follows the hardening strecke established in [jjsmackay/obsidian-web-mcp](https://github.com/jjsmackay/obsidian-web-mcp) (community fork), originally contributed by [David Ronen](https://github.com/dr-growth) and integrated by [Marcelo Toledo](https://github.com/jjsmackay). This fork implements the same security guarantees on top of its own OAuth code paths (hash-based client secrets, session cookies, persistent client registrations).

## License

MIT. See [LICENSE](LICENSE).
