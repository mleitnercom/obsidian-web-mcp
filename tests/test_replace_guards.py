"""An empty old_str must never corrupt a file via replace_all (issue #13).

str.count("") returns len+1 (never 0, so the not-found guard misses it) and
str.replace("", new) interleaves new between every character. Both
vault_str_replace and vault_batch_replace route through _replace_in_content,
which now rejects an empty old_str before counting.
"""

import json

from obsidian_vault_mcp.tools.write import vault_str_replace, vault_batch_replace

ORIGINAL = "# Title\n\nalpha beta gamma\n"


def test_str_replace_rejects_empty_old_str(vault_dir):
    note = vault_dir / "note.md"
    note.write_text(ORIGINAL, encoding="utf-8")

    result = json.loads(vault_str_replace("note.md", old_str="", new_str="X", replace_all=True))

    assert "error" in result
    assert note.read_text(encoding="utf-8") == ORIGINAL  # file untouched


def test_batch_replace_missing_old_str_does_not_corrupt(vault_dir):
    note = vault_dir / "note.md"
    note.write_text(ORIGINAL, encoding="utf-8")

    # omitting old_str defaults it to "" in vault_batch_replace
    json.loads(vault_batch_replace([{"path": "note.md", "new_str": "X", "replace_all": True}]))

    assert note.read_text(encoding="utf-8") == ORIGINAL
