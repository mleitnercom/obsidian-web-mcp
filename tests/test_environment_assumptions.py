"""Make a skipped security test impossible to mistake for a passing one.

Several capabilities here depend on an external binary. Where the binary is absent the
tests covering those capabilities skip, and a suite reporting "N skipped" reads exactly
like a pass. That is not hypothetical: upstream traced three months of a security hole
believed closed to precisely this, and this repository did the same thing twice.

The dev machine legitimately has none of these binaries, so the check is opt-in.
VAULT_TEST_REQUIRE_TOOLS=1 declares "this run is expected to prove the real behaviour",
and the suite then fails loudly instead of skipping quietly. The deploy runbook sets it
for the server run (see docs/testing.md).
"""

import os
import shutil

import pytest

# binary -> what goes unproven without it
REQUIRED_TOOLS = {
    "rg": "the ripgrep search backend, which is the default whenever rg is installed, "
          "including its hardlink guard",
    "tesseract": "image OCR in vault_read, and the PDF OCR wrapper",
    "pdftoppm": "PDF rendering for the OCR wrapper (poppler-utils)",
}

REQUIRE = os.environ.get("VAULT_TEST_REQUIRE_TOOLS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@pytest.mark.parametrize("tool,covers", sorted(REQUIRED_TOOLS.items()))
def test_required_tool_is_present(tool, covers):
    """With VAULT_TEST_REQUIRE_TOOLS=1 a missing binary fails the run.

    Both messages name the capability that goes unproven, so whoever reads the output
    learns what a green suite would otherwise have been hiding.
    """
    if not REQUIRE:
        pytest.skip(
            f"VAULT_TEST_REQUIRE_TOOLS not set, so {tool} is not required here. "
            f"This run does not prove: {covers}"
        )

    assert shutil.which(tool) is not None, (
        f"{tool} is missing, so this run does not prove: {covers}. "
        "Install it, or do not count this run as verification."
    )
