# Operations Runbook

This is the practical operator path for the current production-oriented fork.

## Health and Heartbeat

- `GET /health` returns a compact JSON status snapshot without bearer auth.
- It includes vault reachability, frontmatter-index state, semantic-engine state, heartbeat state, and uptime.
- If you want push-style monitoring, set:

```ini
VAULT_MCP_HEARTBEAT_URL=https://healthchecks.example/ping/...
VAULT_MCP_HEARTBEAT_INTERVAL=60
```

- This is intentionally simple: the server emits periodic HTTP GET pings and also reports the last heartbeat attempt/success in `/health`.

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

- Keep `VAULT_SEMANTIC_ALLOW_MCP_FULL_REINDEX=false` in normal live operation.
- Prefer `vault-semantic reindex --mode full` manually or via a nightly timer.
- Leave `VAULT_SEMANTIC_AUTO_REINDEX=0` on stability-sensitive systems unless you explicitly want watcher-driven semantic refreshes.

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

## Release Baseline

The current operator baseline assumes:

- atomic text writes
- atomic binary writes with allowlist and size limit
- exact-string replace for micro-edits
- `/health` plus optional push heartbeat
- UTF-8 doctor scan/report/repair flow
- read-only vault analytics summary/findings
