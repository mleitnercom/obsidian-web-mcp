# Changelog

All notable changes to this fork will be documented in this file.
This project follows semantic versioning. Release dates use YYYY-MM-DD.

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
