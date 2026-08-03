# Configuration

Back to [README](../README.md).

All configuration is read from environment variables at process startup.

## Core

| Variable | Default | Description |
|---|---|---|
| `VAULT_PATH` | `~/Obsidian/MyVault` | Vault root directory |
| `VAULT_MCP_TOKEN` | empty | Bearer token for MCP requests |
| `VAULT_MCP_PORT` | `8420` | HTTP listen port |
| `VAULT_MCP_HOST` | `0.0.0.0` | Bind host (default `0.0.0.0`; set to `127.0.0.1` when a proxy/tunnel runs on the same host) |
| `VAULT_MCP_ALLOWED_HOSTS` | (extra hosts only) | Extra hostnames for DNS-rebinding protection, **appended** to the always-present loopback defaults (`127.0.0.1:*`, `localhost:*`, `[::1]:*`); set only your tunnel/proxy host. Deprecated alias: `VAULT_ALLOWED_HOSTS` |
| `VAULT_MCP_PUBLIC_URL` | empty | External HTTPS base URL for OAuth metadata and signed uploads (deprecated alias: `VAULT_PUBLIC_BASE_URL`) |
| `VAULT_MCP_FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Proxy IPs trusted for forwarded headers (deprecated alias: `VAULT_TRUSTED_PROXY_IPS`) |

## OAuth

| Variable | Default | Description |
|---|---|---|
| `VAULT_OAUTH_CLIENT_ID` | `vault-mcp-client` | Default OAuth client id |
| `VAULT_OAUTH_CLIENT_SECRET` | empty | OAuth client secret |
| `VAULT_OAUTH_AUTH_USERNAME` | empty | Optional authorize-page username |
| `VAULT_OAUTH_AUTH_PASSWORD` | empty | Optional authorize-page password |
| `VAULT_OAUTH_SESSION_SECRET` | empty | Cookie/session signing secret |
| `VAULT_OAUTH_REQUIRE_APPROVAL` | `true` | Require approval click after login |
| `VAULT_OAUTH_ALLOW_NO_AUTH` | `false` | Insecure opt-in: allow unauthenticated auto-approval at `/oauth/authorize` when no login credentials are set. Leave unset in production. |
| `VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS` | `true` | Persist dynamic client registrations |
| `VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH` | semantic cache path + `oauth_registered_clients.json` | Registered-client store |
| `VAULT_REGISTERED_CLIENT_TTL_SECONDS` | `0` | Dynamic client TTL; `0` disables expiry |
| `VAULT_MAX_REGISTERED_CLIENTS` | `128` | Maximum retained dynamic clients |

## Vault Scope

| Variable | Default | Description |
|---|---|---|
| `VAULT_INCLUDED_ROOTS` | `.` | Comma-separated allowlist of visible vault subtrees |
| `VAULT_EXCLUDED_PATH_PREFIXES` | empty | Comma-separated relative prefixes excluded from access |

## Writes, Imports, and Uploads

| Variable | Default | Description |
|---|---|---|
| `VAULT_MAX_CONTENT_SIZE` | `1000000` | Max text content bytes |
| `VAULT_MAX_BINARY_SIZE` | `10485760` | Max binary bytes |
| `VAULT_EXTRA_BINARY_MEDIA_TYPES_JSON` | empty | JSON map of additional MIME types to extensions |
| `VAULT_IMPORT_FILE_ALLOWED_ROOTS` | empty | Local filesystem roots allowed for `vault_import_file` |
| `VAULT_IMPORT_URL_TIMEOUT_SECONDS` | `30` | URL import timeout |
| `VAULT_IMPORT_URL_ALLOW_PRIVATE` | `false` | Allow URL imports from private/local addresses |
| `VAULT_UPLOAD_URL_SECRET` | empty | Signing secret for direct upload URLs |
| `VAULT_UPLOAD_URL_TTL_SECONDS` | `900` | Default signed upload URL TTL |
| `VAULT_UPLOAD_URL_MAX_TTL_SECONDS` | `3600` | Max signed upload URL TTL |

## Daily Notes

| Variable | Default | Description |
|---|---|---|
| `VAULT_DAILY_NOTES_FOLDER` | empty | Daily-note folder inside vault |
| `VAULT_DAILY_NOTES_FORMAT` | `%Y-%m-%d` | `strftime` date format |
| `VAULT_DAILY_NOTES_TEMPLATE` | empty | Template prepended to newly created daily notes |

## Audit and Hooks

| Variable | Default | Description |
|---|---|---|
| `VAULT_AUDIT_LOG_PATH` | empty | Enables JSONL audit logging when set |
| `VAULT_AUDIT_LOG_INCLUDE_READS` | `false` | Include read/search/list operations in audit |
| `VAULT_MCP_POST_WRITE_CMD` | empty | Optional post-write command |
| `VAULT_MCP_POST_WRITE_TIMEOUT` | `30` | Hook timeout in seconds |

## Health and Heartbeat

| Variable | Default | Description |
|---|---|---|
| `VAULT_MCP_HEARTBEAT_URL` | empty | Optional push-heartbeat URL |
| `VAULT_MCP_HEARTBEAT_INTERVAL` | `60` | Heartbeat interval seconds |
| `VAULT_HEALTH_ALLOW_REMOTE_DETAILS` | `false` | Expose detailed health to non-loopback callers |

## PDF and OCR

| Variable | Default | Description |
|---|---|---|
| `VAULT_PDF_OCR_ENABLED` | `false` | Enable external OCR fallback for image-only PDFs |
| `VAULT_PDF_OCR_CMD` | empty | External command that prints OCR text to stdout |
| `VAULT_PDF_OCR_TIMEOUT` | `120` | OCR timeout seconds |
| `VAULT_PDF_OCR_LANGUAGES` | `deu+eng` | OCR language hint exposed to the command |
| `VAULT_PDF_OCR_SIDECAR_ENABLED` | same as `VAULT_PDF_OCR_ENABLED` | Cache OCR output as sidecar text files |
| `VAULT_PDF_OCR_SIDECAR_SUFFIX` | `.ocr.txt` | Sidecar suffix |

## Obsidian Local REST API

| Variable | Default | Description |
|---|---|---|
| `VAULT_OBSIDIAN_REST_URL` | empty | Local REST API base URL |
| `VAULT_OBSIDIAN_REST_API_KEY` | empty | Local REST API key |
| `VAULT_OBSIDIAN_REST_VERIFY_TLS` | `false` | Verify Local REST TLS certificates |
| `VAULT_OBSIDIAN_REST_TIMEOUT` | `15` | Local REST timeout seconds |
| `VAULT_TEMPLATER_FOLDER` | empty | Template folder inside vault |

## Semantic Search

| Variable | Default | Description |
|---|---|---|
| `VAULT_SEMANTIC_SEARCH_ENABLED` | `false` | Enable semantic-search tooling |
| `VAULT_SEMANTIC_EMBED_BACKEND` | `fastembed` | Embedding backend: `auto`, `sentence`, or `fastembed` |
| `VAULT_SEMANTIC_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model |
| `VAULT_SEMANTIC_CACHE_PATH` | `VAULT_PATH/.obsidian-vault-mcp` | Semantic cache directory |
| `VAULT_SEMANTIC_AUTO_REINDEX` | `false` | Automatically reindex changed files |
| `VAULT_SEMANTIC_BUILD_ON_DEMAND` | `false` | Build index on first search |
| `VAULT_SEMANTIC_ALLOW_MCP_REINDEX` | `false` | Allow MCP-triggered incremental reindex |
| `VAULT_SEMANTIC_ALLOW_MCP_FULL_REINDEX` | `false` | Allow MCP-triggered full reindex |
| `VAULT_SEMANTIC_CHUNK_SIZE` | `900` | Chunk size |
| `VAULT_SEMANTIC_CHUNK_OVERLAP` | `150` | Chunk overlap |
| `VAULT_SEMANTIC_EMBED_BATCH_SIZE` | `64` | Embedding batch size |
| `VAULT_SEMANTIC_MAX_RESULTS` | `20` | Max semantic results |
| `VAULT_SEMANTIC_UPDATE_DEBOUNCE_SECONDS` | `4` | Reindex debounce seconds |

## Tool Limits and Rate Limits

| Variable | Default | Description |
|---|---|---|
| `VAULT_MAX_BATCH_SIZE` | `20` | Max batch items |
| `VAULT_MAX_SEARCH_RESULTS` | `50` | Hard max search results |
| `VAULT_MAX_FRONTMATTER_SEARCH_RESULTS` | `500` | Hard max frontmatter search results |
| `VAULT_MAX_FRONTMATTER_RESPONSE_BYTES` | `200000` | Frontmatter response byte budget before pagination truncation |
| `VAULT_DEFAULT_SEARCH_RESULTS` | `20` | Default search results |
| `VAULT_MAX_LIST_DEPTH` | `5` | Max listing depth |
| `VAULT_MAX_TREE_DEPTH` | `10` | Max tree depth |
| `VAULT_CONTEXT_LINES` | `2` | Search context lines |
| `VAULT_RATE_LIMIT_READ` | `100` | Read calls per minute per token |
| `VAULT_RATE_LIMIT_WRITE` | `30` | Write calls per minute per token |
| `VAULT_RATE_LIMIT_OAUTH_AUTHORIZE` | `30` | OAuth authorize rate limit |
| `VAULT_RATE_LIMIT_OAUTH_TOKEN` | `30` | OAuth token rate limit |
| `VAULT_RATE_LIMIT_OAUTH_REGISTER` | `10` | OAuth registration rate limit |
