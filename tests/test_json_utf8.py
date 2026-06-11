"""Tool responses must emit non-ASCII as UTF-8, not \\uXXXX escapes (issue #49).

ensure_ascii=True (json.dumps default) escapes every non-ASCII character, which
inflates token counts for non-ASCII vaults and breaks verbatim round-trips of
non-ASCII paths. vault_json_dumps defaults ensure_ascii=False to avoid that.
"""

import datetime

from obsidian_vault_mcp.vault import vault_json_dumps


def test_non_ascii_emitted_as_utf8_not_escaped():
    payload = {"title": "Erklärung", "path": "70_Privat/Müll/Größe.md"}
    out = vault_json_dumps(payload)
    assert "Erklärung" in out
    assert "Größe" in out
    assert "\\u" not in out  # no escape sequences


def test_ensure_ascii_override_still_supported():
    out = vault_json_dumps({"x": "ä"}, ensure_ascii=True)
    assert "\\u00e4" in out


def test_date_encoder_still_applied():
    out = vault_json_dumps({"d": datetime.date(2026, 6, 11)})
    assert "2026-06-11" in out
