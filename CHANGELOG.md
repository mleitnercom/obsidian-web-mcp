# Changelog

All notable changes to this fork will be documented in this file.
This project follows semantic versioning. Release dates use YYYY-MM-DD.

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
