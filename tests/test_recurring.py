"""Tests for the recurring-task materialization feature.

Covers:
 * Pure anchor / interval logic (no IO, no time mocking).
 * The MCP tool entry point against a temp vault with a started
   frontmatter index.

These tests must never touch the real vault: every test uses ``tmp_path``
and monkeypatches ``VAULT_PATH``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import obsidian_vault_mcp.config as config
import obsidian_vault_mcp.server as server
import obsidian_vault_mcp.tools.recurring as recurring
from obsidian_vault_mcp.frontmatter_index import FrontmatterIndex
from obsidian_vault_mcp.tools.recurring import (
    AnchorError,
    Interval,
    TriggeredPeriod,
    compute_pending_periods,
    compute_relative_period,
    parse_interval,
    recurring_materialize,
)


# --------------------------------------------------------------------------
# Pure anchor / interval tests
# --------------------------------------------------------------------------


def test_parse_interval_days():
    assert parse_interval("7d") == Interval(n=7, unit="d")


def test_parse_interval_months():
    assert parse_interval("3m") == Interval(n=3, unit="m")


@pytest.mark.parametrize("bad", ["", "abc", "0d", "-1d", "1w", "12", " ", "5 d"])
def test_parse_interval_rejects_bad(bad):
    with pytest.raises(AnchorError):
        parse_interval(bad)


def test_interval_add_to_days():
    assert Interval(7, "d").add_to(date(2026, 1, 1)) == date(2026, 1, 8)


def test_interval_add_to_months_simple():
    assert Interval(2, "m").add_to(date(2026, 1, 31)) == date(2026, 3, 31)


def test_interval_add_to_months_clamps_to_short_month():
    # Jan 31 + 1 month -> Feb 28 (or Feb 29 in leap years)
    assert Interval(1, "m").add_to(date(2026, 1, 31)) == date(2026, 2, 28)
    assert Interval(1, "m").add_to(date(2024, 1, 31)) == date(2024, 2, 29)


def test_interval_add_to_months_crosses_year():
    assert Interval(13, "m").add_to(date(2026, 7, 15)) == date(2027, 8, 15)


def test_compute_relative_period_returns_iso_period_key():
    period = compute_relative_period("7d", date(2026, 5, 1))
    assert period.trigger_date == date(2026, 5, 8)
    assert period.period_key == "2026-05-08"


def test_pending_periods_no_baseline_returns_empty():
    # Bootstrap-conservative: without a since baseline, absolute anchors
    # never backfill, regardless of where as_of falls.
    out = compute_pending_periods("month_end", date(2026, 5, 31), since=None)
    assert out == []
    out = compute_pending_periods("month_end", date(2026, 5, 15), since=None)
    assert out == []
    out = compute_pending_periods("quarter_end_plus_3d", date(2026, 7, 4), since=None)
    assert out == []


def test_pending_periods_with_baseline_catches_up():
    # With a since baseline 2026-04-30, May month_end is the next trigger.
    out = compute_pending_periods(
        "month_end", date(2026, 5, 31), since=date(2026, 4, 30)
    )
    assert out == [TriggeredPeriod(date(2026, 5, 31), "2026-05")]


def test_pending_periods_quarter_end_plus_3d_with_baseline():
    # Q2 ends 2026-06-30, +3d = 2026-07-03. Baseline before that -> fired.
    out = compute_pending_periods(
        "quarter_end_plus_3d", date(2026, 7, 4), since=date(2026, 6, 30)
    )
    assert out == [TriggeredPeriod(date(2026, 7, 3), "q2-2026")]


def test_pending_periods_quarter_end_plus_3d_catchup_all():
    # since 2025-12-31 -> we have Q4 2025 +3d (2026-01-03), Q1 2026 +3d (2026-04-03),
    # Q2 2026 +3d (2026-07-03)
    out = compute_pending_periods(
        "quarter_end_plus_3d",
        date(2026, 7, 10),
        since=date(2025, 12, 31),
        catchup="all",
    )
    assert [p.period_key for p in out] == ["q4-2025", "q1-2026", "q2-2026"]


def test_pending_periods_catchup_next_collapses():
    out = compute_pending_periods(
        "quarter_end_plus_3d",
        date(2026, 7, 10),
        since=date(2025, 12, 31),
        catchup="next",
    )
    assert [p.period_key for p in out] == ["q2-2026"]


def test_pending_periods_fixed_date_with_baseline():
    out = compute_pending_periods(
        "fixed-12-31", date(2026, 12, 31), since=date(2025, 12, 31)
    )
    assert out == [TriggeredPeriod(date(2026, 12, 31), "fixed-12-31-2026")]


def test_pending_periods_t_before_christmas_with_baseline():
    # T-7-before-12-31 -> trigger 2026-12-24, anchor key 2026
    out = compute_pending_periods(
        "T-7-before-12-31", date(2026, 12, 25), since=date(2025, 12, 24)
    )
    assert out == [TriggeredPeriod(date(2026, 12, 24), "fixed-12-31-2026")]


def test_pending_periods_since_blocks_repeat():
    # Already materialized May 2026 -> nothing pending in June
    out = compute_pending_periods(
        "month_end", date(2026, 6, 10), since=date(2026, 5, 31)
    )
    assert out == []


def test_pending_periods_invalid_anchor_raises():
    # Pass a since date so the empty-on-no-baseline short-circuit doesn't
    # swallow the error before the anchor is parsed.
    with pytest.raises(AnchorError):
        compute_pending_periods(
            "nonsense-anchor", date(2026, 1, 1), since=date(2025, 1, 1)
        )


def test_pending_periods_invalid_fixed_day_raises():
    with pytest.raises(AnchorError):
        compute_pending_periods(
            "fixed-02-30", date(2026, 5, 1), since=date(2025, 5, 1)
        )


# --------------------------------------------------------------------------
# Tool fixture against a temp vault
# --------------------------------------------------------------------------


@pytest.fixture
def recurring_vault(tmp_path, monkeypatch):
    """Temp vault with frontmatter index running, ready for recurring tests."""
    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "tasks").mkdir(parents=True)

    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "VAULT_PATH", Path(str(vault)))
    monkeypatch.setattr(config, "VAULT_RECURRING_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_RECURRING_TEMPLATES_FOLDER", "templates")
    monkeypatch.setattr(config, "VAULT_RECURRING_DONE_STATUS", "done")
    monkeypatch.setattr(config, "VAULT_RECURRING_CATCHUP_MODE", "next")

    # Swap in a freshly started frontmatter index for this test vault.
    fresh_index = FrontmatterIndex()
    monkeypatch.setattr(server, "frontmatter_index", fresh_index)
    fresh_index.start()
    try:
        yield vault, fresh_index
    finally:
        fresh_index.stop()


def _write_template(
    vault: Path, name: str, frontmatter: str, body: str = "", ensure_title: bool = True
) -> Path:
    # Tasks-Schema v0.8 (and recurring instances since v0.8.15) require a title;
    # give minimal fixtures a valid one unless the test deliberately omits it.
    has_title = any(
        line.strip().startswith("title:") for line in frontmatter.splitlines()
    )
    if ensure_title and not has_title:
        frontmatter = "title: Recurring Test Template\n" + frontmatter
    path = vault / "templates" / name
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return path


def _refresh_template_index(index: FrontmatterIndex, path: Path) -> None:
    rel = path.relative_to(config.VAULT_PATH).as_posix()
    index.refresh_path(rel, action="modify")


# --------------------------------------------------------------------------
# Tool behaviour tests
# --------------------------------------------------------------------------


def test_disabled_returns_error(monkeypatch, recurring_vault):
    monkeypatch.setattr(config, "VAULT_RECURRING_ENABLED", False)
    result = json.loads(recurring_materialize())
    assert result["error_code"] == "recurring_disabled"


def test_folder_unset_returns_error(monkeypatch, recurring_vault):
    monkeypatch.setattr(config, "VAULT_RECURRING_TEMPLATES_FOLDER", "")
    result = json.loads(recurring_materialize())
    assert result["error_code"] == "recurring_folder_unset"


def test_inactive_template_is_skipped(recurring_vault):
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "inactive.md",
        """id: q-report
type: recurring-template
active: false
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
last_run: 2026-04-30
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert result["checked"] == 0
    assert any(
        s.get("reason") == "not_recurring_template_or_inactive"
        for s in result["skipped"]
    )
    assert not result["created"]


def test_absolute_quarter_end_creates_instance(recurring_vault):
    """Canonical schema: target_folder + frontmatter_to_inherit as dict."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "tucho.md",
        """id: tucho-quarterly
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_3d
target_folder: tasks
due_offset_days: 0
priority_initial: 2
last_run: 2026-06-30
frontmatter_to_inherit:
  scope: privat
  project: garagenkauf
tags_to_inherit:
  - tucho
""",
        body="## Next Action (Template)\nBeleg prüfen und ablegen.\n",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-07-04"))
    assert result["checked"] == 1
    assert len(result["created"]) == 1
    created = result["created"][0]
    assert created["period"] == "q2-2026"
    assert created["trigger_date"] == "2026-07-03"
    assert created["path"] == "tasks/recurring-tucho-quarterly-q2-2026.md"
    assert result["warnings"] == []
    instance_path = vault / "tasks" / "recurring-tucho-quarterly-q2-2026.md"
    assert instance_path.exists()

    raw = instance_path.read_text(encoding="utf-8")
    assert "recurrence_template: tucho-quarterly" in raw
    assert "recurrence_period: q2-2026" in raw
    assert "due: '2026-07-03'" in raw or "due: 2026-07-03" in raw
    assert "scope: privat" in raw
    assert "project: garagenkauf" in raw
    assert "recurring-instance" in raw
    assert "tucho" in raw


def test_instance_folder_alias_still_works_with_warning(recurring_vault):
    """Legacy alias `instance_folder` writes to the same place but warns."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "legacy.md",
        """id: legacy
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
instance_folder: tasks
last_run: 2026-04-30
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert len(result["created"]) == 1
    assert (vault / "tasks" / "recurring-legacy-2026-05.md").exists()
    assert any("instance_folder" in w["warning"] for w in result["warnings"])


def test_target_folder_wins_when_both_present(recurring_vault):
    """If both target_folder and instance_folder are set, target_folder wins."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "conflict.md",
        """id: conflict
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
target_folder: tasks-canonical
instance_folder: tasks-legacy
last_run: 2026-04-30
""",
    )
    (vault / "tasks-canonical").mkdir()
    (vault / "tasks-legacy").mkdir()
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert len(result["created"]) == 1
    assert (vault / "tasks-canonical" / "recurring-conflict-2026-05.md").exists()
    assert not (vault / "tasks-legacy" / "recurring-conflict-2026-05.md").exists()
    assert any(
        "both 'target_folder' and 'instance_folder'" in w["warning"]
        for w in result["warnings"]
    )


def test_frontmatter_to_inherit_legacy_list_warns(recurring_vault):
    """List form copies values from template meta but emits a deprecation warning."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "list.md",
        """id: list-form
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
target_folder: tasks
last_run: 2026-04-30
frontmatter_to_inherit:
  - scope
  - project
scope: privat
project: foo
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert len(result["created"]) == 1
    raw = (vault / "tasks" / "recurring-list-form-2026-05.md").read_text(encoding="utf-8")
    assert "scope: privat" in raw
    assert "project: foo" in raw
    assert any("legacy" in w["warning"] for w in result["warnings"])


def test_frontmatter_to_inherit_list_with_no_matches_warns(recurring_vault):
    """List form referencing missing keys emits the 'nothing inherited' warning."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "empty-list.md",
        """id: empty-list
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
target_folder: tasks
last_run: 2026-04-30
frontmatter_to_inherit:
  - scope_typo
  - project_typo
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert len(result["created"]) == 1
    assert any(
        "nothing inherited" in w["warning"] for w in result["warnings"]
    )


def test_frontmatter_to_inherit_invalid_type_warns(recurring_vault):
    """Non-dict, non-list form emits a type warning."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "bogus.md",
        """id: bogus-inherit
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
target_folder: tasks
last_run: 2026-04-30
frontmatter_to_inherit: "should-be-a-dict-not-a-string"
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert len(result["created"]) == 1
    assert any("must be a dict" in w["warning"] for w in result["warnings"])


def test_absolute_no_last_run_no_created_does_not_backfill(recurring_vault):
    """Fresh absolute template + no created date -> not_due (no baseline)."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "uva.md",
        """id: uva
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_1d
target_folder: tasks
due_offset_days: 30
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-30"))
    assert result["created"] == []
    assert any(s.get("reason") == "not_due" for s in result["skipped"])
    assert not list((vault / "tasks").glob("recurring-uva-*.md"))


def test_absolute_bootstrap_with_created_fires_at_anchor(recurring_vault):
    """Template `created` acts as implicit baseline. Anchor at as_of fires."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "uva.md",
        """id: uva
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_1d
target_folder: tasks
due_offset_days: 30
created: 2026-04-01
""",
    )
    _refresh_template_index(index, tpl)
    # Q2 trigger 2026-07-01. as_of == trigger.
    result = json.loads(recurring_materialize(as_of="2026-07-01"))
    assert len(result["created"]) == 1
    created_entry = result["created"][0]
    assert created_entry["period"] == "q2-2026"
    assert created_entry["trigger_date"] == "2026-07-01"
    assert (vault / "tasks" / "recurring-uva-q2-2026.md").exists()


def test_absolute_bootstrap_anchor_before_created_is_not_due(recurring_vault):
    """Anchor predating template `created` is not retroactively fired."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "uva.md",
        """id: uva-late
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_1d
target_folder: tasks
created: 2026-05-01
""",
    )
    _refresh_template_index(index, tpl)
    # Q1 trigger 2026-04-01 < created 2026-05-01 -> ignore.
    # Q2 trigger 2026-07-01 > as_of 2026-06-30 -> not yet.
    result = json.loads(recurring_materialize(as_of="2026-06-30"))
    assert result["created"] == []
    assert any(s.get("reason") == "not_due" for s in result["skipped"])


def test_absolute_bootstrap_no_anchor_between_created_and_as_of(recurring_vault):
    """No anchor in (created, as_of] -> not_due even with created baseline."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "uva.md",
        """id: uva-fut
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_1d
target_folder: tasks
created: 2026-04-02
""",
    )
    _refresh_template_index(index, tpl)
    # created=2026-04-02 (q1 already past), as_of=2026-06-15 (before q2 trigger).
    # No anchor in window -> not_due.
    result = json.loads(recurring_materialize(as_of="2026-06-15"))
    assert result["created"] == []
    assert any(s.get("reason") == "not_due" for s in result["skipped"])


def test_absolute_no_last_run_future_anchor_also_not_due(recurring_vault):
    """Fresh absolute template + anchor still in future -> not_due (same path)."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "fixed.md",
        """id: yearly-fixed
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: fixed-12-31
instance_folder: tasks
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-30"))
    assert result["created"] == []
    assert any(s.get("reason") == "not_due" for s in result["skipped"])


def test_relative_interval_uses_done_instance(recurring_vault):
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "log.md",
        """id: weekly-log
type: recurring-template
active: true
recurrence_anchor_mode: relative
recurrence_interval: 7d
instance_folder: tasks
due_offset_days: 0
""",
    )
    _refresh_template_index(index, tpl)

    # Pre-existing done instance dated 2026-05-01
    done_path = vault / "tasks" / "recurring-weekly-log-2026-05-01.md"
    done_path.write_text(
        "---\n"
        "recurrence_template: weekly-log\n"
        "recurrence_period: 2026-05-01\n"
        "status: done\n"
        "closed: 2026-05-01\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    index.refresh_path(
        done_path.relative_to(vault).as_posix(), action="create"
    )

    result = json.loads(recurring_materialize(as_of="2026-05-15"))
    assert len(result["created"]) == 1
    created = result["created"][0]
    assert created["period"] == "2026-05-08"
    assert created["trigger_date"] == "2026-05-08"
    assert (vault / "tasks" / "recurring-weekly-log-2026-05-08.md").exists()


def test_idempotent_second_call_skips(recurring_vault):
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "monthly.md",
        """id: monthly-check
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
instance_folder: tasks
due_offset_days: 0
last_run: 2026-04-30
""",
    )
    _refresh_template_index(index, tpl)

    first = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert len(first["created"]) == 1

    second = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert second["created"] == []
    # Only one file on disk, no duplicates.
    instance_files = list((vault / "tasks").glob("recurring-monthly-check-2026-05*.md"))
    assert len(instance_files) == 1


def test_idempotent_when_last_run_stale(recurring_vault, monkeypatch):
    """If last_run is stale, the index-based already_exists check fires."""
    monkeypatch.setattr(recurring, "_update_template_last_run", lambda *a, **kw: None)
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "monthly.md",
        """id: monthly-check
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
instance_folder: tasks
last_run: 2026-04-30
""",
    )
    _refresh_template_index(index, tpl)

    first = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert len(first["created"]) == 1

    second = json.loads(recurring_materialize(as_of="2026-05-31"))
    assert second["created"] == []
    assert any(s.get("reason") == "already_exists" for s in second["skipped"])
    instance_files = list((vault / "tasks").glob("recurring-monthly-check-2026-05*.md"))
    assert len(instance_files) == 1


def test_catchup_all_creates_missed_periods(monkeypatch, recurring_vault):
    monkeypatch.setattr(config, "VAULT_RECURRING_CATCHUP_MODE", "all")
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "q.md",
        """id: q-report
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_3d
instance_folder: tasks
due_offset_days: 0
last_run: 2025-12-31
""",
    )
    _refresh_template_index(index, tpl)

    result = json.loads(recurring_materialize(as_of="2026-07-10"))
    periods = sorted(c["period"] for c in result["created"])
    assert periods == ["q1-2026", "q2-2026", "q4-2025"]


def test_catchup_next_collapses_to_latest(recurring_vault):
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "q.md",
        """id: q-report
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_3d
instance_folder: tasks
due_offset_days: 0
last_run: 2025-12-31
""",
    )
    _refresh_template_index(index, tpl)

    result = json.loads(recurring_materialize(as_of="2026-07-10"))
    periods = [c["period"] for c in result["created"]]
    assert periods == ["q2-2026"]


def test_dry_run_does_not_write(recurring_vault):
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "m.md",
        """id: monthly-check
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
instance_folder: tasks
last_run: 2026-04-30
""",
    )
    _refresh_template_index(index, tpl)

    result = json.loads(recurring_materialize(dry_run=True, as_of="2026-05-31"))
    assert result["dry_run"] is True
    assert len(result["created"]) == 1
    assert result["created"][0].get("dry_run") is True
    # No actual file written
    assert not (vault / "tasks" / "recurring-monthly-check-2026-05.md").exists()
    # last_run UNTOUCHED (still the fixture baseline, not advanced).
    raw = tpl.read_text(encoding="utf-8")
    assert "last_run: 2026-04-30" in raw
    assert "last_run: 2026-05" not in raw


def test_template_id_filter(recurring_vault):
    vault, index = recurring_vault
    tpl_a = _write_template(
        vault,
        "a.md",
        """id: alpha
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
instance_folder: tasks
last_run: 2026-04-30
""",
    )
    tpl_b = _write_template(
        vault,
        "b.md",
        """id: beta
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: month_end
instance_folder: tasks
last_run: 2026-04-30
""",
    )
    _refresh_template_index(index, tpl_a)
    _refresh_template_index(index, tpl_b)

    result = json.loads(recurring_materialize(template_id="alpha", as_of="2026-05-31"))
    assert result["checked"] == 1
    assert all(c["template_id"] == "alpha" for c in result["created"])
    assert not (vault / "tasks" / "recurring-beta-2026-05.md").exists()


def test_relative_no_baseline_bootstraps_with_today(recurring_vault):
    """Fresh relative template fires once with trigger=as_of (today)."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "x.md",
        """id: brand-new
type: recurring-template
active: true
recurrence_anchor_mode: relative
recurrence_interval: 7d
instance_folder: tasks
""",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-05-15"))
    assert len(result["created"]) == 1
    created = result["created"][0]
    assert created["period"] == "2026-05-15"
    assert created["trigger_date"] == "2026-05-15"
    assert (vault / "tasks" / "recurring-brand-new-2026-05-15.md").exists()


def test_relative_bootstrap_then_idempotent(recurring_vault):
    """Bootstrap fires once; second call same day -> already_exists."""
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "x.md",
        """id: brand-new
type: recurring-template
active: true
recurrence_anchor_mode: relative
recurrence_interval: 7d
instance_folder: tasks
""",
    )
    _refresh_template_index(index, tpl)
    first = json.loads(recurring_materialize(as_of="2026-05-15"))
    assert len(first["created"]) == 1
    second = json.loads(recurring_materialize(as_of="2026-05-15"))
    assert second["created"] == []
    files = list((vault / "tasks").glob("recurring-brand-new-2026-05-15.md"))
    assert len(files) == 1


def test_invalid_as_of(recurring_vault):
    result = json.loads(recurring_materialize(as_of="not-a-date"))
    assert result["error_code"] == "invalid_as_of"


# --------------------------------------------------------------------------
# v0.8.15: instances are schema-complete tasks (title / status / updated / body)
# --------------------------------------------------------------------------


def _read_instance(vault: Path, rel: str) -> tuple[str, str]:
    """Return (raw_text, body) of a materialized instance file."""
    raw = (vault / rel).read_text(encoding="utf-8")
    parts = raw.split("---\n", 2)
    body = parts[2] if len(parts) == 3 else ""
    return raw, body


# All templates below are absolute quarter_end_plus_3d with last_run 2026-06-30,
# so as_of=2026-07-04 fires exactly the q2-2026 period.
def _q_template(vault, name, extra_fm="", body="## Next Action (Template)\nBeleg prüfen.\n"):
    return _write_template(
        vault,
        name,
        "id: {id}\n".format(id=name[:-3])
        + "title: Quartals-Check\n"
        "type: recurring-template\n"
        "active: true\n"
        "recurrence_anchor_mode: absolute\n"
        "recurrence_anchor: quarter_end_plus_3d\n"
        "target_folder: tasks\n"
        "last_run: 2026-06-30\n"
        + extra_fm,
        body=body,
    )


def test_instance_carries_title_status_updated_and_body(recurring_vault):
    vault, index = recurring_vault
    tpl = _q_template(vault, "tucho.md", body="## Next Action (Template)\nBeleg prüfen und ablegen.\n")
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-07-04"))
    assert len(result["created"]) == 1
    raw, body = _read_instance(vault, result["created"][0]["path"])
    # exp 1/2/3: required frontmatter present
    assert "title: Quartals-Check" in raw
    assert "status: next" in raw
    assert "updated: '2026-07-04'" in raw or "updated: 2026-07-04" in raw
    # body opens with a blank line after the frontmatter, then the H1 title
    assert body.startswith("\n# Quartals-Check\n")
    # exp 5: body carries the template's Next Action section, plus Verlauf + Bezug
    assert "## Next Action" in body
    assert "Beleg prüfen und ablegen." in body
    assert "## Verlauf" in body
    assert "## Bezug" in body
    assert "[[tucho]]" in body


def test_updated_uses_as_of_not_template_date(recurring_vault):
    vault, index = recurring_vault
    tpl = _q_template(vault, "asof.md")
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-07-04"))
    raw, _ = _read_instance(vault, result["created"][0]["path"])
    # exp 3: updated is the generation date, not the template's last_run (2026-06-30)
    assert "2026-06-30" not in raw.split("---\n", 2)[1]  # not in frontmatter


def test_frontmatter_to_inherit_overrides_status(recurring_vault):
    vault, index = recurring_vault
    tpl = _q_template(vault, "wv.md", extra_fm="frontmatter_to_inherit:\n  status: wv\n")
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-07-04"))
    raw, _ = _read_instance(vault, result["created"][0]["path"])
    # exp 4: explicit inherited status wins over the default
    assert "status: wv" in raw
    assert "status: next" not in raw


def test_body_action_overrides_next_action_section(recurring_vault):
    vault, index = recurring_vault
    tpl = _q_template(
        vault,
        "ba.md",
        extra_fm="body_action: Konkret diese eine Sache tun.\n",
        body="## Next Action (Template)\nDieser Text darf NICHT erscheinen.\n",
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-07-04"))
    _, body = _read_instance(vault, result["created"][0]["path"])
    # exp 7: body_action wins over the template section
    assert "Konkret diese eine Sache tun." in body
    assert "NICHT erscheinen" not in body


def test_missing_next_action_section_uses_placeholder_and_warns(recurring_vault):
    vault, index = recurring_vault
    tpl = _q_template(vault, "nobody.md", body="")  # no ## Next Action (Template)
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-07-04"))
    _, body = _read_instance(vault, result["created"][0]["path"])
    # exp 6: placeholder (the title) plus a warning, never an empty body
    assert "## Next Action" in body
    assert "Quartals-Check" in body
    assert any("placeholder" in w.get("warning", "") for w in result["warnings"])


def test_template_without_title_errors_and_writes_nothing(recurring_vault):
    vault, index = recurring_vault
    tpl = _write_template(
        vault,
        "notitle.md",
        """id: notitle-quarterly
type: recurring-template
active: true
recurrence_anchor_mode: absolute
recurrence_anchor: quarter_end_plus_3d
target_folder: tasks
last_run: 2026-06-30
""",
        body="## Next Action (Template)\nEgal.\n",
        ensure_title=False,
    )
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(as_of="2026-07-04"))
    # exp 8: no title -> errors entry, nothing written
    assert result["created"] == []
    assert any(
        e.get("template_id") == "notitle-quarterly" and "title" in e.get("error", "")
        for e in result["errors"]
    )
    assert not (vault / "tasks" / "recurring-notitle-quarterly-q2-2026.md").exists()


def test_dry_run_returns_planned_frontmatter(recurring_vault):
    vault, index = recurring_vault
    tpl = _q_template(vault, "dry.md")
    _refresh_template_index(index, tpl)
    result = json.loads(recurring_materialize(dry_run=True, as_of="2026-07-04"))
    entry = result["created"][0]
    assert entry["dry_run"] is True
    planned = entry["planned_frontmatter"]
    assert planned["title"] == "Quartals-Check"
    assert planned["status"] == "next"
    assert planned["updated"] == "2026-07-04"
    # dry-run writes nothing
    assert not (vault / "tasks" / "recurring-dry-quarterly-q2-2026.md").exists()
