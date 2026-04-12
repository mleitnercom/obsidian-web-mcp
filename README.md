# obsidian-web-mcp

A secure, remote-accessible MCP server that gives LLMs read/write access to your Obsidian vault from anywhere -- your desktop, your phone, a hotel Wi-Fi network. Unlike local-only Obsidian MCP servers, this one runs over HTTPS with real authentication, so Claude (or any MCP client) can reach your vault whether you're at your desk or not.

It reads and writes markdown files on disk, parses YAML frontmatter, serializes YAML date/datetime values safely into JSON responses, maintains an in-memory frontmatter index for fast queries, and handles full-text plus optional semantic search -- all behind OAuth 2.0 authentication and a Cloudflare Tunnel that never exposes your machine directly to the internet.

## Release

Latest: [v0.3.0](https://github.com/mleitnercom/obsidian-web-mcp/releases/tag/v0.3.0) (2026-04-12).

## Why This Exists

There are many Obsidian MCP servers. Most are local stdio servers -- they work when Claude Code is running on the same machine as your vault. That's useful, but it means:

- **Claude.ai (web) can't reach your vault.** The browser-based Claude has no way to connect to a local stdio server.
- **Claude on your phone can't reach your vault.** Same problem.
- **If you use Obsidian Sync, local MCP servers can corrupt files.** Non-atomic writes create partial files that Sync propagates to every device.

This server solves all three. It runs as a persistent HTTP service on the machine where your vault lives, tunneled securely through Cloudflare, and authenticates via OAuth 2.0 -- the same protocol Claude uses for Gmail, Google Calendar, and other integrations. The result: your vault becomes a first-class MCP connector available everywhere Claude is.

## Architecture

```
+----------+     +------------+     +-----------------+     +------------------+
| Obsidian | <-> | Filesystem | <-> | obsidian-web-mcp| <-> | Cloudflare       |
| (app)    |     | (*.md)     |     | (MCP over HTTPS)|     | Tunnel           |
+----------+     +------------+     +-----------------+     +------------------+
                                                                   |
                                                            +------+-------+
                                                            | Claude       |
                                                            | (web/desktop/|
                                                            |  mobile)     |
                                                            +--------------+
```

Your vault files never leave your machine. Cloudflare Tunnel creates an outbound-only connection from your server to Cloudflare's edge -- no inbound ports opened, no public IP exposed, no port forwarding. Claude connects to the Cloudflare edge, which relays requests through the tunnel to your server.

Obsidian and the MCP server both operate on the same directory of markdown files. The server uses atomic writes (write-to-temp-then-rename) so Obsidian Sync and the server never conflict.

## Security Model

This is a server that provides network access to your personal notes. Security is not optional.

**Authentication is enforced on every request.** The server implements OAuth 2.0 authorization code flow with PKCE for initial client authentication (what Claude uses when you connect the integration), plus bearer token validation on every subsequent MCP tool call. No request reaches a tool function without a valid token.

**OAuth authorization supports two secure single-user modes.** If you set `VAULT_OAUTH_AUTH_USERNAME` and `VAULT_OAUTH_AUTH_PASSWORD`, `/oauth/authorize` requires browser login before issuing an authorization code. You can keep an extra explicit consent click (`VAULT_OAUTH_REQUIRE_APPROVAL=true`, default) or disable it for connector compatibility (`VAULT_OAUTH_REQUIRE_APPROVAL=false`). If you leave login credentials unset, the server falls back to single-user auto-approve mode for compatibility.

**OAuth state is intentionally in-memory (non-persistent).** Authorization codes, access tokens, and temporary client registrations live in RAM and are cleared on process restart. This is a deliberate security tradeoff: it minimizes long-lived secrets on disk at the cost of requiring a reconnect after restarts. If you need persistence, consider using a reverse proxy or a private network boundary rather than storing tokens on disk.

**Your vault is never exposed directly to the internet.** The recommended deployment uses a Cloudflare Tunnel -- an outbound-only encrypted connection. Your machine opens no inbound ports. You can layer Cloudflare Access on top for additional authentication (SSO, device posture checks, IP restrictions) if you want defense in depth.

**Path traversal is blocked at the filesystem layer.** Every file operation resolves paths against the vault root directory and rejects any attempt to escape it -- `..` traversal, symlink following, null byte injection, and dotfile access (`.obsidian`, `.git`, `.trash`) are all caught before they reach the filesystem. The server will never read or write outside your vault directory.

**Reverse-proxy trust is explicit.** Forwarded headers are only trusted from IPs in `VAULT_TRUSTED_PROXY_IPS` (default `127.0.0.1,::1`) instead of trusting all upstreams.

**Writes are atomic.** Every file write goes to a temporary file first, then atomically replaces the target via `os.replace()`. This guarantees that neither Obsidian nor Obsidian Sync ever sees a partially-written file -- the operation either completes fully or doesn't happen at all.

**Safety limits prevent abuse.** By default, writes are capped at 1MB per file, batch operations at 20 files per request, search results at 50 matches, and directory recursion at 5 levels. These limits are configurable via environment variables for larger vaults or more permissive deployments. Deletions are soft -- files move to `.trash/` rather than being permanently removed, matching Obsidian's own behavior. The delete tool also requires an explicit `confirm=true` parameter as a safety gate.

**Authentication fails closed.** If the authenticated Starlette app cannot be constructed at startup, the process exits instead of falling back to an unauthenticated MCP server.

**MCP transport compatibility is preserved.** The server answers `GET /` and `HEAD /` with an MCP protocol probe response for newer clients, while keeping normal tool access behind the authenticated HTTP app.

## Tools

| Tool | Description |
|------|-------------|
| `vault_read` | Read a file, returning content, metadata, and parsed YAML frontmatter |
| `vault_batch_read` | Read multiple files in one call; handles missing files gracefully |
| `vault_write` | Write a file with optional frontmatter merging; creates parent dirs |
| `vault_batch_frontmatter_update` | Update YAML frontmatter fields on multiple files without touching body content |
| `vault_search` | Full-text search across vault files (uses ripgrep when available and falls back to Python when needed) |
| `vault_semantic_search` | Optional hybrid semantic plus keyword search backed by a persistent FAISS index (supports `path_prefix`, `filter_tags`, `min_score`) |
| `vault_search_frontmatter` | Query the in-memory frontmatter index by field value, substring, or field existence |
| `vault_list` | List directory contents with recursion depth, glob filtering, and file/dir toggles |
| `vault_tree` | Return a compact nested JSON tree of folders and files for quick orientation |
| `vault_reindex` | Rebuild semantic cache (`full=true`) or run incremental refresh (`full=false`) |
| `vault_move` | Move or rename a file or directory within the vault |
| `vault_delete` | Soft-delete a file by moving it to `.trash/` (requires explicit confirmation) |

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An Obsidian vault (any directory of markdown files)
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (only needed for remote access)
- A domain managed by Cloudflare (only needed for remote access)

## Quick Start

### Local development

```bash
# Clone and enter the project
git clone https://github.com/yourname/obsidian-web-mcp.git
cd obsidian-web-mcp

# Generate auth tokens
export VAULT_MCP_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
export VAULT_OAUTH_CLIENT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")

# Point at your vault
export VAULT_PATH="$HOME/Obsidian/MyVault"

# Run the server
uv run vault-mcp
```

If you prefer `pip` instead of `uv`:

```bash
python -m pip install -e .
vault-mcp
```

To enable optional semantic search:

```bash
python -m pip install -e .[semantic]
export VAULT_SEMANTIC_SEARCH_ENABLED=1
```

Optional (only if you want the heavier sentence-transformers backend):

```bash
python -m pip install -e .[semantic-sentence]
```

The server starts on port 8420 by default. It serves MCP over Streamable HTTP at `/mcp/`.

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VAULT_PATH` | Yes | `~/Obsidian/MyVault` | Absolute path to your Obsidian vault directory |
| `VAULT_MCP_TOKEN` | Yes | (none) | 256-bit bearer token for authenticating MCP requests |
| `VAULT_MCP_PORT` | No | `8420` | Port the HTTP server listens on |
| `VAULT_OAUTH_CLIENT_ID` | No | `vault-mcp-client` | OAuth 2.0 client ID for Claude integration |
| `VAULT_OAUTH_CLIENT_SECRET` | Yes | (none) | OAuth 2.0 client secret for Claude integration |
| `VAULT_OAUTH_AUTH_USERNAME` | No | (none) | Optional username required at `/oauth/authorize` before issuing an auth code |
| `VAULT_OAUTH_AUTH_PASSWORD` | No | (none) | Optional password required at `/oauth/authorize` before issuing an auth code |
| `VAULT_OAUTH_REQUIRE_APPROVAL` | No | `true` | Require an extra post-login consent click (`false` keeps login but skips the extra allow step) |
| `VAULT_OAUTH_SESSION_SECRET` | No | `VAULT_OAUTH_CLIENT_SECRET` | Secret used to sign the temporary browser login session cookie |
| `VAULT_TRUSTED_PROXY_IPS` | No | `127.0.0.1,::1` | Comma-separated proxy IPs trusted for forwarded headers (uvicorn `forwarded_allow_ips`) |
| `VAULT_ALLOWED_HOSTS` | No | `127.0.0.1:*,localhost:*,[::1]:*` | Comma-separated hosts allowed by DNS rebinding protection (add your tunnel hostname here) |
| `VAULT_SEMANTIC_SEARCH_ENABLED` | No | `false` | Enable optional FAISS-based semantic search |
| `VAULT_SEMANTIC_EMBED_BACKEND` | No | `fastembed` | Embedding backend selection: `auto`, `sentence`, or `fastembed` |
| `VAULT_SEMANTIC_EMBED_MODEL` | No | `BAAI/bge-small-en-v1.5` | Embedding model used by the selected semantic backend |
| `VAULT_SEMANTIC_CACHE_PATH` | No | `VAULT_PATH/.obsidian-vault-mcp` | Cache directory for FAISS index and semantic metadata |
| `VAULT_SEMANTIC_CHUNK_SIZE` | No | `900` | Target character length for semantic chunks |
| `VAULT_SEMANTIC_CHUNK_OVERLAP` | No | `150` | Character overlap between adjacent semantic chunks |
| `VAULT_SEMANTIC_EMBED_BATCH_SIZE` | No | `64` | Embedding batch size during index builds (lower values reduce RAM peaks) |
| `VAULT_SEMANTIC_MAX_RESULTS` | No | `20` | Hard upper bound for semantic search results |
| `VAULT_SEMANTIC_UPDATE_DEBOUNCE_SECONDS` | No | `4` | Debounce window for automatic incremental semantic updates |
| `VAULT_MAX_CONTENT_SIZE` | No | `1000000` | Maximum bytes allowed per write operation |
| `VAULT_MAX_BATCH_SIZE` | No | `20` | Maximum files allowed in a batch read/frontmatter update |
| `VAULT_MAX_SEARCH_RESULTS` | No | `50` | Hard upper bound for search results |
| `VAULT_DEFAULT_SEARCH_RESULTS` | No | `20` | Default search result count when the client does not specify one |
| `VAULT_MAX_LIST_DEPTH` | No | `5` | Maximum recursion depth for `vault_list` |
| `VAULT_MAX_TREE_DEPTH` | No | `10` | Maximum recursion depth for `vault_tree` |
| `VAULT_CONTEXT_LINES` | No | `2` | Default context lines returned around search hits |
| `VAULT_RATE_LIMIT_READ` | No | `100` | Per-token read requests per minute |
| `VAULT_RATE_LIMIT_WRITE` | No | `30` | Per-token write requests per minute |
| `VAULT_RATE_LIMIT_OAUTH_AUTHORIZE` | No | `30` | Per-IP `/oauth/authorize` requests per minute |
| `VAULT_RATE_LIMIT_OAUTH_TOKEN` | No | `30` | Per-IP `/oauth/token` requests per minute |
| `VAULT_RATE_LIMIT_OAUTH_REGISTER` | No | `10` | Per-IP `/oauth/register` requests per minute |
| `VAULT_REGISTERED_CLIENT_TTL_SECONDS` | No | `3600` | How long dynamic OAuth client registrations stay valid in memory |
| `VAULT_MAX_REGISTERED_CLIENTS` | No | `128` | Maximum retained dynamic OAuth client registrations in memory |

Generate tokens with: `python -c "import secrets; print(secrets.token_hex(32))"`

## Connecting to Claude

The Claude desktop and mobile apps can connect to remote MCP servers via OAuth.

1. Start the server (locally or behind a tunnel)
2. Open Claude and go to **Settings > Integrations > Add Integration**
3. Enter your server URL (e.g. `https://vault-mcp.yourdomain.com`)
4. Enter the OAuth client ID and client secret you configured
5. Claude will discover the OAuth endpoints automatically and open a browser window
6. If authorize-login credentials are configured, sign in in the browser window (and approve if `VAULT_OAUTH_REQUIRE_APPROVAL=true`); otherwise the server auto-approves the authorization
7. Claude now has access to all twelve vault tools -- on desktop and mobile

For local-only use (no tunnel), point Claude at `http://localhost:8420`.

## Remote Access with Cloudflare Tunnel

To make the server accessible from anywhere:

```bash
# Install cloudflared
brew install cloudflare/cloudflare/cloudflared

# Set your desired hostname and run the interactive setup
export VAULT_MCP_HOSTNAME="vault-mcp.yourdomain.com"
./scripts/setup-tunnel.sh
```

The script authenticates with Cloudflare, creates a tunnel, writes the config, and sets up the DNS record. You will need a domain managed by Cloudflare.

For a publicly reachable deployment, set `VAULT_OAUTH_AUTH_USERNAME` and `VAULT_OAUTH_AUTH_PASSWORD` so the browser-based OAuth step requires an explicit login before Claude receives an authorization code.

After setup, set `VAULT_ALLOWED_HOSTS` to include your tunnel hostname so DNS rebinding protection accepts requests from your domain, for example:

```bash
export VAULT_ALLOWED_HOSTS="127.0.0.1:*,localhost:*,[::1]:*,vault-mcp.yourdomain.com"
```

## Production Deployment (macOS)

For always-on operation, use launchd to run both the MCP server and the Cloudflare Tunnel as persistent background services that start at login and restart on failure.

### 1. Edit the plist templates

```bash
cp scripts/launchd/com.example.vault-mcp.plist ~/Library/LaunchAgents/
cp scripts/launchd/com.example.cloudflared-vault.plist ~/Library/LaunchAgents/
```

Open each plist and replace the placeholder tokens:
- `REPLACE_WITH_UV_PATH` -- path to `uv` binary (run `which uv`)
- `REPLACE_WITH_PROJECT_PATH` -- absolute path to this project directory
- `REPLACE_WITH_VAULT_PATH` -- absolute path to your Obsidian vault
- `REPLACE_WITH_TOKEN` -- your `VAULT_MCP_TOKEN` value
- `REPLACE_WITH_OAUTH_SECRET` -- your `VAULT_OAUTH_CLIENT_SECRET` value
- `REPLACE_WITH_HOME` -- your home directory (e.g. `/Users/yourname`)
- `REPLACE_WITH_CLOUDFLARED_PATH` -- path to `cloudflared` binary (run `which cloudflared`)

### 2. Load the services

```bash
launchctl load ~/Library/LaunchAgents/com.example.vault-mcp.plist
launchctl load ~/Library/LaunchAgents/com.example.cloudflared-vault.plist
```

Both services are configured with `RunAtLoad` (start at login) and `KeepAlive` (restart on failure). They will survive reboots.

### 3. Verify

```bash
# Check both services are running
launchctl list | grep vault

# Test the server responds
curl -s http://localhost:8420/.well-known/oauth-authorization-server

# Check logs
tail -f ~/Library/Logs/vault-mcp-error.log
```

## Deployment Examples

- Headless Linux VM on Proxmox (Obsidian + Xvfb + systemd + tunnel):
  [`docs/deploy/headless-linux-proxmox.md`](docs/deploy/headless-linux-proxmox.md)

## Obsidian Sync Compatibility

The server coexists with Obsidian Sync (or any file-based sync mechanism) without conflict. All writes use atomic file replacement (`write-to-temp-then-rename`), which means:

- Obsidian never sees a half-written file
- If Sync and the MCP server write to the same file simultaneously, the last write wins (standard filesystem semantics) but neither write is corrupted
- The frontmatter index watches for filesystem changes via `watchdog` and updates automatically when Sync brings in new files

## Semantic Search

Semantic search is optional and disabled by default. The current implementation is CPU-first and uses:

- `fastembed` for embeddings by default
- optional `sentence-transformers` backend if explicitly installed/enabled
- `faiss-cpu` for vector similarity search
- `rank-bm25` for keyword scoring

Set `VAULT_SEMANTIC_EMBED_BACKEND` to control backend choice:

- `fastembed` (default): require fastembed
- `auto`: prefer fastembed, fall back to sentence-transformers if installed
- `sentence`: require sentence-transformers

Queries are answered with a hybrid score that blends semantic similarity with keyword relevance. The semantic index is persisted on disk so normal searches stay fast after restart.

`vault_reindex(full=true)` performs a full rebuild. `vault_reindex(full=false)` performs an incremental refresh based on changed/deleted files.

When semantic search is enabled, filesystem changes are picked up via debounced callbacks from the frontmatter watcher, and the semantic index is incrementally refreshed in the background.

## Development

### Running tests

```bash
uv run pytest tests/ -v
```

If you are using `pip` instead of `uv`, run:

```bash
python -m pytest tests/ -v
```

Tests use temporary directories and never touch your real vault.

### Project structure

```
src/obsidian_vault_mcp/
    auth.py                 # Bearer token middleware (Starlette)
    config.py               # Environment variable configuration
    frontmatter_index.py    # In-memory YAML frontmatter index with filesystem watcher
    models.py               # Pydantic input validation models
    oauth.py                # OAuth 2.0 authorization code flow with PKCE
    retrieval/              # Optional FAISS-based semantic retrieval engine
    server.py               # FastMCP server setup, tool registration, entry point
    vault.py                # Core filesystem operations (path security, atomic writes)
    tools/
        manage.py           # list, move, delete tools
        read.py             # read, batch_read tools
        search.py           # full-text search, frontmatter search tools
        semantic_search.py  # optional semantic search + reindex tools
        write.py            # write, batch_frontmatter_update tools
tests/
    test_chunker.py         # Semantic chunking tests
    conftest.py             # Shared fixtures (temp vault with sample files)
    test_frontmatter.py     # Frontmatter index and query tests
    test_semantic_search.py # Semantic search tool tests
    test_tools.py           # Integration tests for tool functions
    test_vault.py           # Path resolution and file operation tests
scripts/
    setup-tunnel.sh         # Interactive Cloudflare Tunnel setup
    launchd/                # macOS launchd plist templates
```

## License

MIT -- see [LICENSE](LICENSE).
