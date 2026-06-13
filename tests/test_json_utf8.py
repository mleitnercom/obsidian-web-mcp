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


def test_lone_surrogate_filename_stays_utf8_encodable():
    """A non-UTF-8 filename (lone surrogate) must not break the whole response (#14).

    os.listdir decodes invalid bytes via surrogateescape, so a Latin-1 'café.md'
    arrives as 'bad\\udce9name.md'. ensure_ascii=False would emit it verbatim and
    the MCP transport's .encode('utf-8') would then fail for the entire payload.
    The guard falls back to escaped output for that one response.
    """
    out = vault_json_dumps({"path": "bad\udce9name.md", "ok": "Größe"})
    # transport requirement: the whole response must be UTF-8 encodable
    out.encode("utf-8")  # would raise UnicodeEncodeError without the guard
    # and the value still round-trips
    import json
    assert json.loads(out)["path"] == "bad\udce9name.md"
