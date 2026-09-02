# Security

Back to [README](../README.md).

## Threat Model

The server exposes read/write access to an Obsidian vault. Treat it as a sensitive service: if a client can authenticate and call write tools, it can modify vault content.

Primary concerns:

- Unauthorized remote access
- Path traversal outside the vault
- Symlink escape
- Accidental destructive writes
- Partial writes propagated by file sync
- Large binary payloads or imports exhausting resources
- Audit records leaking secrets

## Path Policy

All vault paths resolve under `VAULT_PATH`. The resolver rejects:

- `..` traversal outside the vault
- null bytes
- dot-prefixed path components such as `.obsidian`
- symlink escape
- configured excluded roots
- paths outside `VAULT_INCLUDED_ROOTS`

Use:

```env
VAULT_INCLUDED_ROOTS=notes,projects
VAULT_EXCLUDED_PATH_PREFIXES=private,secrets
```

## Atomic Writes

Text and binary writes go through temp-file-plus-rename patterns. Text writes are read back and verified where applicable. This protects Obsidian Sync and similar file-sync systems from seeing partial content as a successful write.

## Soft Delete

`vault_delete` and `vault_delete_directory` move targets into `.trash/` and require `confirm=true`. They do not hard-delete files.

## Binary Limits

Binary operations enforce media type and size checks:

- `VAULT_MAX_BINARY_SIZE`
- `VAULT_EXTRA_BINARY_MEDIA_TYPES_JSON`
- `VAULT_IMPORT_FILE_ALLOWED_ROOTS`
- `VAULT_IMPORT_URL_ALLOW_PRIVATE`
- signed upload TTLs

`vault_request_upload_url` is single-use and signed. Use it for real agent/local files.

### Signed download URLs

`vault_request_download_url` is the read counterpart and follows the same trust model: the signed URL *is* the credential, so `/download/` is exempt from bearer auth exactly as `/upload/` is. That exemption covers authentication, not authorization:

- The URL is issued only by an authenticated MCP tool call, and the caller must already be allowed to read that path.
- The HMAC covers download id, path, mime type, size, digest and expiry, so none of them can be altered after issuing. An extended `expires` fails with `403`.
- Redemption re-runs the vault path policy against the current configuration instead of trusting the signed record. A path that became excluded after issuing answers `404`, not the file.
- The URL serves exactly one `GET` and dies after `VAULT_DOWNLOAD_URL_TTL_SECONDS` (default 300). `HEAD` and `Range` deliberately do not consume it, so size checks and resumed transfers stay possible.
- If the file changed since the URL was issued, the server answers `409` rather than serving bytes that do not match the promised `sha256`.
- Responses are sent with `Cache-Control: no-store`: a cached copy would be a second read that never reaches the server and never appears in the audit log.

It is format-agnostic on purpose. It does not widen what a caller may read; it changes the transport for a read they could already perform.

## OAuth and Host Validation

Set `VAULT_ALLOWED_HOSTS` and `VAULT_PUBLIC_BASE_URL` when using a tunnel or reverse proxy. Use `VAULT_OAUTH_AUTH_USERNAME` and `VAULT_OAUTH_AUTH_PASSWORD` for browser login before authorization.

`/oauth/authorize` fails closed when no login credentials are configured: it returns `503` instead of auto-approving and handing out the vault bearer token. `VAULT_OAUTH_ALLOW_NO_AUTH=true` is an explicit, insecure opt-in for local/dev only — never set it in production.

## Audit Privacy

Audit uses `token_id_hash`, not raw tokens. Do not add secrets to hook payloads. Hook stdin receives the same JSON record as the JSONL audit line and nothing extra.

## Secrets

Never commit or write these to the vault:

- `VAULT_MCP_TOKEN`
- `VAULT_OAUTH_CLIENT_SECRET`
- `VAULT_OAUTH_AUTH_PASSWORD`
- `VAULT_UPLOAD_URL_SECRET`
- `VAULT_OBSIDIAN_REST_API_KEY`
- cloud or S3 credentials
