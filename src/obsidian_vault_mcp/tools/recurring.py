"""Recurring task materialization.

This module turns "recurring-template" notes in the vault into concrete
task instances on a schedule. Templates declare WHEN a new instance becomes
due (via an anchor expression or a relative interval), WHAT the instance
should look like (which frontmatter fields to inherit, which tags, what
priority and due-offset), and WHERE to put it.

The tool is strictly idempotent: a second invocation for the same template
and period yields no new files (it returns skipped/already_exists).

Design notes:
- All filesystem writes go through ``vault.write_file_atomic`` + read-back
  verification (same path as ``vault_write``).
- Idempotency lookup uses the in-process frontmatter index, so it sees
  concurrent writes synchronously.
- Anchor / interval parsing is pure: ``compute_pending_periods``,
  ``compute_relative_period`` and ``parse_interval`` are unit-testable
  without a vault, without time mocking, and without filesystem state.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Iterator

from .. import config
from .. import frontmatter_io
from ..hooks import fire_post_write
from ..vault import (
    is_vault_path_allowed,
    read_file,
    resolve_vault_path,
    vault_json_dumps,
    write_file_atomic,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Pure anchor / interval logic
# --------------------------------------------------------------------------

_QUARTER_END_PLUS_RE = re.compile(r"^quarter_end_plus_(\d+)d$")
_FIXED_RE = re.compile(r"^fixed-(\d{2})-(\d{2})$")
_T_BEFORE_RE = re.compile(r"^T-(\d+)-before-(\d{2})-(\d{2})$")
_INTERVAL_RE = re.compile(r"^(\d+)([dm])$")


class AnchorError(ValueError):
    """Raised when an anchor or interval expression is malformed."""


@dataclass(frozen=True)
class TriggeredPeriod:
    """One period whose trigger date has fired.

    ``trigger_date`` is the calendar date when the instance becomes "due"
    in the anchor sense (before applying ``due_offset_days``).
    ``period_key`` is a deterministic string used together with the template
    id to enforce idempotency (e.g. ``q3-2026`` or ``2026-07-31``).
    """

    trigger_date: date
    period_key: str


@dataclass(frozen=True)
class Interval:
    """A non-zero positive interval of days or months."""

    n: int
    unit: str  # 'd' or 'm'

    def add_to(self, base: date) -> date:
        if self.unit == "d":
            return base + timedelta(days=self.n)
        if self.unit == "m":
            month_zero_based = base.month - 1 + self.n
            year = base.year + month_zero_based // 12
            month = (month_zero_based % 12) + 1
            day = min(base.day, calendar.monthrange(year, month)[1])
            return date(year, month, day)
        raise AnchorError(f"Unsupported interval unit: {self.unit!r}")


def parse_interval(spec: str) -> Interval:
    """Parse ``'7d'`` or ``'3m'`` into an :class:`Interval`."""
    if not isinstance(spec, str):
        raise AnchorError(f"Interval must be a string, got {type(spec).__name__}")
    match = _INTERVAL_RE.match(spec.strip())
    if not match:
        raise AnchorError(
            f"Invalid interval format: {spec!r}; expected 'Nd' (days) or 'Nm' (months)"
        )
    n = int(match.group(1))
    unit = match.group(2)
    if n < 1:
        raise AnchorError(f"Interval must be >= 1: {spec!r}")
    return Interval(n=n, unit=unit)


def compute_relative_period(interval_spec: str, last_done_date: date) -> TriggeredPeriod:
    """Compute the next trigger for a relative-mode template.

    The trigger fires at ``last_done_date + interval``. The period key is the
    ISO date of the trigger, so two different runs that produce the same
    next-trigger collide on the idempotency key.
    """
    interval = parse_interval(interval_spec)
    trigger = interval.add_to(last_done_date)
    return TriggeredPeriod(trigger_date=trigger, period_key=trigger.isoformat())


def _quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def _quarter_end_date(year: int, quarter: int) -> date:
    last_month = quarter * 3
    last_day = calendar.monthrange(year, last_month)[1]
    return date(year, last_month, last_day)


_MAX_DAY_PER_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _validate_month_day(mm: int, dd: int) -> None:
    if not (1 <= mm <= 12):
        raise AnchorError(f"Invalid month: {mm}")
    max_day = _MAX_DAY_PER_MONTH[mm - 1]
    if not (1 <= dd <= max_day):
        raise AnchorError(f"Invalid day for month {mm:02d}: {dd}")


def _resolve_fixed_in_year(year: int, mm: int, dd: int) -> date | None:
    """Return ``date(year, mm, dd)`` or None if not a real calendar date.

    Used so anchors like ``fixed-02-29`` skip non-leap years instead of
    crashing on every invocation.
    """
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


_MAX_DESCENDING_STEPS = 4000  # ~333 years of monthly walks, hard hang preventer


def _iter_triggers_descending(anchor: str, as_of: date) -> Iterator[TriggeredPeriod]:
    """Yield triggers ``<= as_of`` in descending trigger-date order.

    Effectively infinite for well-formed anchors. A hard cap of
    ``_MAX_DESCENDING_STEPS`` internal iterations prevents true hangs
    even on pathological inputs.
    """
    if anchor == "month_end":
        year, month = as_of.year, as_of.month
        for _ in range(_MAX_DESCENDING_STEPS):
            last_day = calendar.monthrange(year, month)[1]
            trigger = date(year, month, last_day)
            if trigger <= as_of:
                yield TriggeredPeriod(trigger, f"{year:04d}-{month:02d}")
            month -= 1
            if month < 1:
                month = 12
                year -= 1
        return

    if anchor == "month_start":
        year, month = as_of.year, as_of.month
        for _ in range(_MAX_DESCENDING_STEPS):
            trigger = date(year, month, 1)
            if trigger <= as_of:
                yield TriggeredPeriod(trigger, f"{year:04d}-{month:02d}")
            month -= 1
            if month < 1:
                month = 12
                year -= 1
        return

    match = _QUARTER_END_PLUS_RE.match(anchor)
    if match:
        n_days = int(match.group(1))
        year = as_of.year
        quarter = _quarter_of(as_of.month)
        for _ in range(_MAX_DESCENDING_STEPS):
            qe = _quarter_end_date(year, quarter)
            trigger = qe + timedelta(days=n_days)
            if trigger <= as_of:
                yield TriggeredPeriod(trigger, f"q{quarter}-{year:04d}")
            quarter -= 1
            if quarter < 1:
                quarter = 4
                year -= 1
        return

    match = _FIXED_RE.match(anchor)
    if match:
        mm = int(match.group(1))
        dd = int(match.group(2))
        _validate_month_day(mm, dd)
        year = as_of.year
        for _ in range(_MAX_DESCENDING_STEPS):
            d = _resolve_fixed_in_year(year, mm, dd)
            if d is not None and d <= as_of:
                yield TriggeredPeriod(d, f"fixed-{mm:02d}-{dd:02d}-{year:04d}")
            year -= 1
        return

    match = _T_BEFORE_RE.match(anchor)
    if match:
        n = int(match.group(1))
        mm = int(match.group(2))
        dd = int(match.group(3))
        _validate_month_day(mm, dd)
        year = as_of.year
        for _ in range(_MAX_DESCENDING_STEPS):
            anchor_d = _resolve_fixed_in_year(year, mm, dd)
            if anchor_d is not None:
                trigger = anchor_d - timedelta(days=n)
                if trigger <= as_of:
                    yield TriggeredPeriod(
                        trigger, f"fixed-{mm:02d}-{dd:02d}-{year:04d}"
                    )
            year -= 1
        return

    raise AnchorError(f"Unsupported anchor expression: {anchor!r}")


def compute_pending_periods(
    anchor: str,
    as_of: date,
    since: date | None,
    *,
    catchup: str = "next",
    safety_limit: int = 50,
) -> list[TriggeredPeriod]:
    """Return absolute-anchor periods that should fire now.

    Sorted ascending by trigger date.

    - ``since=None`` (fresh install, no prior ``last_run``): return ``[]``.
      A freshly installed absolute template MUST NOT retroactively claim
      that past periods are open -- those are usually already-handled by
      other means (e.g. an accountant filed the Q1 VAT). The template only
      becomes active for FUTURE triggers; the first real firing happens
      when ``as_of >= trigger`` after ``last_run`` has been set by an
      earlier (no-op) invocation.
    - ``since`` set: standard catch-up from ``since`` to ``as_of``.
        - ``catchup='next'`` keeps only the most recent triggered period.
        - ``catchup='all'`` returns every period in (since, as_of].
    - ``safety_limit`` caps descent to prevent runaway walks.
    """
    if catchup not in {"next", "all"}:
        raise AnchorError(f"Invalid catchup mode: {catchup!r}")

    if since is None:
        # No baseline -> never backfill. This is the bootstrap-conservative
        # branch for absolute anchors; callers are expected to set last_run
        # on the template (e.g. to today) after the first observed run so
        # subsequent invocations have a baseline.
        return []

    collected: list[TriggeredPeriod] = []
    gen = _iter_triggers_descending(anchor, as_of)
    for _ in range(safety_limit):
        try:
            period = next(gen)
        except StopIteration:
            break
        if period.trigger_date <= since:
            break
        collected.append(period)

    collected.reverse()
    if catchup == "next" and len(collected) > 1:
        return collected[-1:]
    return collected


# --------------------------------------------------------------------------
# Template handling
# --------------------------------------------------------------------------


def _today() -> date:
    """Server-local current date. Indirected so tests can patch."""
    return datetime.now().date()


def _coerce_date(value: Any) -> date | None:
    """Coerce a frontmatter value to a date if possible, else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _format_iso(d: date) -> str:
    return d.isoformat()


def _list_template_paths(folder: str) -> list[str]:
    """List markdown files inside the templates folder, relative to the vault root."""
    folder_clean = folder.strip().strip("/\\")
    if not folder_clean:
        return []
    base_dir = resolve_vault_path(folder_clean)
    if not base_dir.is_dir():
        return []
    paths: list[str] = []
    for md_path in sorted(base_dir.rglob("*.md")):
        if md_path.is_symlink() or not md_path.is_file():
            continue
        if not is_vault_path_allowed(md_path):
            continue
        # All template paths must stay in this folder.
        rel = md_path.relative_to(resolve_vault_path("")).as_posix()
        paths.append(rel)
    return paths


def _instance_dir_for(
    template_meta: dict[str, Any],
    template_path: str,
    warnings: list[str] | None = None,
) -> str:
    """Return the directory (vault-relative, POSIX) where instances should be written.

    Schema resolution (canonical key first, legacy alias second):
      1. ``target_folder`` (canonical, Tasks-Schema v0.7+).
      2. ``instance_folder`` (legacy alias, emits a deprecation warning
         when used without ``target_folder``).
      3. Parent directory of the template itself (sibling fallback).

    When both keys are present, ``target_folder`` wins and a warning is
    appended so the operator can clean up the duplicate.
    """
    target = template_meta.get("target_folder")
    alias = template_meta.get("instance_folder")

    canonical = target if isinstance(target, str) and target.strip() else None
    legacy = alias if isinstance(alias, str) and alias.strip() else None

    if canonical is not None and legacy is not None:
        if warnings is not None:
            warnings.append(
                "both 'target_folder' and 'instance_folder' set; "
                "'target_folder' takes precedence"
            )
        return canonical.strip().strip("/\\")
    if canonical is not None:
        return canonical.strip().strip("/\\")
    if legacy is not None:
        if warnings is not None:
            warnings.append(
                "'instance_folder' is a legacy alias; prefer 'target_folder'"
            )
        return legacy.strip().strip("/\\")

    parent = PurePosixPath(template_path).parent.as_posix()
    return parent if parent and parent != "." else ""


def _build_instance_filename(template_id: str, period_key: str) -> str:
    """Build the slug used as the instance filename."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(template_id)).strip("-")
    safe_period = re.sub(r"[^A-Za-z0-9_-]+", "-", str(period_key)).strip("-")
    if not safe_id:
        safe_id = "tpl"
    if not safe_period:
        safe_period = "period"
    return f"recurring-{safe_id}-{safe_period}.md"


def _instance_relpath(
    template_meta: dict[str, Any],
    template_path: str,
    filename: str,
    warnings: list[str] | None = None,
) -> str:
    folder = _instance_dir_for(template_meta, template_path, warnings)
    if folder:
        return f"{folder}/{filename}"
    return filename


def _resolve_inheritance(
    template_meta: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the inherited frontmatter map per the canonical-with-alias schema.

    Canonical form (Tasks-Schema v0.7+): ``frontmatter_to_inherit`` is a dict
    of ``key: value`` pairs copied verbatim onto the instance. DRY: values
    live once in the template's inheritance map, not duplicated into
    top-level template frontmatter.

    Legacy alias: ``frontmatter_to_inherit`` as a list of key names; the
    tool looks up each key in the rest of the template frontmatter. Emits
    a deprecation warning.

    Returns the resolved ``{key: value}`` map (possibly empty). Warnings
    are appended to the supplied list, including a "configured but
    nothing copied" alarm that prevents lautloses Scheitern.
    """
    raw = template_meta.get("frontmatter_to_inherit")
    if raw is None:
        return {}

    inherited: dict[str, Any] = {}

    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                warnings.append(
                    "'frontmatter_to_inherit' contains a non-string key; ignored"
                )
                continue
            inherited[key] = value
        if not inherited:
            warnings.append(
                "'frontmatter_to_inherit' is configured as a dict but resolved to no fields"
            )
        return inherited

    if isinstance(raw, list):
        warnings.append(
            "'frontmatter_to_inherit' as a list of keys is a legacy form; "
            "prefer the dict {key: value} form"
        )
        for key in raw:
            if not isinstance(key, str) or not key.strip():
                continue
            if key in template_meta:
                inherited[key] = template_meta[key]
        if not inherited:
            warnings.append(
                "'frontmatter_to_inherit' (list form) is configured but no listed "
                "key was present in the template frontmatter; nothing inherited"
            )
        return inherited

    warnings.append(
        "'frontmatter_to_inherit' must be a dict (canonical) or list (legacy); "
        f"got {type(raw).__name__}; ignored"
    )
    return inherited


def _build_instance_content(
    *,
    template_id: str,
    template_meta: dict[str, Any],
    period_key: str,
    trigger_date: date,
    warnings: list[str] | None = None,
) -> str:
    """Render the instance markdown (frontmatter + optional body header).

    Inheritance is resolved via :func:`_resolve_inheritance`; warnings about
    legacy / mis-shaped inheritance configuration are appended to the
    supplied list so the calling tool can surface them in its response.
    """
    sink: list[str] = warnings if warnings is not None else []
    due_offset = int(template_meta.get("due_offset_days", 0) or 0)
    priority_initial = template_meta.get("priority_initial")
    tags_to_inherit = template_meta.get("tags_to_inherit") or []
    title_template = template_meta.get("instance_title")

    metadata: dict[str, Any] = {}

    # Stable identity fields first -- they make the file searchable.
    metadata["id"] = f"recurring-{template_id}-{period_key}"
    if title_template:
        metadata["title"] = str(title_template).format(
            template_id=template_id,
            period=period_key,
            trigger=_format_iso(trigger_date),
        )
    metadata["recurrence_template"] = str(template_id)
    metadata["recurrence_period"] = str(period_key)
    metadata["source"] = f"recurring-{template_id}-{period_key}"
    metadata["created"] = _format_iso(_today())
    metadata["due"] = _format_iso(trigger_date + timedelta(days=due_offset))

    if priority_initial is not None:
        metadata["priority"] = priority_initial

    # Inherit explicit frontmatter fields from the template (dict or legacy list).
    for key, value in _resolve_inheritance(template_meta, sink).items():
        metadata[key] = value

    # Tags: marker tag + inherited tags.
    tags: list[str] = ["recurring-instance"]
    if isinstance(tags_to_inherit, list):
        for tag in tags_to_inherit:
            if isinstance(tag, str) and tag and tag not in tags:
                tags.append(tag)
    metadata["tags"] = tags

    body = ""
    body_template = template_meta.get("body_template")
    if isinstance(body_template, str) and body_template:
        body = body_template.format(
            template_id=template_id,
            period=period_key,
            trigger=_format_iso(trigger_date),
            due=metadata["due"],
        )
        if not body.endswith("\n"):
            body += "\n"
    return frontmatter_io.dumps(metadata, body)


def _existing_instance_for(template_id: str, period_key: str) -> str | None:
    """Idempotency check via the live frontmatter index.

    Returns the relative path of an existing instance, or None.
    """
    try:
        from ..server import frontmatter_index
    except Exception:
        return None

    matches = frontmatter_index.search_by_field(
        field="recurrence_template",
        value=str(template_id),
        match_type="exact",
        filters=[
            {
                "field": "recurrence_period",
                "value": str(period_key),
                "match_type": "exact",
            }
        ],
    )
    if not matches:
        return None
    return matches[0].get("path")


def _refresh_index(rel_path: str, action: str) -> None:
    try:
        from ..server import frontmatter_index
    except Exception:
        return
    try:
        frontmatter_index.refresh_path(rel_path, action=action)
    except Exception:
        logger.warning("Frontmatter index refresh failed for %s", rel_path)


def _write_text_verified(rel_path: str, content: str) -> int:
    """Atomic write + read-back verification (mirrors tools.write._write_text_with_verification)."""
    _, size = write_file_atomic(rel_path, content, create_dirs=True)
    written_back, _ = read_file(rel_path)
    if written_back != content:
        raise RuntimeError(
            "Recurring instance write verification failed for {rel_path!r}".format(
                rel_path=rel_path
            )
        )
    return size


def _update_template_last_run(
    template_path: str,
    template_meta: dict[str, Any],
    template_body: str,
    last_run: date,
) -> None:
    """Update the template's ``last_run`` frontmatter to ``last_run`` (ruamel-stable)."""
    template_meta = dict(template_meta)
    template_meta["last_run"] = _format_iso(last_run)
    new_content = frontmatter_io.dumps(template_meta, template_body)
    _write_text_verified(template_path, new_content)
    _refresh_index(template_path, "modify")


def _read_template(rel_path: str) -> tuple[dict[str, Any], str] | None:
    """Parse a template file and return (metadata, body), or None if unusable."""
    try:
        content, _meta = read_file(rel_path)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Could not read template %s: %s", rel_path, exc)
        return None
    try:
        metadata, body = frontmatter_io.loads(content)
    except Exception as exc:
        logger.warning("Could not parse frontmatter in template %s: %s", rel_path, exc)
        return None
    return dict(metadata or {}), body


def _is_active_recurring_template(meta: dict[str, Any]) -> bool:
    if not meta:
        return False
    if meta.get("type") != "recurring-template":
        return False
    active = meta.get("active", True)
    if isinstance(active, str):
        return active.strip().lower() in {"1", "true", "yes", "on"}
    return bool(active)


def _last_done_for(template_id: str) -> date | None:
    """For relative-mode templates, find the most recent done instance's date."""
    try:
        from ..server import frontmatter_index
    except Exception:
        return None

    matches = frontmatter_index.search_by_field(
        field="recurrence_template",
        value=str(template_id),
        match_type="exact",
        filters=[
            {
                "field": "status",
                "value": config.VAULT_RECURRING_DONE_STATUS,
                "match_type": "exact",
            }
        ],
    )
    best: date | None = None
    for m in matches:
        fm = m.get("frontmatter", {}) or {}
        for candidate_key in ("closed", "done_at", "completed", "due", "updated", "created"):
            candidate = _coerce_date(fm.get(candidate_key))
            if candidate is None:
                continue
            if best is None or candidate > best:
                best = candidate
            break
    return best


def _process_template(
    *,
    template_path: str,
    template_meta: dict[str, Any],
    template_body: str,
    as_of: date,
    catchup: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Process one template; return its contribution to the aggregated result."""
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    template_id = template_meta.get("id") or template_meta.get("template_id")
    if not template_id:
        errors.append(
            {"path": template_path, "error": "template missing 'id' or 'template_id' frontmatter"}
        )
        return {"created": created, "skipped": skipped, "errors": errors, "warnings": warnings}
    template_id = str(template_id)

    anchor_mode = (template_meta.get("recurrence_anchor_mode") or "").strip().lower()
    if anchor_mode not in {"absolute", "relative"}:
        errors.append(
            {
                "path": template_path,
                "template_id": template_id,
                "error": (
                    "template missing or invalid 'recurrence_anchor_mode' "
                    "(must be 'absolute' or 'relative')"
                ),
            }
        )
        return {"created": created, "skipped": skipped, "errors": errors, "warnings": warnings}

    try:
        if anchor_mode == "absolute":
            anchor = (template_meta.get("recurrence_anchor") or "").strip()
            if not anchor:
                raise AnchorError("template missing 'recurrence_anchor' for absolute mode")
            since = _coerce_date(template_meta.get("last_run"))
            implicit_baseline_used = False
            if since is None:
                # Bootstrap: use the template's `created` date as an implicit
                # baseline. This lets a freshly installed quarterly template
                # actually fire when its first anchor arrives, without
                # forcing the operator to hand-seed last_run. Subtract one
                # day so a trigger ON the baseline date itself qualifies
                # as "after baseline".
                implicit = _coerce_date(template_meta.get("created"))
                if implicit is not None:
                    since = implicit - timedelta(days=1)
                    implicit_baseline_used = True
            periods = compute_pending_periods(anchor, as_of, since, catchup=catchup)
            # Bootstrap: no baseline at all (neither last_run nor created)
            # OR baseline present but nothing fired in scope -> not_due.
            if not periods and (
                since is None
                or implicit_baseline_used
            ):
                skipped.append(
                    {
                        "path": template_path,
                        "template_id": template_id,
                        "reason": "not_due",
                    }
                )
                return {"created": created, "skipped": skipped, "errors": errors, "warnings": warnings}
        else:
            interval_spec = (template_meta.get("recurrence_interval") or "").strip()
            if not interval_spec:
                raise AnchorError(
                    "template missing 'recurrence_interval' for relative mode"
                )
            last_done = _last_done_for(template_id) or _coerce_date(template_meta.get("last_run"))
            if last_done is None:
                # Bootstrap: a freshly installed relative template fires once
                # with trigger=today, so the cadence has a starting point.
                # Subsequent calls find this instance (or last_run) and proceed
                # from there.
                periods = [TriggeredPeriod(as_of, _format_iso(as_of))]
            else:
                candidate = compute_relative_period(interval_spec, last_done)
                if candidate.trigger_date > as_of:
                    skipped.append(
                        {
                            "path": template_path,
                            "template_id": template_id,
                            "reason": "not_yet_due",
                            "next_trigger": _format_iso(candidate.trigger_date),
                        }
                    )
                    return {"created": created, "skipped": skipped, "errors": errors, "warnings": warnings}
                periods = [candidate]
    except AnchorError as exc:
        errors.append(
            {"path": template_path, "template_id": template_id, "error": str(exc)}
        )
        return {"created": created, "skipped": skipped, "errors": errors, "warnings": warnings}

    if not periods:
        skipped.append(
            {
                "path": template_path,
                "template_id": template_id,
                "reason": "no_pending_periods",
            }
        )
        return {"created": created, "skipped": skipped, "errors": errors, "warnings": warnings}

    last_processed_trigger: date | None = None
    for period in periods:
        existing = _existing_instance_for(template_id, period.period_key)
        if existing:
            skipped.append(
                {
                    "template_id": template_id,
                    "period": period.period_key,
                    "reason": "already_exists",
                    "existing_path": existing,
                }
            )
            continue

        filename = _build_instance_filename(template_id, period.period_key)
        warn_messages: list[str] = []
        rel_path = _instance_relpath(
            template_meta, template_path, filename, warn_messages
        )
        content = _build_instance_content(
            template_id=template_id,
            template_meta=template_meta,
            period_key=period.period_key,
            trigger_date=period.trigger_date,
            warnings=warn_messages,
        )
        for msg in warn_messages:
            warnings.append(
                {
                    "template_id": template_id,
                    "period": period.period_key,
                    "warning": msg,
                }
            )
        if dry_run:
            created.append(
                {
                    "template_id": template_id,
                    "period": period.period_key,
                    "path": rel_path,
                    "trigger_date": _format_iso(period.trigger_date),
                    "dry_run": True,
                }
            )
            last_processed_trigger = period.trigger_date
            continue

        try:
            size = _write_text_verified(rel_path, content)
        except Exception as exc:
            errors.append(
                {
                    "template_id": template_id,
                    "period": period.period_key,
                    "path": rel_path,
                    "error": str(exc),
                }
            )
            continue
        _refresh_index(rel_path, "create")
        fire_post_write("created", [rel_path])
        created.append(
            {
                "template_id": template_id,
                "period": period.period_key,
                "path": rel_path,
                "trigger_date": _format_iso(period.trigger_date),
                "size": size,
            }
        )
        last_processed_trigger = period.trigger_date

    if not dry_run and last_processed_trigger is not None:
        try:
            _update_template_last_run(
                template_path, template_meta, template_body, last_processed_trigger
            )
        except Exception as exc:
            errors.append(
                {
                    "path": template_path,
                    "template_id": template_id,
                    "error": f"could not update template last_run: {exc}",
                }
            )

    return {"created": created, "skipped": skipped, "errors": errors, "warnings": warnings}


# --------------------------------------------------------------------------
# Public tool entry point
# --------------------------------------------------------------------------


def recurring_materialize(
    *,
    dry_run: bool = False,
    template_id: str | None = None,
    as_of: str | None = None,
) -> str:
    """Materialize pending recurring-template instances.

    Parameters
    ----------
    dry_run:
        If True, compute what would be created but make no filesystem
        changes and do not touch template ``last_run``.
    template_id:
        Restrict processing to a single template id. If a template with that
        id is not found, the response lists it under ``errors``.
    as_of:
        ISO date (YYYY-MM-DD) overriding the "current date" used for anchor
        resolution. Useful for backfills and tests. Defaults to today.

    Returns
    -------
    JSON string of ``{checked, created, skipped, errors, dry_run, as_of}``.
    """
    if not config.VAULT_RECURRING_ENABLED:
        return vault_json_dumps(
            {
                "error": "recurring materialization is disabled (VAULT_RECURRING_ENABLED=false)",
                "error_code": "recurring_disabled",
            }
        )

    folder = config.VAULT_RECURRING_TEMPLATES_FOLDER
    if not folder:
        return vault_json_dumps(
            {
                "error": (
                    "recurring materialization requires VAULT_RECURRING_TEMPLATES_FOLDER"
                ),
                "error_code": "recurring_folder_unset",
            }
        )

    try:
        as_of_date = (
            date.fromisoformat(as_of) if isinstance(as_of, str) and as_of else _today()
        )
    except ValueError:
        return vault_json_dumps(
            {
                "error": f"invalid as_of value: {as_of!r} (expected YYYY-MM-DD)",
                "error_code": "invalid_as_of",
            }
        )

    catchup = config.VAULT_RECURRING_CATCHUP_MODE

    template_paths = _list_template_paths(folder)
    aggregate_created: list[dict[str, Any]] = []
    aggregate_skipped: list[dict[str, Any]] = []
    aggregate_errors: list[dict[str, Any]] = []
    aggregate_warnings: list[dict[str, Any]] = []
    checked = 0
    matched_filter = False

    for path in template_paths:
        parsed = _read_template(path)
        if parsed is None:
            continue
        meta, body = parsed
        if not _is_active_recurring_template(meta):
            aggregate_skipped.append(
                {"path": path, "reason": "not_recurring_template_or_inactive"}
            )
            continue
        if template_id and str(meta.get("id") or meta.get("template_id")) != template_id:
            continue
        matched_filter = True
        checked += 1
        result = _process_template(
            template_path=path,
            template_meta=meta,
            template_body=body,
            as_of=as_of_date,
            catchup=catchup,
            dry_run=dry_run,
        )
        aggregate_created.extend(result["created"])
        aggregate_skipped.extend(result["skipped"])
        aggregate_errors.extend(result["errors"])
        aggregate_warnings.extend(result.get("warnings", []))

    if template_id and not matched_filter:
        aggregate_errors.append(
            {"template_id": template_id, "error": "template id not found"}
        )

    return vault_json_dumps(
        {
            "checked": checked,
            "created": aggregate_created,
            "skipped": aggregate_skipped,
            "errors": aggregate_errors,
            "warnings": aggregate_warnings,
            "dry_run": dry_run,
            "as_of": _format_iso(as_of_date),
            "catchup_mode": catchup,
        }
    )


# --------------------------------------------------------------------------
# CLI entry point (suitable for `vault-recurring` console script)
# --------------------------------------------------------------------------


def cli_main(argv: list[str] | None = None) -> int:
    """Standalone CLI for systemd-timer style invocation.

    Boots the frontmatter index against the configured vault, materializes
    pending periods once, prints the JSON result on stdout, and exits.
    Intended for ``systemd.timer`` setups; the in-process scheduler loop
    in the server lifespan is the preferred path for long-running servers.
    """
    import argparse
    import logging as _logging
    import sys as _sys

    parser = argparse.ArgumentParser(prog="vault-recurring")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run recurring materialization once.")
    run.add_argument("--dry-run", action="store_true", help="Compute but do not write.")
    run.add_argument("--template-id", default=None, help="Limit to a single template id.")
    run.add_argument("--as-of", default=None, help="Override current date (YYYY-MM-DD).")
    run.add_argument(
        "--no-index",
        action="store_true",
        help=(
            "Skip starting the frontmatter watcher. Faster startup, but idempotency "
            "and relative-mode 'last done' lookups degrade to file-existence checks."
        ),
    )

    args = parser.parse_args(argv)

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=_sys.stderr,
    )

    if not args.no_index:
        try:
            from ..server import frontmatter_index

            frontmatter_index.start()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not start frontmatter index; continuing without it")

    result = recurring_materialize(
        dry_run=args.dry_run,
        template_id=args.template_id,
        as_of=args.as_of,
    )
    _sys.stdout.write(result)
    _sys.stdout.write("\n")
    return 0

