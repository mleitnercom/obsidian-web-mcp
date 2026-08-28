# Changelog

All notable changes to this fork will be documented in this file.
This project follows semantic versioning. Release dates use YYYY-MM-DD.

## [Unreleased]

## [v0.8.23] - 2026-08-29

### Fixed
- **`vault_search` honours `context_lines` on the ripgrep path.** The ripgrep backend passed `--context=N` to `rg` and then threw the answer away: the parser read `match` events and dropped every `context` event, so `match_context` held the bare matching line. The Python fallback meanwhile returned the full `lines[i - N : i + N + 1]` block. Same tool, same arguments, two different answers depending on whether `rg` happened to be installed on the host -- invisible in review, because each backend looks correct on its own.

  Found in production, not by reading code. Ripgrep was installed on the reference server on 2026-08-29 to fix a latency problem: without it a full-vault query falls back to a Python scan over 5,499 files and takes **8.7 s**, long enough to block the server's event loop so that `/health` stops answering and the uptime monitor reports the server down. With `rg` the same query takes **0.207 s**, a factor of 42. In the same moment, every search result silently lost its surrounding lines.

  The parser now collects the lines ripgrep emits per file first and cuts each match's window out of them afterwards. Assembling while streaming is not possible: a match's *trailing* context arrives after its `match` event. Absent neighbours at the start or end of a file are skipped rather than padded, which is what `_search_python` does. Binary hits carry `{"bytes": ...}` and no line number and are now skipped instead of raising `KeyError`. The unused `current_match` local, leftover of a context assembly that was never built, is gone.

  Pinned by `tests/test_search_context.py`, including a parity test that runs the real ripgrep and the real Python scan over the same vault and compares the full payload; it skips where `rg` is absent.

## [v0.8.22] - 2026-08-04

### Added
- **`fields` projection for `vault_search_frontmatter`.** Returns only the named frontmatter keys per hit instead of the whole block. A task file in the reference vault carries 700-900 characters of frontmatter; a briefing pass that needs `status`, `due` and `defer` previously paid for all of it on every one of ~100 hits. This is the replacement for the server-side projection that `vault_dataview_query` provided until Local REST API dropped Dataview (removed in v0.8.21) -- but it lands where the reads actually happen: `vault_search_frontmatter` was called 635 times in the 30 days before the change, `vault_dataview_query` 9 times.

  Deliberate semantics, each pinned by a test:
  - **Projection never narrows matching.** It is applied after the index query, so a filter may use a field the caller does not request back.
  - **Missing keys are omitted, not returned as `null`** -- "not set" stays distinguishable from "set to nothing".
  - **An empty list means no projection, not "drop all"** -- silently returning empty frontmatter would be indistinguishable from data loss.
  - `path` and `title` live outside the frontmatter block and always survive.

  Purely additive: omitting `fields` returns the full frontmatter exactly as before.

## [v0.8.21] - 2026-08-03

### Removed
- **`vault_dataview_query` is gone.** It ran Dataview DQL through Obsidian Local REST API using the `application/vnd.olrapi.dataview.dql+txt` content type. Local REST API **4.0 removed its Dataview dependency**, and with it that content type -- every DQL request has since answered `HTTP 400 / errorCode 40012` ("Unknown or invalid Content-Type"). Measured against the installed plugin, not inferred: `jsonlogic+json` returns `40015` (content type known, body rejected) while the DQL type returns `40012` (content type unknown), the string `vnd.olrapi.dataview.dql+txt` no longer appears anywhere in the plugin bundle, and its OpenAPI spec lists only JsonLogic for `POST /search/`.

  There is no successor: JsonLogic filters notes but returns whole `NoteJson` objects rather than projected columns, which is precisely what the tool existed to avoid. Pinning the plugin to a pre-4.0 release was rejected because the path-traversal fix (advisory `GHSA-62gx-5q78-wrvx`) only landed in 4.1.3 -- downgrading would trade a convenience feature for an unpatched vulnerability. Field projection belongs in this server, which reads the vault from disk and keeps its own frontmatter index.

  Removed with it: `VaultDataviewQueryInput`, `VAULT_DATAVIEW_TIMEOUT`, the `dataview_unavailable` / `dataview_query_failed` error codes and the four mock-based tests. `obsidian_rest.py` stays -- `/health` still reports Local REST API reachability, and the Templater bridge is unaffected.

### Fixed
- **`path_prefix` no longer produces phantom broken links in analytics.** `_load_posts` built the wikilink index from `path_prefix` instead of the whole vault, so `path_prefix` silently scoped which files were *resolvable* rather than only which were *checked*. A perfectly valid link from `15_Tasks/` to `70_Privat/` was reported as `missing_target` -- the narrower the prefix, the more false positives, which made the parameter actively misleading rather than merely limited. Against a real vault this inflated one scoped run to over 3.000 phantom findings. Checking scope is unchanged; only resolution now spans the full vault. Covered by the new `tests/test_analytics.py`, which also pins that genuinely dangling links are still reported and that the prefix still limits which files get checked.

### Note on test coverage
- Neither gap had a test. The Dataview path was covered only by mocks that asserted the server sent the content type it always sent -- they stayed green while the feature was dead for eight weeks, because nothing exercised the real endpoint. Analytics had no test file at all (only a stale `.pyc` from a deleted one). The lesson kept: a mock that asserts your own outgoing request proves nothing about the other side of the boundary.

## [v0.8.20] - 2026-08-02

### Security
- **Ripgrep argument injection closed in `vault_search` (upstream #51).** The search query was appended as the final positional argument, so a query beginning with `-` was parsed by ripgrep as an option. Notably `--pre=<cmd>` makes ripgrep execute an arbitrary program once per searched file -- remote code execution as the server user, reachable by anyone who can call `vault_search`. The pattern is now passed via `-e <query>` and the search path is shielded behind a `--` option terminator, so a leading-dash query is always treated as a literal search pattern. No API change.

### Changed
- **Frontmatter parsing hardened and now fails closed (upstream #42).** `frontmatter_io.loads` previously returned `({}, content)` on malformed or unterminated frontmatter, so a `merge_frontmatter` write could silently drop a note's existing frontmatter. It now:
  - raises `ruamel.yaml.YAMLError` on malformed YAML or an unterminated block (an opening `---` with no closing fence), letting merge callers decide instead of losing data. All in-tree callers already treat a parse failure as "leave the file as-is";
  - uses strict, line-based fence detection -- a fence is exactly `---` on its own line (trailing whitespace tolerated), so a Markdown thematic break (`----`), an indented `---`, or `--- text` is no longer mistaken for a delimiter;
  - strips a leading UTF-8 BOM before fence detection, so a BOM-prefixed note is no longer seen as frontmatter-less;
  - builds a fresh `YAML()` per call, since ruamel is not reentrant and FastMCP runs sync tools in a threadpool.

## [v0.8.19] - 2026-08-02

### Fixed
- **Broken-wikilink analytics no longer false-positives on block anchors.** `_split_wikilink_target` stripped the display alias (`|`) and section anchor (`#`) but not the block anchor (`^`), so a valid link like `[[Note^block-id]]` was looked up as a note literally named `Note^block-id`, not found, and wrongly reported under `broken_wikilinks` / `missing_target`. It now also strips `^…`; a same-note anchor-only link (`[[^block-id]]`, `[[#heading]]`) collapses to an empty target and is treated as OK. Links to genuinely missing notes are still flagged. (Section-anchor targets are still validated at note level only — a link to an existing note with a non-existent heading remains a false negative, tracked separately.)

## [v0.8.18] - 2026-08-02

### Added
- **Multi-year cadence for absolute recurring templates.** Two optional template fields let an absolute anchor fire less often than yearly: `recurrence_year_cycle` (run every N years, default `1` = yearly) and `recurrence_year_base` (a reference year the cadence lands on). A year materializes only when `(year - recurrence_year_base) % recurrence_year_cycle == 0`; off years yield no instance. `recurrence_year_cycle` > 1 without a valid `recurrence_year_base` warns and falls back to yearly rather than guess the phase. Period keys stay year-stamped, so idempotency is unchanged. Purely additive — every existing template (which omits the fields) keeps its yearly behaviour. Relative-mode logic is untouched.

## [v0.8.17] - 2026-07-31

### Added
- **Run observability for `recurring_materialize`** — so a failed run surfaces where the operator looks instead of only in the server log (materialization is often unattended, driven by the internal interval loop or a timer). Two opt-in, vault-relative outputs, both off by default:
  - **`VAULT_RECURRING_ALERT_PATH`** — a **self-clearing alert task**. Written (`status: next`, `focus_date: today`) with the failing templates when a run has errors, so it shows up in task views / the morning briefing; automatically **deleted** on the next clean run. A marker (`source: recurring-materialize`) guards the delete so a note the operator put at that path is never removed.
  - **`VAULT_RECURRING_REPORT_PATH`** — a **run-report note** (`type: recurring-run-report`) overwritten every run with the counts and any errors/warnings, as a durable last-run record for a Base / weekly view.
  Both writes are failure-isolated: an observability error never rolls back a materialization run. `dry_run` writes neither.

## [v0.8.16] - 2026-07-31

### Fixed
- **Recurring instance body now leads with the `# {title}` H1 and a blank line after the frontmatter**, per the Tasks-Schema v0.8 body convention. v0.8.15 emitted the body starting directly with `## Next Action`, missing the top-level heading and the separating blank line. Cosmetic only — no effect on Bases or the briefing. The `body_template` full-override path is unchanged.

## [v0.8.15] - 2026-07-30

### Fixed
- **Recurring instances are now schema-complete tasks, not invisible fragments.** `recurring_materialize` wrote instances without `title`, `status`, `updated` or a body, so they satisfied no Base and never surfaced in Next, WV or the morning briefing — they ran silently (14 such instances observed on prod, four overdue including a quarterly governance item). Step 5 of the algorithm now always stamps `title` (from the template's `instance_title` format string, else its `title` verbatim), `status` (default `next`, configurable via the new `VAULT_RECURRING_INSTANCE_STATUS`, overridable per-template via `frontmatter_to_inherit`) and `updated` (the generation date, never the template date). The body gets a canonical `## Next Action` (copied from the template's `## Next Action (Template)` section, or the optional `body_action` field, else the title as a placeholder plus a warning), an empty `## Verlauf`, and a `## Bezug` link to the master template. An instance that still cannot resolve a title fails closed into the tool's `errors` instead of being written. `dry_run` now returns the planned frontmatter for schema checks without writing. Existing `instance_title` / `body_template` / `frontmatter_to_inherit` behaviour is unchanged. No migration needed (only future creation is corrected).

### Added
- **`VAULT_RECURRING_INSTANCE_STATUS`** (default `next`) — the status stamped onto freshly materialized recurring instances.

## [v0.8.14] - 2026-06-18

### Security
- **`image/svg+xml` removed from the default binary allowlist.** SVG is the one entry that is not inert: it can carry `<script>`/`onload`. Because `_validate_binary_target` gates only on the declared `media_type` + extension and never sniffs bytes, allowing it was an arbitrary-active-content write into a vault that may be synced or rendered in a preview surface. This hardens all three binary entry points (`vault_write_binary`, `vault_import_url`, `vault_import_file`). PNG/JPEG/WEBP/GIF/PDF (all inert) remain; SVG can be re-enabled deliberately via `VAULT_EXTRA_BINARY_MEDIA_TYPES_JSON`. Surfaced by the upstream review of mleitnercom's `vault_write_binary` PR (jimprosser/obsidian-web-mcp#61).

### Fixed
- **`write_bytes_atomic` no-clobber is now atomic.** With `overwrite=False` it used `exists()`-then-`os.replace`, a check-then-write race that could still clobber a file created in between. It now uses `os.link`, which fails atomically if the target exists. (jimprosser/obsidian-web-mcp#61 review)
- **Vault analytics no longer reads whole files unbounded.** `analytics._load_posts` pulled every markdown file fully into memory; one pathological multi-hundred-MB note (or a very large vault) could spike memory on the tunnel-reachable server. It now reads at most `MAX_CONTENT_SIZE` (1 MB) per file (top-of-file frontmatter still parses) and drops the per-post duplicate `text`/`name` fields that nothing consumed. (jimprosser/obsidian-web-mcp#59 review)

### Notes
- All three were found by Jim's upstream review of the analogous contributions; this release backports the same hardening to the fork's live, tunnel-exposed deployment.

## [v0.8.13] - 2026-06-16

### Security
- **`vault_import_url` is now SSRF-hardened.** The previous guard resolved the hostname and checked the IP, then handed the URL to `urlopen` — defeatable two ways, both now closed:
  - **DNS rebinding / TOCTOU.** The validator and `urlopen` resolved the name independently, so a malicious DNS could return a public IP to the check and a private IP (e.g. `169.254.169.254`, loopback) to the connect. The new `url_fetch.fetch_url` resolves **once** and pins the connection to the validated IP, preserving the original `Host` header and TLS SNI/certificate hostname.
  - **Redirects.** `urlopen` auto-followed `30x` and only the first URL was validated, so a public URL could redirect to an internal one. Redirects are no longer auto-followed; **every hop is re-validated and re-pinned** against a hop budget (`VAULT_IMPORT_URL_MAX_REDIRECTS`, default 5).
  - Defense in depth: public-IP-only by default (`is_global`, IPv4-mapped IPv6 unwrapped so `::ffff:169.254.169.254` cannot smuggle a target), scheme allowlist, and a port allowlist (`VAULT_IMPORT_URL_ALLOWED_PORTS`, default `80,443`). `VAULT_IMPORT_URL_ALLOW_PRIVATE=true` remains the explicit opt-in for trusted single-user deployments.
- New module `obsidian_vault_mcp/url_fetch.py` holds the hardened fetcher; it is mirrored by the `obsidian-vault-mcp-ext` ImportExtension so the two stay in sync.

### Tests
- `tests/test_url_fetch.py`: rebinding-closed (resolve once + pin), redirect-to-metadata rejected, redirect-budget, IP classification, scheme/port guards, size cap. Existing `vault_import_url` tests now stub the `fetch_url` seam.

## [v0.8.12] - 2026-06-15

### Changed
- **Serialization converged onto upstream's module (#9).** Added `serialization.py` (byte-identical to `jimprosser/obsidian-web-mcp`'s) and routed `vault_json_dumps` through it. Tool responses now use compact separators like upstream (`{"a":1}` rather than `{"a": 1}`) — token-efficient and semantically identical JSON; the date/datetime encoder and the non-UTF-8/surrogate fallback are unchanged. `vault_json_dumps` remains a thin alias; the canonical name is `serialization.dumps`. This is the keystone for the planned re-platform: shared primitives now match upstream, so feature modules become portable onto stock `upstream/main`.

## [v0.8.11] - 2026-06-14

### Security
- **Audit log rejected inside the vault (fail-closed).** If `VAULT_AUDIT_LOG_PATH` resolves inside `VAULT_PATH`, the server now refuses to start: a same-vault log is reachable by the vault tools (`vault_write`/`vault_delete`), which would let an authenticated caller tamper with or delete the trail. Backported from the upstream review of jimprosser/obsidian-web-mcp#56.

### Fixed
- **Batch mutations record correct per-file status.** `vault_batch_frontmatter_update` and `vault_batch_replace` now emit **one audit record per file** with that file's own `operation_status` and real before/after size+checksum. Previously a batch with per-file failures was logged as a single `success` and the list `target_path` produced null snapshots. (jimprosser/obsidian-web-mcp#56)
- **`vault_edit(dry_run=true)` is no longer recorded as a mutation** (it writes nothing).

### Tests
- `tests/test_audit_hardening.py`: in-vault rejection, per-file batch status (frontmatter + replace), dry-run skip, and an HTTP-level test that the bearer middleware propagates the principal to the sync tool layer (the path the audit's `token_id_hash` depends on).

## [v0.8.10] - 2026-06-14

### Changed
- **Push-heartbeat hardened (inbound from upstream jimprosser/obsidian-web-mcp#45).** The optional `VAULT_MCP_HEARTBEAT_URL` ping now refuses to follow redirects (a redirect could leak the capability URL to another host), reads at most 1 KB of the response, validates its configuration fail-closed at startup (`validate_heartbeat()` rejects a non-`http(s)` URL or a non-positive interval, so a typo cannot boot a server that silently never pings), and no longer stores or logs the raw exception on failure (it could contain the secret URL). The richer `/health` heartbeat state is unchanged.

### Tests
- `tests/test_heartbeat_hardening.py` (10 cases): `validate_heartbeat` enabled/disabled, bad URL scheme/host, non-positive interval, error messages never echo the capability URL, and the no-redirect handler.

### Notes
- Upstream's configurable mount path (jimprosser/obsidian-web-mcp#43, `VAULT_MCP_PATH`) was evaluated and intentionally **not** adopted: this fork serves MCP at `/` through its own compatibility middleware + root probe, so there is no mount-path knob to make configurable and nothing for a path validator to guard.

## [v0.8.9] - 2026-06-13

### Fixed
- `VAULT_MCP_ALLOWED_HOSTS` now **appends** to the always-present loopback defaults instead of replacing the list. Previously, setting the env var without re-listing `127.0.0.1:*`/`localhost:*`/`[::1]:*` dropped loopback (a lockout footgun). New `effective_allowed_hosts()` composes loopback + operator hosts, de-duplicated. Matches upstream's append semantics (jimprosser/obsidian-web-mcp#34).

### Docs
- README: added a "Relationship to upstream" section describing the cooperative fork model — generic capabilities are offered back upstream, config/tool conventions are kept aligned (`VAULT_MCP_*`, `vault_edit`), and the fork stays the daily driver with a fork-specific workflow layer.

## [v0.8.8] - 2026-06-13

### Added
- **`vault_edit` tool — upstream-compatible edit contract.** Applies an ordered list of exact text replacements to a file in one call: each `old_text` must match exactly once, edits apply in order, and `dry_run=true` returns a unified diff without writing. Accepts `old_str`/`new_str` as aliases for `old_text`/`new_text`. Writes go through the verified atomic path; the tool is audited as a mutation. Convergence toward upstream's `vault_edit` so the same edit shape works across fork and upstream; the existing `vault_str_replace` / `vault_patch` / `vault_batch_replace` tools are unchanged.

### Tests
- `tests/test_vault_edit.py` (10 cases): dry-run preview without writing, single/multiple ordered edits, `old_str`/`new_str` aliases, exactly-once enforcement (0 and >1 matches leave the file untouched), `old_text`+`old_str` conflict, missing file, model validation (empty edits / empty `old_text`), and the server wiring seam.

## [v0.8.7] - 2026-06-13

### Changed
- **Server/transport env vars converged to upstream's `VAULT_MCP_*` names**, to lower the friction of pulling future upstream changes back into this fork. Canonical names now: `VAULT_MCP_ALLOWED_HOSTS`, `VAULT_MCP_FORWARDED_ALLOW_IPS`, `VAULT_MCP_PUBLIC_URL`. The previous names (`VAULT_ALLOWED_HOSTS`, `VAULT_TRUSTED_PROXY_IPS`, `VAULT_PUBLIC_BASE_URL`) keep working as **deprecated aliases** and emit a one-line warning, so existing systemd units / `.env` files are not broken. OAuth (`VAULT_OAUTH_AUTH_*`) is intentionally left unchanged — the fork's auth design differs from upstream's.
- Added `VAULT_MCP_HOST` (default `0.0.0.0`, preserving current bind behavior) so the listen host is configurable and named like upstream, instead of hard-coding `0.0.0.0` in `server.py`.

### Tests
- `tests/test_config_aliases.py`: canonical name wins, deprecated alias is honored with a warning, default applies when neither is set (scalar and CSV variants).

## [v0.8.6] - 2026-06-11

### Fixed
- **Non-UTF-8 filenames no longer break whole tool responses (#14, regression from v0.8.5).** After v0.8.5 switched `vault_json_dumps` to `ensure_ascii=False`, a file whose on-disk name is not valid UTF-8 (a lone surrogate via `surrogateescape`) was emitted verbatim and the MCP transport then failed to UTF-8 encode the entire response, taking neighbouring valid notes down with it. `vault_json_dumps` now verifies UTF-8 encodability and falls back to escaped output for that one response only, preserving the v0.8.5 token savings for every valid-UTF-8 vault. Mirrors the second half of upstream jimprosser/obsidian-web-mcp#38.
- **`vault_str_replace` / `vault_batch_replace` no longer corrupt files on an empty `old_str` with `replace_all=True` (#13).** `str.count("")` returns `len+1` (never 0) and `str.replace("", new)` interleaves `new` between every character, slipping past the not-found and uniqueness guards; the atomic-write read-back did not catch it. `_replace_in_content` now rejects an empty `old_str` up front.

### Tests
- `tests/test_json_utf8.py`: a lone-surrogate payload stays UTF-8 encodable and round-trips.
- `tests/test_replace_guards.py`: empty `old_str` (and a missing `old_str` key in batch) is rejected and leaves the file untouched.

## [v0.8.5] - 2026-06-11

### Fixed
- Tool responses now emit non-ASCII text as UTF-8 instead of `\uXXXX` escape sequences. `vault_json_dumps` defaulted to `ensure_ascii=True` (the stdlib `json.dumps` default), which inflated token counts for non-ASCII vaults and broke verbatim round-trips of non-ASCII paths. It now defaults `ensure_ascii=False`; callers can still override it for ASCII-only on-disk state. Mirrors the upstream fix in jimprosser/obsidian-web-mcp#38 / issue #49.

### Tests
- `tests/test_json_utf8.py`: non-ASCII emitted verbatim (no `\u` escapes), `ensure_ascii=True` override still works, date-encoder still applied.

## [v0.8.4] - 2026-06-11

### Security
- `/oauth/authorize` now **fails closed** when no login credentials are configured. Previously, with `VAULT_OAUTH_AUTH_USERNAME`/`VAULT_OAUTH_AUTH_PASSWORD` unset, the endpoint auto-approved and issued an authorization code to any caller, who could then exchange it for the vault bearer token. The endpoint now returns `503 server_error` in that state.
- New `VAULT_OAUTH_ALLOW_NO_AUTH` env var (default `false`) is the explicit, intentionally insecure opt-in that restores unauthenticated auto-approval for local/dev use. Production deployments should leave it unset and configure login credentials instead.
- Mirrors the upstream GHSA-hwhg-mrjc-8g43 remediation (this fork already fixed the `/oauth/token` PKCE/client-identity and `/oauth/register` secret-leak issues in v0.7.3 and v0.6.x).

### Tests
- 3 new cases in `tests/test_oauth_hardening.py`: fail-closed without credentials (503), explicit opt-in restores auto-approval (302), and login form is shown when credentials are configured. The fixture now opts into auto-approval explicitly so the client-auth/PKCE cases stay focused.

## [v0.8.3] - 2026-05-31

### Changed
- **Bootstrap baseline for absolute templates uses the template's `created` date** when `last_run` is absent. An anchor fires when its trigger is on or after `created` and on or before `as_of`. Pre-v0.8.3 behavior would silently `skipped: not_due` even at the anchor moment itself, losing a full period for templates installed before their first anchor (e.g. quarterly templates installed mid-quarter would miss the next quarter end and only fire the one after that).
- Anchors strictly before `created` still `skipped: not_due` — no retroactive backfill of pre-template history. Anchors strictly after `as_of` likewise `skipped: not_due`. The change only opens the bootstrap window between those two endpoints.

### Invariant
- `last_run` remains tool-managed only. To shift the bootstrap baseline, hand-edit `created` (not `last_run`).

### Docs
- `docs/recurring.md` "Bootstrap behavior" section rewritten for the new absolute path; the Q2-UVA example illustrates the typical use case.

### Tests
- 4 new cases for the bootstrap-with-created paths: trigger ON `as_of` fires, trigger before `created` is `not_due`, trigger after `as_of` is `not_due`, no anchor in window is `not_due`. The previous "always not_due without last_run" test is rewritten to require `created` to also be absent.
- 49 recurring cases total, 280 in the full suite, zero regressions.

## [v0.8.2] - 2026-05-31

### Changed
- **Canonical template schema aligned with Tasks-Schema v0.7+**:
  - `target_folder` (formerly `instance_folder`) is the canonical key for the instance directory. `instance_folder` is kept as a legacy alias and emits a warning when used.
  - `frontmatter_to_inherit` accepts the dict form `{key: value}` (canonical, DRY: values live once in the inheritance map). The previous list form `[key1, key2]` (which read values from top-level template frontmatter) remains as a legacy alias and emits a warning when used.
  - When both canonical and legacy keys are present on the same template, the canonical form wins and a warning is recorded.
- Tool response now includes a `warnings` field aggregating per-template schema-shape advisories so misconfiguration surfaces in the response rather than failing silently. Empty inheritance configuration that resolves to zero fields also produces a warning.

### Docs
- `docs/recurring.md` updated to use the canonical schema in the worked example and to document the alias / conflict / silent-failure-prevention behavior.

### Tests
- 5 new test cases covering: `instance_folder` legacy alias warning; `target_folder` precedence when both keys are set; `frontmatter_to_inherit` list form warning; list form with no matching keys → "nothing inherited" warning; invalid type (string) → type warning.
- All 46 recurring test cases green (277 total in the full suite).

## [v0.8.1] - 2026-05-31

### Changed
- **Bootstrap semantics for fresh templates** are now mode-aware:
  - **Absolute** templates without `last_run` no longer backfill the most recent already-triggered period. They report `skipped: not_due` instead. Rationale: a freshly installed quarterly template should not retroactively claim that historical periods are open inbox items — those are typically already handled elsewhere. The first real firing happens once `last_run` exists.
  - **Relative** templates without any baseline (no done instance, no `last_run`) now bootstrap immediately with `trigger_date = today` and `period_key = today.isoformat()`. Rationale: relative templates are self-driven cadences; the previous `skipped: no_baseline_for_relative` behavior meant the template would never fire on its own.
- Updated `docs/recurring.md` with a dedicated **Bootstrap behavior** section so the two paths are explicit per mode.

### Tests
- Test suite restructured around the new semantics: existing absolute fixtures now include explicit `last_run` baselines; new tests cover the conservative absolute path (`not_due` for both past and future anchors) and the relative bootstrap path (instance + idempotency on second same-day call). All 41 recurring tests green (272 total).

## [v0.8.0] - 2026-05-25

### Added
- New MCP tool `recurring_materialize` that turns `recurring-template` notes into concrete task instances. Supports absolute anchors (`month_end`, `month_start`, `quarter_end_plus_Nd`, `fixed-MM-DD`, `T-N-before-MM-DD`) and relative intervals (`Nd`, `Nm`). Strictly idempotent on `(recurrence_template, recurrence_period)` via the frontmatter index.
- Optional internal scheduler: when `VAULT_RECURRING_INTERVAL > 0`, the server lifespan runs `recurring_materialize` on a cadence with crash-isolated error handling.
- New `vault-recurring` CLI entry point for systemd-timer setups (`vault-recurring run [--dry-run] [--template-id ID] [--as-of YYYY-MM-DD] [--no-index]`).
- Five new env vars: `VAULT_RECURRING_ENABLED`, `VAULT_RECURRING_TEMPLATES_FOLDER`, `VAULT_RECURRING_INTERVAL`, `VAULT_RECURRING_DONE_STATUS`, `VAULT_RECURRING_CATCHUP_MODE` (values `next` / `all`).

### Tests
- 38 new test cases in `tests/test_recurring.py` covering anchor / interval parsing, idempotency, catch-up modes, dry-run, inactive templates, disabled feature flag, and the relative-no-baseline path.

### Docs
- New `docs/recurring.md` with template schema, anchor reference, catch-up behavior, and a systemd timer example.

## [v0.7.3] - 2026-05-24

### Security
- PKCE (S256) is now mandatory for all OAuth authorize requests, not just public clients. Confidential clients (`client_secret_post`) must also present a `code_challenge`.
- The `authorization_code` grant strictly validates `client_id` and `client_secret` before touching the auth code or running PKCE verification. The previous "PKCE substitutes for client_secret" path is removed.
- Verified that the startup fallback to an unauthenticated `mcp.run()` is gone (was already removed in an earlier release; this release adds regression tests).
- Added `MCP-Protocol-Version: 2025-06-18` probe response on `GET`/`HEAD /` if not already present.

### Tests
- New `tests/test_oauth_hardening.py` with 13 regression cases covering every accept/reject path of the OAuth flow.

### References
- Aligned with [jjsmackay/obsidian-web-mcp](https://github.com/jjsmackay/obsidian-web-mcp) commits `a9084d5`, `a525d64`.

## [v0.7.2] - 2026-05-24

### Changed
- Raise the `vault_search_frontmatter` limit to 500 via a frontmatter-specific safety cap while keeping regular text search capped separately.
- Add `offset`, `total_matches`, `returned`, and `next_offset` metadata so large frontmatter result sets can be paged without silent cuts.
- Add a frontmatter response byte budget that sets `truncated=true` and `truncated_by_response_size=true` when a response is shortened for token safety.

### Tests
- Add synthetic 200+ frontmatter fixtures covering `max_results=200`, pagination, and response-size truncation.

## [v0.7.1] - 2026-05-24

### Fixed
- Restore the public `vault_str_replace` MCP contract to `path`, `old_string`, `new_string`, and `replace_all`, matching existing Claude and ChatGPT clients.
- Add regression coverage for documented `vault_str_replace` keyword calls and schema discovery.

## [v0.7.0] - 2026-05-15

### BREAKING CHANGES
- Removed `vault_upload_init`, `vault_upload_part`, `vault_upload_status`, `vault_upload_commit`, and `vault_upload_abort`. Migrate to `vault_request_upload_url` plus signed `POST /upload/{id}`.

### Changed
- Fully restructured the README around At a Glance, Application Scenarios, Capabilities, Plugin Bridge, Configuration, Security, and Audit.
- Updated binary upload guidance so the signed direct-upload flow is the only recommended large-file path.

### Added
- Added `docs/` guides for audit, OAuth, Plugin Bridge, configuration, security, and headless Linux/Proxmox deployment.

## [v0.6.10] - 2026-05-15

### Features
- Add persistent OCR sidecars for image-only PDFs: successful external OCR results are cached next to the source PDF as `*.pdf.ocr.txt` with source mtime and SHA-256 metadata.
- Add `VAULT_PDF_OCR_SIDECAR_ENABLED` and `VAULT_PDF_OCR_SIDECAR_SUFFIX` to control sidecar generation and naming.
- Add `include_ocr_sidecars` to `vault_list`; generated OCR sidecars stay hidden from default listings but remain available for search and explicit listing.

### Reliability / Operator UX
- Reuse valid OCR sidecars on later reads, avoiding repeated OCR process launches for unchanged scan PDFs.
- Invalidate OCR sidecars automatically when the source PDF mtime or first-64KB SHA-256 fingerprint changes.
- Guard concurrent first reads with a dotfile lock so only one OCR command runs per uncached PDF.
- Return stable OCR error codes (`ocr_tool_unavailable`, `ocr_timeout`, `ocr_failed`) without creating partial sidecars on failure.

### Docs / Tests
- Add regression coverage for sidecar validity checks, cache hits, invalidation, concurrency locking, disabled-sidecar behavior, OCR failure mapping, and listing visibility.
- Document v0.6.10 OCR sidecar smoke scope for live validation.

## [v0.6.9] - 2026-05-15

### Deprecated
- Deprecated `vault_upload_init`, `vault_upload_part`, `vault_upload_status`, `vault_upload_commit`, and `vault_upload_abort`. Use `vault_request_upload_url` plus signed `POST /upload/{id}` instead. Removal is planned for `v0.7.0`.

### Reliability / Operator UX
- Legacy upload tool responses now include `deprecated=true` and a client-visible migration warning while preserving existing functionality.
- Deprecated upload tool calls emit a server WARNING with the tool name and client family so operators can identify clients that still need migration.
- MCP tool descriptions are prefixed with `DEPRECATED - use vault_request_upload_url instead.` for schema discoverability.

## [v0.6.8] - 2026-05-15

### Features
- Complete the audit pipeline with post-write hook coupling: when both `VAULT_MCP_POST_WRITE_CMD` and `VAULT_AUDIT_LOG_PATH` are set, the hook receives the same JSONL audit record on stdin.
- Add opt-in read auditing via `VAULT_AUDIT_LOG_INCLUDE_READS=true` for read, list, search, semantic search, analytics, and Canvas read operations.
- Add Obsidian Canvas tools: `vault_canvas_read`, `vault_canvas_add_node`, and `vault_canvas_add_edge`.

### Reliability / Operator UX
- `/health` detailed output now reports audit status, including log path, last successful audit write, rolling 24h write-error count, rolling 24h bytes written, and whether read auditing is enabled.
- Audit health counters are process-local and reset on service restart; `/health` does not read the audit log from disk.
- Canvas writes preserve existing node/edge order and extra fields, generate collision-safe alphanumeric IDs when omitted, and write through the existing read-back verification path.
- Canvas write tools participate in mutation audit logging; `vault_canvas_read` participates in read audit only when `VAULT_AUDIT_LOG_INCLUDE_READS=true`.

### Docs / Tests
- Add regression coverage for hook stdin forwarding, hook failure non-rollback, read-audit opt-in/default-off behavior, audit health rolling counters, Canvas schema discoverability, and Canvas roundtrips.
- Document v0.6.8 smoke scope for Audit Pipeline and Canvas.

## [v0.6.7] - 2026-05-14

### Features
- Add `vault_template_list`, `vault_template_render`, and `vault_template_apply` for a strictly scoped server-side template flow.
- Add `vault_dataview_query` for Dataview TABLE DQL via Obsidian Local REST API.
- Add `VAULT_OBSIDIAN_REST_URL`, `VAULT_OBSIDIAN_REST_API_KEY`, `VAULT_OBSIDIAN_REST_VERIFY_TLS`, `VAULT_OBSIDIAN_REST_TIMEOUT`, `VAULT_TEMPLATER_FOLDER`, and `VAULT_DATAVIEW_TIMEOUT`.

### Reliability / Operator UX
- Template rendering is intentionally simple `{{ }}` variable substitution, not full Templater execution; Templater `<%` syntax fails closed with `error_code="template_render_unavailable"` and no write.
- `vault_template_apply` writes through the existing verified `vault_write` path and respects `overwrite=false` with `error_code="target_exists"`.
- `/health` now reports Local REST API configuration/reachability and warns when TLS verification is disabled for a non-loopback URL.
- Dataview script queries remain out of the schema; `query_type` is strictly `enum=["dql"]`.

### Docs / Tests
- Document the Local REST API configuration and the Simple-Renderer limitation.
- Add regression coverage for template path policy, variable edge cases, Templater-syntax rejection, Dataview REST error mapping, health status, and schema discoverability.

## [v0.6.6] - 2026-05-14

### Features
- Add append-only JSON-lines audit logging for vault mutations via `VAULT_AUDIT_LOG_PATH`.
- Audit records include UTC timestamp, token hash, client id/family, operation, target path, before/after size and checksum, request id, and `operation_status`.

### Reliability / Operator UX
- Audit logging covers MCP write/mutation tools plus signed direct uploads through `POST /upload/{id}`.
- Failed tool-level mutations emit `operation_status="error"` without rolling back the original tool result or turning audit failures into client-facing errors.

## [v0.6.5] - 2026-05-14

### Features
- Add Daily Note tools: `vault_daily_note_path`, `vault_daily_note_read`, and `vault_daily_note_append`.
- Add `VAULT_DAILY_NOTES_FOLDER`, `VAULT_DAILY_NOTES_FORMAT`, and `VAULT_DAILY_NOTES_TEMPLATE` so operators can align paths with their Obsidian Daily Notes setup.

### Reliability / Operator UX
- `vault_daily_note_append` creates missing daily notes through the existing verified `vault_append` write path, including read-back verification.
- Missing daily-note reads return `error_code="daily_note_not_found"` with `status_code=404` in the tool payload instead of silently creating a note.

## [v0.6.4] - 2026-05-12

### Features
- Add `vault_request_upload_url` plus signed `POST /upload/{upload_id}` direct uploads for agent-local binary files, avoiding MCP argument-size limits for PDFs and other attachments.

### Reliability / Operator UX
- Log direct upload completions and rejections, and steer tool descriptions away from legacy chunked upload flows for practical agent file transfer.
- Clean up binary-ingestion tool guidance so agents prefer `vault_request_upload_url` for real file uploads while keeping legacy `vault_upload_*` tools available until observed usage supports removal.
- Refresh the frontmatter index synchronously after `vault_move`, `vault_delete`, and `vault_delete_directory` so moved or deleted Markdown files do not remain as ghost paths in `vault_search_frontmatter`.
- Teach the frontmatter filesystem watcher to process move/rename events by removing the old path and indexing the new path.
- Disable HTTP access logs so short-lived signed upload URLs are not written to the service journal.

### Docs / Tests
- Document direct upload configuration (`VAULT_UPLOAD_URL_SECRET`, TTL limits) and add regression tests for signed upload success and bad-signature rejection.
- Add regression coverage for direct uploads, moved/deleted frontmatter paths, watchdog move events, and Linux-compatible hook tests.

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
