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
instance_folder: 15_Tasks/pbs    # vault-relative folder for instances; defaults to the template's parent
instance_title: "Q-Report {period}"  # optional; {template_id}, {period}, {trigger} are interpolated
frontmatter_to_inherit:          # keys copied verbatim from template to instance
  - scope
  - project
tags_to_inherit:                 # appended to ['recurring-instance']
  - quarterly
  - reporting
scope: pbs
project: governance
# last_run is managed by the tool; do not edit by hand
---

# Optional body content
```

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

### Absolute templates: conservative

Without `last_run`, an absolute template **does not fire**, regardless of
whether the anchor has already triggered or is still in the future. The
template is reported as `skipped: not_due`.

Why: a freshly installed `quarter_end_plus_1d` template at the end of May
should not claim that Q1 is open as an inbox task — that period is usually
already handled elsewhere (an accountant filed the VAT, a colleague closed
the report). Surfacing it as an overdue task is a false alarm. The first
real firing happens once `last_run` exists; the second run with `since`
in place then catches up from there.

Operator note: the tool itself sets `last_run` only after a real firing
(non-dry-run). To "arm" a freshly installed absolute template without
materializing past periods, write a `last_run` field by hand to the date
you consider the baseline.

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
title: "Q-Report q2-2026"          # only if instance_title set
recurrence_template: q-report
recurrence_period: q2-2026
source: recurring-q-report-q2-2026
created: 2026-07-04
due: 2026-07-03                    # trigger_date + due_offset_days
priority: 2                        # from priority_initial
scope: pbs                         # inherited via frontmatter_to_inherit
project: governance
tags: [recurring-instance, quarterly, reporting]
---
```

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
