# Operations Runbook

This is the practical operator path for the current production-oriented fork.

## Health and Heartbeat

- `GET /health` returns a compact JSON status snapshot without bearer auth.
- It includes vault reachability, frontmatter-index state, semantic-engine state, heartbeat state, post-write-hook state, and uptime.
- Open it directly in a browser if you just want a quick operator check.
- If you want push-style monitoring, set:

```ini
VAULT_MCP_HEARTBEAT_URL=https://healthchecks.example/ping/...
VAULT_MCP_HEARTBEAT_INTERVAL=60
```

- This is intentionally simple: the server emits periodic HTTP GET pings and also reports the last heartbeat attempt/success in `/health`.

## Post-Write Hook

If you want fire-and-forget follow-up automation after vault mutations, set:

```ini
VAULT_MCP_POST_WRITE_CMD=/usr/local/bin/obsidian-post-write
VAULT_MCP_POST_WRITE_TIMEOUT=30
```

Runtime behavior:

- the command runs locally on the vault host
- it is executed without a shell
- it receives `MCP_OPERATION`, `MCP_PATHS`, and `MCP_PATHS_JSON`
- it is best-effort only; failures are logged but do not fail the user request

Use cases:

- git add/commit automation
- backup triggers
- audit or webhook forwarding through a local wrapper script

## UTF-8 Hygiene

Use the semantic maintenance CLI as the operator workflow.

Scan only:

```bash
vault-semantic doctor --scan-utf8
```

Scan and persist a report:

```bash
vault-semantic doctor --scan-utf8 --report-path ./reports/utf8-doctor.json
```

Dry-run repair using a legacy source encoding:

```bash
vault-semantic doctor --repair-utf8 --repair-encoding cp1252 --dry-run
```

Real repair:

```bash
vault-semantic doctor --repair-utf8 --repair-encoding cp1252
```

Recommended operator order:

1. run scan
2. write a report
3. dry-run repair
4. only then perform the real repair

## Reindex Discipline

- Keep `VAULT_SEMANTIC_ALLOW_MCP_REINDEX=false` in normal live operation.
- Keep `VAULT_SEMANTIC_ALLOW_MCP_FULL_REINDEX=false` unless you intentionally also re-enable MCP reindexing.
- Prefer `vault-semantic reindex --mode full` manually or via a nightly timer.
- If you need an ad-hoc live refresh, prefer `vault-semantic reindex --mode incremental` over the MCP tool.
- Leave `VAULT_SEMANTIC_AUTO_REINDEX=0` on stability-sensitive systems unless you explicitly want watcher-driven semantic refreshes.

## OAuth Client Registrations

- Dynamic OAuth client registrations are persisted by default.
- `VAULT_REGISTERED_CLIENT_TTL_SECONDS=0` disables automatic expiry and is the recommended single-user setting for stable ChatGPT and Claude reconnects.
- Keep `VAULT_MAX_REGISTERED_CLIENTS` as the safety cap so very old registrations can still be trimmed if the store ever grows unexpectedly.

Restart-safe checklist:

1. Keep `VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS=true`.
2. Keep `VAULT_REGISTERED_CLIENT_TTL_SECONDS=0`.
3. Set `VAULT_PUBLIC_BASE_URL` explicitly when you run behind Cloudflare Tunnel or another reverse proxy.
4. Keep `VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH` on persistent disk, not in a temp directory.
5. After a restart, open `GET /health` and confirm the `oauth` block reports:
   - `registered_client_persistence_enabled=true`
   - `registered_client_store_exists=true`
   - `restart_stable_reconnects=true`

If those values are correct and the connector still asks for reauthentication after a restart, the remaining problem is likely client-side reconnect behavior rather than missing server-side persistence.

Legacy note:

- If a ChatGPT connector was created before `VAULT_REGISTERED_CLIENT_TTL_SECONDS=0` became the default, one final delete/reconnect cycle may still be required.
- In that case, ChatGPT may still hold an older dynamic `client_id` locally while the server-side registration has already expired from the persisted store.

## ChatGPT Refresh Quirks

ChatGPT currently behaves a little differently from Claude when it refreshes actions:

- it may probe both `/` and `/mcp`
- it may use an SSE-style root probe on `GET /`
- it may send permissive refresh headers such as `Accept: */*`
- it may try action refresh even when the actual `/mcp` tool path is healthy

This fork now compensates for those refresh quirks by:

- returning explicit content types on `GET /`
- allowing `POST /` to reach the same MCP transport as `POST /mcp`
- normalizing wildcard or missing `Accept` headers on refresh-style MCP POSTs

Typical symptom patterns:

- `MCP SSE probe returned an unsupported content type`
- `Child exited without calling task_status.started()`
- "Noch keine App-Aktionen verfuegbar" even though Claude still works

Operator checklist when ChatGPT action refresh fails:

1. open `GET /health`
2. confirm the `oauth` block still shows persisted registrations and restart-stable reconnects
3. inspect `journalctl -u obsidian-mcp` for `GET /`, `POST /`, `GET /.well-known/...`, and `POST /mcp`
4. distinguish root-probe/refresh failures from real `/mcp` failures

If Claude still works while ChatGPT action refresh fails, that usually points to a ChatGPT-specific probe or refresh-compatibility issue rather than a total MCP outage.

## Vault Analytics

Use analytics for read-only hygiene checks, not as an auto-fix path.

Quick summary:

```text
vault_analytics_summary(path_prefix?, required_frontmatter?, max_examples?)
```

Detailed findings:

```text
vault_analytics_findings(category, path_prefix?, required_frontmatter?, max_results?)
```

Current categories:

- `frontmatter_missing`
- `required_frontmatter_missing`
- `broken_wikilinks`
- `suspicious_tag_variants`
- `encoding_issues`

Broken-link findings now separate:

- `repairable_path_mismatch`
- `missing_target`
- `ambiguous_basename`
- `ambiguous_path_mismatch`

## PDF Reads

- `vault_read` and `vault_batch_read` now extract text from `.pdf` files via `pypdf`.
- Other known binary formats remain intentionally blocked with a clear error.
- If a PDF is image-only or otherwise has no extractable text layer, the read call may return empty content while still reporting PDF metadata such as page count.

## Release Baseline

The current operator baseline assumes:

- atomic text writes
- atomic binary writes with allowlist and size limit
- exact-string replace for micro-edits
- `/health` plus optional push heartbeat
- UTF-8 doctor scan/report/repair flow
- read-only vault analytics summary/findings
- format-stable frontmatter merge/update flow
- optional post-write hook for local follow-up automation
