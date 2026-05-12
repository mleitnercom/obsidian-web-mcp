# Changelog

All notable changes to this fork will be documented in this file.
This project follows semantic versioning. Release dates use YYYY-MM-DD.

## [Unreleased]

### Features
- Add `vault_request_upload_url` plus signed `POST /upload/{upload_id}` direct uploads for agent-local binary files, avoiding MCP argument-size limits for PDFs and other attachments.

### Reliability / Operator UX
- Log direct upload completions and rejections, and steer tool descriptions away from legacy chunked upload flows for practical agent file transfer.

### Docs / Tests
- Document direct upload configuration (`VAULT_UPLOAD_URL_SECRET`, TTL limits) and add regression tests for signed upload success and bad-signature rejection.

## [v0.6.3] - 2026-05-07

### Features
- Extend `vault_search_frontmatter` with comparison operators (`lt`, `lte`, `gt`, `gte`), scalar membership (`in`), list membership (`list_contains`, `list_any`, `list_all`), and optional multi-filter AND queries for task-oriented frontmatter workflows.
- Make `vault_upload_init` return a tool-call-friendly default `part_size` plus `total_parts`, while still allowing callers to request an explicit chunk size.
- Add an optional OCR fallback for image-only PDFs in `vault_read` via `VAULT_PDF_OCR_ENABLED` and `VAULT_PDF_OCR_CMD`, keeping the default deployment lightweight unless an operator explicitly wires in an external OCR command.

### Reliability / Operator UX
- Verify text writes by reading files back after atomic write operations so `vault_write`, `vault_patch`, `vault_append`, replace flows, and frontmatter batch updates fail loudly instead of silently succeeding on truncated output.
- Surface `size_before` and `size_delta` on `vault_patch` results so unexpected shrinkage is easier to spot during targeted edits.
- Count non-Markdown vault files as valid wikilink targets in analytics so links such as `[[exports/report.pdf]]` no longer inflate broken-link findings.

### Docs / Tests
- Expand regression coverage for frontmatter search operators, multi-filter task queries, text-write verification failures, chunked-upload init sizing, non-Markdown wikilink targets, and OCR fallback behavior.
- Document the optional external OCR fallback and its configuration in the README.

## [v0.6.2] - 2026-05-04

This release smooths out practical binary-ingestion workflows, hardens the public health surface, and adds the first useful operator visibility into which MCP tools clients actually use.

### Features
- Add `vault_import_file` so mounted or local files can be copied into the vault without base64-wrapping them into the tool call first.
- Allow operators to extend the binary media-type allowlist via `VAULT_EXTRA_BINARY_MEDIA_TYPES_JSON` instead of forcing every practical file type into the default set.
- Add `vault-observe tool-usage` for a compact journal-based summary of MCP tool usage by client family and user agent.

### Reliability / Operator UX
- Cross-reference `vault_write_binary` and `vault_upload_*` in the tool descriptions so LLM clients discover the single-call vs. resumable path more reliably.
- Restrict detailed `/health` output to direct local callers by default, with `VAULT_HEALTH_ALLOW_REMOTE_DETAILS=true` as an explicit opt-in for remote detail.
- Carry best-effort request metadata (`client_family`, `client_ip`, `user_agent`, `mcp_protocol_version`) into tool-start logs so real Claude/ChatGPT usage patterns can be observed more clearly.
- Improve the observability CLI so it can reconstruct and parse the wrapped Rich/journalctl tool-start log format seen in production.

### Docs / Tests
- Document additive binary media-type config and local file imports in the README.
- Document the new observability CLI in the operations runbook.
- Add regression coverage for extra binary media types, allowlisted local file imports, health-detail gating, request-metadata binding, and wrapped-log observability parsing.

## [v0.6.1] - 2026-04-26

This release hardens the fork for more realistic live operation: safer large-file ingestion, direct URL-based imports, and a real subtree-boundary model so operators can expose only the parts of a vault they actually want MCP clients to see.

### Features
- Add a resumable binary upload flow with `vault_upload_init`, `vault_upload_part`, `vault_upload_status`, `vault_upload_commit`, and `vault_upload_abort` so larger LLM-generated files can recover from missing or retried chunks.
- Add `vault_import_url` so the server can import allowed binary files directly from HTTP(S) URLs instead of forcing every byte through a tool-call argument.
- Add `VAULT_INCLUDED_ROOTS` and `VAULT_EXCLUDED_PATH_PREFIXES` so operators can expose only selected vault subtrees and carve out scratch/machinery paths underneath them.

### Reliability / Operator UX
- Block `vault_reindex(full=false)` through the live MCP tool by default, not just full rebuilds, because incremental MCP-triggered refreshes still fan out into long-running vector-index rebuilds in practice.
- Add `VAULT_SEMANTIC_ALLOW_MCP_REINDEX` as the explicit opt-in switch for any MCP-triggered semantic reindexing, while keeping full rebuilds behind the existing `VAULT_SEMANTIC_ALLOW_MCP_FULL_REINDEX` gate.
- Update the README and operations runbook so live operators are pointed to `vault-semantic reindex --mode incremental/full` and the nightly job instead of the MCP reindex tool.
- Protect URL imports against private/local address SSRF by default, with `VAULT_IMPORT_URL_ALLOW_PRIVATE=true` as an explicit trusted-deployment opt-in.
- Enforce vault subtree policy consistently across path resolution, listing, search, analytics, frontmatter indexing, and semantic indexing so allowlists act as a real no-leak boundary.

### Docs / Tests
- Document subtree allowlisting and excluded prefixes in the README with operator examples.
- Add focused regression coverage for allowlisted-root and excluded-prefix behavior.
## [v0.6.0] - 2026-04-19

### Features
- Add `vault_batch_replace` for exact-string replacements across multiple files in one request.
- Add `vault_patch` and `vault_append` for lighter-weight targeted editing without requiring a full file rewrite.
- Add an optional post-write hook via `VAULT_MCP_POST_WRITE_CMD` for local follow-up automation after vault mutations.

### Reliability / Operator UX
- Preserve YAML frontmatter formatting more faithfully during `vault_write(merge_frontmatter=true)` and `vault_batch_frontmatter_update` by round-tripping through `ruamel.yaml`.
- Expand broken-link analytics with explicit ambiguous classifications and line/column metadata for findings.
- Surface post-write-hook enablement in `/health`.
- Preserve the OAuth `resource` parameter across login and approval redirects so stricter MCP clients keep their resource indicator intact during interactive authorize flows.
- Improve ChatGPT connector refresh compatibility by returning explicit root-probe content types, allowing `POST /` to reach the MCP transport, and normalizing wildcard or missing `Accept` headers on refresh-style MCP POSTs.
- Expand operational guidance for distinguishing root-probe/refresh failures from real `/mcp` transport failures during connector troubleshooting.

### Docs / Tests
- Document the post-write hook, format-stable frontmatter updates, and the new editing tools in the README and operations runbook.
- Add regression coverage for YAML formatting preservation, batch replace, patch/append, ambiguous wikilinks, and the hook execution model.
- Clarify in the README that the post-write hook is a trusted-operator feature for single-user or otherwise tightly controlled deployments.
- Document ChatGPT-specific refresh quirks and troubleshooting patterns in the README and operations runbook.

### Implementation Notes
- The format-stable frontmatter round-trip and lighter-weight edit primitives continue in the same direction as work consolidated by `jjsmackay`.
- The optional post-write hook was informed by `cruciblemining`, with this fork keeping the execution model intentionally stricter by avoiding shell invocation.

## [v0.5.2] - 2026-04-17

### Features
- Add direct PDF text extraction to `vault_read` and `vault_batch_read` via `pypdf`, including basic PDF metadata in the response.

### Reliability / Operator UX
- Keep other known binary formats on the clear rejection path so binary-read failures are differentiated from PDF support.
- Surface restart-relevant OAuth state more clearly through the health payload and startup logging so reconnect problems after service restarts are easier to diagnose.
- Document PDF-read behavior in the README and operations runbook.

## [v0.5.1] - 2026-04-17

Small follow-up release focused on making analytics output more actionable and string replacement more useful for real vault-normalization work.

### Features
- Extend `vault_str_replace` with optional `replace_all=true` so file-local normalization passes no longer require repeated single-hit calls.

### Analytics
- Fix wikilink analysis so source-relative links like `[[../target-note]]` are resolved against the note's own folder instead of always against the vault root.
- Classify broken wikilink findings into more useful buckets, including `repairable_path_mismatch` and `missing_target`.
- Expand `vault_analytics_summary` with a broken-link breakdown (`broken_wikilinks_repairable`, `broken_wikilinks_missing_target`) while keeping the overall count.

### Tests / Documentation
- Add regression coverage for `replace_all`, source-relative wikilinks, and repairable-vs-missing broken-link classification.
- Refresh the README release reference and tool descriptions for the new replace and analytics behavior.

## [v0.5.0] - 2026-04-16

This release turns the current fork backlog into a practical operator-focused package: better write primitives, better health visibility, better vault hygiene workflows, and a first read-only analytics layer.

### Features
- Add `vault_write_binary` for writing allowed binary files such as PNG, JPEG, WebP, GIF, SVG, and PDF from base64 input with overwrite protection and size limits.
- Add `vault_str_replace` for exact unique-string replacement without requiring a full file rewrite in the request.
- Add `vault_analytics_summary` for compact read-only vault hygiene summaries.
- Add `vault_analytics_findings` for detailed findings by category, including broken wikilinks, missing frontmatter, suspicious tag variants, and encoding issues.

### Operations
- Add a real `/health` endpoint that reports vault reachability, frontmatter-index state, semantic-engine status, heartbeat state, and uptime.
- Add optional push-style heartbeats via `VAULT_MCP_HEARTBEAT_URL` and `VAULT_MCP_HEARTBEAT_INTERVAL`.
- Expand `vault-semantic doctor` with JSON report writing and explicit UTF-8 repair flows (`--repair-utf8`, `--repair-encoding`, `--dry-run`).
- Add a dedicated operations runbook covering health/heartbeat, UTF-8 repair flow, reindex discipline, and analytics usage.

### Reliability and Safety
- Add atomic binary writes via a dedicated byte-write path instead of routing binary content through text-only writes.
- Keep binary writes on an allowlist of supported media types and extensions, guarded by a configurable decoded size limit.
- Make string replacement intentionally strict: replacement only succeeds when the target text occurs exactly once.
- Add tests for binary writes, exact string replacement, analytics summaries/findings, heartbeat-aware health payloads, and UTF-8 repair behavior.

### Documentation
- Refresh README for the new release, tool list, heartbeat configuration, analytics capabilities, and UTF-8 operator workflow.

## [v0.4.1] - 2026-04-15

Small maintenance and security release focused on safer OAuth persistence, better vault hygiene diagnostics, and one practical filesystem tool.

### Security
- Stop persisting dynamic OAuth client secrets in clear text on disk; store hashes instead.
- Keep backward-compatible loading so existing persisted client registrations continue to work and are migrated on read.

### Maintenance
- Add `vault_delete_directory` for empty-directory cleanup via `.trash/`, guarded by `confirm=true`.
- Add `vault-semantic doctor --scan-utf8` to report markdown files that are not valid UTF-8 and can break semantic indexing.
- Make UTF-8 doctor reporting resilient even when semantic search itself is disabled or not initialized.

### Documentation
- Update README and Linux deployment docs for the new directory-delete tool, UTF-8 scan workflow, and hashed-at-rest OAuth registration storage.
- Refresh the README tool count and current release reference.

## [v0.4.0] - 2026-04-12

This release turns the semantic-search work from an internal feature set into something that is easier to operate, observe, and keep healthy over time.

### Semantic Search
- Make semantic retrieval explicitly selectable via `vault_semantic_search(search_mode=hybrid|semantic|keyword)`.
- Keep `hybrid` as the default, while allowing direct comparison against pure semantic or pure keyword ranking.
- Add `vault-semantic-benchmark` for timing and result comparisons across query modes.

### Operations
- Add `vault-semantic` for direct operator workflows: `status`, `search`, `doctor`, and manual `reindex`.
- Add clearer progress logging for semantic cache load, full rebuilds, incremental rebuilds, and embedding batches.
- Add optional systemd templates for a nightly semantic full rebuild as a maintenance safety net.
- Deploy and verify the nightly timer in the Linux production setup.

### Documentation
- Update README and Linux deployment docs to document semantic operator tooling, explicit search modes, timer setup, and live monitoring commands.
- Clarify README client-connection guidance by separating client setup from deployment and adding practical ChatGPT connector notes.
- Record the active production timer setup and operational commands in the local deployment notes.

## [v0.3.0] - 2026-04-12

### Features
- Add optional semantic search with a persistent FAISS index and hybrid semantic+keyword scoring.
- Add `vault_tree` for compact nested vault structure discovery.
- Add semantic reindex tooling with full and incremental modes.
- Add configurable embedding backend selection via `VAULT_SEMANTIC_EMBED_BACKEND` (`auto`, `sentence`, `fastembed`).

### Security
- Require explicit consent after login in OAuth authorize flow before issuing auth codes.
- Tighten OAuth session cookie policy with `SameSite=Strict`.
- Restrict trusted forwarded headers via `VAULT_TRUSTED_PROXY_IPS` instead of trusting all proxies.
- Ignore symlinked files/directories in list/search/index paths to reduce indirect traversal risk.

### Reliability
- Add debounced frontmatter-change hooks to trigger incremental semantic index updates.
- Persist semantic manifest/path metadata and improve incremental update detection.

### Docs / Tests
- Update README for semantic backend options, security model, and proxy trust configuration.
- Expand tests for semantic tooling, OAuth consent flow, config validation, and symlink handling.

## [v0.2.0] - 2026-04-12

### Security
- Harden OAuth: validate `client_id` and `redirect_uri` on `/oauth/authorize`, and verify `client_id`/`client_secret` during code exchange.
- Stop leaking the shared OAuth client secret from `/oauth/register` (per-client secret is issued instead).
- Use constant-time bearer token comparison to mitigate timing attacks.

### Reliability
- Prevent frontmatter index leaks in `stateless_http` mode by making index startup idempotent and decoupling it from request lifecycle.

### Compatibility
- Fix YAML date/datetime serialization across read/search tools and harden Windows search behavior.
- Add an optional login gate for `/oauth/authorize` (auto-approve remains default for Claude/Cowork).

### Docs / CI
- Document the optional OAuth login gate and the in-memory OAuth state design.
- Add a pytest GitHub Actions workflow.
