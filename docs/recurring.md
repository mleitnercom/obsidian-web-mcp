# Recurring Task Materialization

Back to [README](../README.md).

This feature turns "recurring-template" notes in the vault into concrete
task instances on a schedule. Templates declare WHEN a new instance becomes
due, WHAT it should look like, and WHERE to put it. The MCP tool
`recurring_materialize` (and an optional internal scheduler) handles the
"how".

The fork is **client-agnostic**: scopes, project slugs, status values, and
folder layouts are all strings configured per template; the fork does not
validate them against a particular task-OS schema.

## Why

You have a vault full of markdown task notes. Some tasks recur on calendar
beats (every quarter end, every 28.10. as a fixed date, 7 days before
12-31). Some recur on activity (7 days after the last "done"). Materializing
those instances by hand is error-prone and easy to forget. This tool turns
the templates into instances, **strictly idempotent**.

## Configuration

All control is via environment variables:

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `VAULT_RECURRING_ENABLED` | bool | `false` | Master switch. Tool returns `recurring_disabled` until set. |
| `VAULT_RECURRING_TEMPLATES_FOLDER` | string | _(none)_ | Vault-relative folder that contains the template notes. Required. |
| `VAULT_RECURRING_INTERVAL` | int (seconds) | `0` | If `> 0`, the server runs an internal scheduler loop at that cadence. `0` disables the loop (CLI / on-demand only). |
| `VAULT_RECURRING_DONE_STATUS` | string | `done` | The `status` value that marks an instance as "completed" for relative-mode templates. |
| `VAULT_RECURRING_INSTANCE_STATUS` | string | `next` | The `status` stamped onto a freshly materialized instance. A template can override it per-instance via `frontmatter_to_inherit`. |
| `VAULT_RECURRING_CATCHUP_MODE` | `next` \| `all` | `next` | Behavior when multiple periods are pending. `next` collapses to the most recent; `all` creates one instance per missed period. |

## Template Schema

A template is any markdown file inside `VAULT_RECURRING_TEMPLATES_FOLDER`
with `type: recurring-template` and `active: true`.

```yaml
---
id: q-report                     # required, used in instance ids and idempotency keys
type: recurring-template
active: true
recurrence_anchor_mode: absolute # 'absolute' or 'relative'
recurrence_anchor: quarter_end_plus_3d   # required for 'absolute'
# recurrence_interval: 7d        # required for 'relative' ('Nd' days, 'Nm' months)
due_offset_days: 0               # added to trigger date to compute the instance 'due'
priority_initial: 2              # written into instance frontmatter as 'priority'
target_folder: 15_Tasks/pbs      # vault-relative folder for instances; defaults to the template's parent
instance_title: "Q-Report {period}"  # optional; {template_id}, {period}, {trigger} are interpolated
frontmatter_to_inherit:          # canonical: map of fields copied verbatim onto the instance
  scope: pbs
  project: governance
tags_to_inherit:                 # appended to ['recurring-instance']
  - quarterly
  - reporting
# last_run is managed by the tool; do not edit by hand
---

# Optional body content
```

### Schema aliases (legacy compatibility)

Two keys accept a legacy form. The canonical form is preferred; the alias
emits a deprecation warning surfaced in the tool's `warnings` field but
still works:

| Canonical (since v0.8.2) | Legacy alias | Behavior on conflict |
|---|---|---|
| `target_folder: 15_Tasks/pbs` | `instance_folder: 15_Tasks/pbs` | If both set, `target_folder` wins + warning. |
| `frontmatter_to_inherit:`<br>`  scope: pbs`<br>`  project: governance` | `frontmatter_to_inherit:`<br>`  - scope`<br>`  - project`<br>(values read from template's top-level frontmatter) | Dict form wins when both forms are usable. |

The dict form for `frontmatter_to_inherit` is DRY: values live once in the
inheritance map, not duplicated as top-level template frontmatter. The list
form remains available for templates predating v0.8.2.

If `frontmatter_to_inherit` is configured but resolves to no fields (e.g. all
listed keys are typos in the legacy form), the tool emits a warning rather
than silently producing an empty inheritance.

### Absolute anchors

| Anchor | Trigger date | Period key |
|---|---|---|
| `month_end` | last day of the month | `YYYY-MM` |
| `month_start` | first day of the month | `YYYY-MM` |
| `quarter_end_plus_{n}d` | last day of the calendar quarter + n days | `qN-YYYY` |
| `fixed-{MM-DD}` | the given date each year | `fixed-MM-DD-YYYY` |
| `T-{n}-before-{MM-DD}` | n days before the given date | `fixed-MM-DD-YYYY` (the anchor date is the key) |

Quarter boundaries: Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec.
February 29 anchors silently skip non-leap years.

### Relative intervals

`recurrence_interval` accepts `Nd` (days) or `Nm` (months). Monthly arithmetic
is calendar-aware and clamps the day-of-month for short months (Jan 31 + 1m
→ Feb 28/29).

A relative template uses, in order of preference, the most recent existing
instance with `status == VAULT_RECURRING_DONE_STATUS`, then `last_run` in the
template. See **Bootstrap behavior** below for what happens when neither is
present.

## Bootstrap behavior

The semantics on the very first invocation of a freshly installed template
(no `last_run` yet, no done instances) differ by mode. This is deliberate:
the two modes encode different intents.

### Absolute templates: `created` is the implicit baseline

Without `last_run`, the tool uses the template's `created` frontmatter
date as an implicit baseline. An anchor fires when its trigger date is
on or after `created` AND on or before `as_of`. Specifically:

- Anchor trigger **before** `created` → `skipped: not_due` (no retroactive
  claim that historical periods are open).
- Anchor trigger **between `created` and `as_of`** (inclusive on both ends)
  → fires. This is the bootstrap moment: the first matching anchor
  produces the first real instance and `last_run` is set accordingly.
- Anchor trigger **after `as_of`** → `skipped: not_due` (future).

Without both `last_run` AND `created`, the template stays `not_due` forever
— the tool cannot tell what "since template existence" means. For UI-created
notes that omit `created`, set one by hand once.

Why: a freshly installed `quarter_end_plus_1d` template anchored on
2026-07-01 should fire on 2026-07-01 without operator intervention. The
previous (v0.8.1) behavior would have skipped silently and missed Q2
entirely; the next firing would have been Q3 on 2026-10-01 — one full
period lost.

Operator note: `last_run` is set only by the tool itself on real
(non-dry-run) firings. To shift the bootstrap baseline forward, hand-edit
`created`, not `last_run`.

### Relative templates: bootstrap with today

Without any baseline, a relative template **fires once immediately** with
`trigger_date = today` and `period_key = today.isoformat()`. The cadence
starts from this first instance: the next trigger is `today + recurrence_interval`.

Why: a relative template is a self-driven cadence ("every 7 days starting
now"). If the bootstrap path skipped, the template would never fire on its
own — the user would have to manually create a baseline instance before the
mechanic does anything. That defeats the purpose.

### Invariant

`last_run` is set only by the tool itself, on real (non-dry-run) firings.
It is the marker for "this template has fired at least once". Hand-editing
is allowed when you want to deliberately seed the baseline (see absolute
operator note above), but normal use is hands-off.

### What ends up in an instance

For a template with `id=q-report` and period `q2-2026`:

```yaml
---
id: recurring-q-report-q2-2026
title: Q-Report                    # instance_title if set, else the template's title verbatim
recurrence_template: q-report
recurrence_period: q2-2026
source: recurring-q-report-q2-2026
created: 2026-07-04
status: next                       # VAULT_RECURRING_INSTANCE_STATUS; overridable via frontmatter_to_inherit
due: 2026-07-03                    # trigger_date + due_offset_days
priority: 2                        # from priority_initial
scope: pbs                         # inherited via frontmatter_to_inherit
project: governance
updated: 2026-07-04                # the generation date, never the template date
tags: [recurring-instance, quarterly, reporting]
---

## Next Action
<the template's "## Next Action (Template)" section, else body_action, else the title>

## Verlauf

## Bezug
- [[q-report]]                     # link to the master template
```

Every instance is a schema-complete task: it always carries `title`, `status`
and `updated`, and a body with a `## Next Action` (copied from the template's
`## Next Action (Template)` section; overridable with an optional `body_action:`
string on the template; if neither is present the title is used as a placeholder
and a warning is returned), an empty `## Verlauf`, and a `## Bezug` link to the
master template. A template that resolves no title fails closed into the tool's
`errors` rather than writing an invisible, schema-invalid task. `dry_run: true`
returns the planned frontmatter under each `created` entry so the shape can be
checked without writing.

The instance file is named `recurring-{template_id}-{period}.md` and
lands in `instance_folder` (or the template's parent directory).

## Idempotency

The tool refuses to create a duplicate when:

1. An instance with matching `(recurrence_template, recurrence_period)`
   already exists in the in-memory frontmatter index, OR
2. The template's `last_run` has advanced past the trigger date (the
   period falls out of the "pending" window).

Both checks are needed: (1) handles cases where someone created the file
manually, dry-runs were used, or `last_run` was wiped; (2) is the normal
fast path. A second call with the same `as_of` is a no-op.

## Catch-up behavior

`VAULT_RECURRING_CATCHUP_MODE=next` (default) returns at most one period
per template per invocation — the most recent that has fired since
`last_run`. Long server downtime followed by a scheduler tick produces
**one** instance per template, not a flood.

`VAULT_RECURRING_CATCHUP_MODE=all` returns every missed period between
`last_run` and `as_of`. Use this when you actually want a full audit trail
of every period that fired, even retroactively.

If a template has no `last_run` at all (fresh install), the tool only ever
creates one instance regardless of mode: the single most recent triggered
period.

## Invocation paths

### MCP tool

```python
recurring_materialize(dry_run=False, template_id=None, as_of=None)
```

- `dry_run=True` computes what would be created, no filesystem writes,
  `last_run` not touched.
- `template_id` restricts processing to one template id.
- `as_of` overrides "today" with an ISO date for backfills or tests.

Returns JSON:
```json
{
  "checked": 3,
  "created": [{"template_id": "...", "period": "...", "path": "...", "trigger_date": "...", "size": 412}],
  "skipped": [{"template_id": "...", "period": "...", "reason": "already_exists", "existing_path": "..."}],
  "errors": [],
  "dry_run": false,
  "as_of": "2026-07-04",
  "catchup_mode": "next"
}
```

### Internal scheduler

If `VAULT_RECURRING_INTERVAL > 0`, the server lifespan starts an asyncio
task that calls `recurring_materialize()` at that cadence. Errors are
logged and swallowed so a misconfigured template never crashes the
service. Cancellation is clean on shutdown.

### CLI

The `vault-recurring` console script (installed alongside `vault-mcp`)
gives systemd-timer setups a way in:

```bash
vault-recurring run [--dry-run] [--template-id ID] [--as-of YYYY-MM-DD]
```

Example systemd timer fragment:

```ini
[Service]
Type=oneshot
User=michael
EnvironmentFile=/etc/obsidian-mcp-extra.env
ExecStart=/home/michael/obsidian-web-mcp-fork/venv/bin/vault-recurring run
```

The CLI starts the frontmatter index against the configured vault before
running. Pass `--no-index` to skip the watcher (faster, but
relative-mode last-done lookups degrade).

## Safety

- All writes go through the same `write_file_atomic` + read-back
  verification path as `vault_write`.
- The tool participates in the post-write hook, so audit/sync hooks see
  recurring instances.
- Anchor parsing is bounded: malformed expressions raise `AnchorError`
  and surface as `errors` in the response; the descending walk caps at
  4000 internal steps so misconfiguration cannot hang the server.

## Testing

Unit tests for anchor / interval logic and integration tests against a
temp vault live in `tests/test_recurring.py`. Both layers run with
`python -m pytest tests/test_recurring.py`.

## Not in scope

- The fork does not model "snooze", "skip this period", or
  "re-materialize". Those belong to the client / task-OS layer.
- No timezone handling: trigger calculations use the server's local date.
- No multi-vault support: one vault per server.
