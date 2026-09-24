"""The environment for programs that parse vault content.

OCR commands (pdftoppm, tesseract, a wrapper script) and ripgrep read files that anyone
with write access to the vault can place there. A parser bug in one of them must not
hand over the server's credentials, so they get an environment built from an allowlist
instead of a copy of the server's own: what a program needs to start and find its
data, the OCR tuning variables, and nothing that authenticates (VAULT_MCP_TOKEN, the
OAuth secret and password, the signing key).

The post-write hook is deliberately not covered: it is the operator's own automation,
parses no vault content, and may legitimately need the operator's environment (a git
commit needs SSH_AUTH_SOCK, for example).
"""

import os

# What a program needs to start and find its data. Nothing that authenticates.
_PASSTHROUGH = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TESSDATA_PREFIX", "OMP_THREAD_LIMIT",
)


def content_parser_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The whole environment for a program that parses vault content."""
    env = {name: os.environ[name] for name in _PASSTHROUGH if name in os.environ}
    # OCR tuning for the wrapper scripts (pages, DPI, languages, the command itself).
    # None of these is a secret.
    env.update({k: v for k, v in os.environ.items() if k.startswith("VAULT_") and "OCR" in k})
    if extra:
        env.update(extra)
    return env
