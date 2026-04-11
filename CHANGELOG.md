# Changelog

All notable changes to this fork will be documented in this file.
This project follows semantic versioning. Release dates use YYYY-MM-DD.

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

