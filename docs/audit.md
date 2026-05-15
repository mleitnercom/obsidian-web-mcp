# Audit

Back to [README](../README.md).

Set `VAULT_AUDIT_LOG_PATH` to enable append-only JSON Lines audit logging. When unset, audit logging is disabled and tool behavior is unchanged.

## Record Format

Each record is one JSON object per line:

| Field | Meaning |
|---|---|
| `timestamp` | UTC ISO 8601 timestamp |
| `token_id_hash` | SHA-256 hash of the active token identity |
| `client_id` | Client id or family when available |
| `operation` | Tool or route operation, for example `vault_write` or `POST /upload/{id}` |
| `operation_status` | `success` or `error` |
| `target_path` | Best-effort target path or list of target paths |
| `size_before` / `size_after` | File size before/after when applicable |
| `checksum_before` / `checksum_after` | SHA-256 checksums before/after when applicable |
| `request_id` | Request id when supplied by middleware/client metadata |
| `error` | Client-visible error summary for failed operations |

Read audit is opt-in via `VAULT_AUDIT_LOG_INCLUDE_READS=true`. For reads, `size_after` and `checksum_after` are null.

## Hook Coupling

When both `VAULT_AUDIT_LOG_PATH` and `VAULT_MCP_POST_WRITE_CMD` are set, the post-write hook receives the same audit record on stdin as a single JSON line. Hook failures are logged but do not roll back the originating MCP tool call.

The server itself does not forward audit data anywhere. Forwarding is an operator decision implemented outside the server.

## Health

Detailed `/health` includes:

```json
{
  "audit": {
    "enabled": true,
    "log_path": "/var/log/obsidian-mcp/audit.jsonl",
    "last_write_at": "2026-05-15T10:42:18Z",
    "write_errors_count_24h": 0,
    "bytes_written_24h": 18540,
    "includes_reads": false
  }
}
```

Counters are process-local and reset on service restart.

## Forwarding Options

### A. Local Only

Keep JSONL locally and rotate it with `logrotate`. This is the recommended default for small single-vault deployments.

```bash
jq 'select(.operation == "vault_write")' /var/log/obsidian-mcp/audit.jsonl
```

### B. S3-Compatible Cold Archive

Use this when audit history must survive VM loss or exceed local retention. Upload rotated compressed files once per day via cron or a systemd timer.

```bash
UPLOAD_TARGET=/var/log/obsidian-mcp/audit.jsonl.1.gz
DATE=$(date -u +%Y-%m-%d)
aws s3 cp "$UPLOAD_TARGET" "s3://REPLACE_WITH_BUCKET/$DATE.jsonl.gz" \
  --storage-class GLACIER_IR
```

Use write-only credentials, store them in a root-only systemd drop-in, and never store them in the vault or repository.

### C. Loki/Grafana

Use this when you already operate a log stack and need cross-system search. Promtail or Vector can tail the JSONL file.

### D. Commercial Loggers

Usually not recommended for personal vault deployments unless an external compliance requirement already exists.

## Trigger Events for External Forwarding

- Audit trail must be retained longer than local rotation.
- VM loss must not destroy audit history.
- Multiple MCP servers or vaults need one searchable audit view.
- External audit or compliance requirements require immutable retention.
