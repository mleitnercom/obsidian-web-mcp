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
