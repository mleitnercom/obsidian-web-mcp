"""frontmatter_io hardening (upstream #42 backport).

Covers strict line-based fence detection, fail-closed parsing (malformed or
unterminated frontmatter raises instead of being silently dropped), and BOM
stripping. A round-trip check guards the formatting-preservation contract.
"""

import pytest

from obsidian_vault_mcp import frontmatter_io
from obsidian_vault_mcp.frontmatter_io import YAMLError


def test_parses_frontmatter_and_body():
    content = "---\ntitle: Hi\nstatus: active\n---\n\nBody.\n"
    meta, body = frontmatter_io.loads(content)
    assert meta["title"] == "Hi"
    assert meta["status"] == "active"
    assert body == "\nBody.\n"


def test_no_frontmatter_returns_empty_meta_and_full_body():
    content = "Just a body, no fence here.\n"
    meta, body = frontmatter_io.loads(content)
    assert meta == {}
    assert body == content


def test_empty_frontmatter_block_returns_empty_meta():
    content = "---\n---\n\nBody.\n"
    meta, body = frontmatter_io.loads(content)
    assert meta == {}
    assert body == "\nBody.\n"


def test_dumps_roundtrips_through_loads():
    content = "---\ntitle: Hello World\nstatus: active\ntags:\n  - a\n  - b\n---\n\nBody paragraph.\n"
    meta, body = frontmatter_io.loads(content)
    out = frontmatter_io.dumps(meta, body)
    meta2, body2 = frontmatter_io.loads(out)
    assert dict(meta2) == dict(meta)
    assert body2 == body


def test_dumps_without_metadata_returns_body_unchanged():
    assert frontmatter_io.dumps({}, "plain body\n") == "plain body\n"


# --- strict fence detection -------------------------------------------------

def test_four_dash_thematic_break_is_not_a_fence():
    """A body opening with a Markdown thematic break must not read as frontmatter."""
    content = "----\nnot frontmatter\n----\n"
    meta, body = frontmatter_io.loads(content)
    assert meta == {}
    assert body == content


def test_indented_dashes_are_not_a_fence():
    content = "  ---\ntitle: x\n  ---\nbody\n"
    meta, body = frontmatter_io.loads(content)
    assert meta == {}
    assert body == content


def test_dashes_with_trailing_text_are_not_a_fence():
    content = "--- foo\ntitle: x\n---\nbody\n"
    meta, body = frontmatter_io.loads(content)
    assert meta == {}
    assert body == content


def test_fence_with_trailing_whitespace_is_recognized():
    content = "---  \ntitle: x\n---  \n\nbody\n"
    meta, body = frontmatter_io.loads(content)
    assert meta["title"] == "x"
    assert body == "\nbody\n"


# --- fail closed ------------------------------------------------------------

def test_unterminated_frontmatter_raises():
    """Opening '---' with no closing fence must raise, not silently swallow the body."""
    content = "---\ntitle: Hi\nstill going, no closing fence\n"
    with pytest.raises(YAMLError):
        frontmatter_io.loads(content)


def test_malformed_yaml_raises():
    """Broken YAML inside the fences must raise so a merge caller can decide."""
    content = "---\ntags: [a, b, c\nfoo: bar\n---\n\nbody\n"
    with pytest.raises(YAMLError):
        frontmatter_io.loads(content)


# --- BOM ---------------------------------------------------------------------

def test_leading_bom_is_stripped_before_fence_detection():
    content = chr(0xFEFF) + "---\ntitle: Hi\n---\n\nBody.\n"
    meta, body = frontmatter_io.loads(content)
    assert meta["title"] == "Hi"
    assert body == "\nBody.\n"
