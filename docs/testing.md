# Testing

The rules below are not general advice. Each one exists because it was broken here, and
the break cost something: a merged-then-reverted upstream PR, a security hole reported as
closed while it was open, a test that measured nothing for two weeks.

The short form lives in the operator's global working rules. This file carries the
concrete mechanics for this repository.

## The one failure mode

**Every incident so far was the same: the test exercised a substitute rather than the
thing that runs in production.**

| Date | What the test exercised | What production runs | Cost |
|---|---|---|---|
| upstream #64 | a hand-built Starlette app | `build_app()` | missing `config` import never executed; PR closed |
| 2026-08-29 | an OCR stub that also answered ripgrep | real ripgrep | green locally, red on the server |
| 2026-09-02 | a call that returned at the rate limiter | the slow path | the test measured nothing |
| 2026-09-02 | the hardlink guard on the Python backend | the ripgrep backend | leak stayed open, was reported as closed |
| 2026-09-16 | a synthetic PNG with a broken CRC | a real image | OCR aborted early; green for the wrong reason |

Upstream hit the identical trap from the other side: on the maintainer's machine `rg` is a
shell function, so `shutil.which("rg")` returns `None`, every ripgrep-dependent test skips,
and a suite reporting `1 skipped` looks exactly like a pass. That is why the hardlink case
read as closed for three months on both sides.

## Rules

### 1. Test through the real composition root

HTTP behaviour goes through `build_app()` with `TestClient`, never through a
`Starlette(routes=[...])` assembled in the test. A route that is only asserted to be
*registered* is not tested; send a request.

Tool behaviour goes through the registered tool in `server.py` when the wrapper matters
(rate limiting, audit, the worker-thread offload). Use `call_registered_tool()` from
`tests/conftest.py` for the tools that are coroutines.

### 2. Negative control first

A test for a guard must first prove the exposure exists without the guard, then prove the
guard removes it. A test that only asserts "no secret in the output" passes just as well
when it never reached the code under test.

For the hardlink tests this means: assert that real ripgrep emits the canary, *then*
assert the guard suppresses it.

### 3. Backend matrix, not a representative

Behaviour that depends on an installed binary is tested in both states. Forcing one
backend with `monkeypatch.setattr(search_mod.shutil, "which", lambda name: None)` tests
that backend only, and production has the other one.

| Capability | Binary | Absent on |
|---|---|---|
| `vault_search` content pass | `rg` | the Windows dev machine |
| image OCR in `vault_read` | `tesseract` | the Windows dev machine |
| PDF OCR in `vault_read` | `pdftoppm` (poppler) | the Windows dev machine |

### 4. Skipped is not passed

A security-relevant test that skips for want of a binary is unproven, not green. On the
server, set `VAULT_TEST_REQUIRE_TOOLS=1`; `tests/test_environment_assumptions.py` then
fails loudly instead of letting the suite report a comfortable `skipped`.

### 5. "Closed" requires the target environment

Local green is a draft. Before a change is called done, before a PR, and before a release
note says "fixed":

```bash
ssh obsidian-mcp.mus.lan
cd /home/michael/obsidian-web-mcp-fork
sudo -u michael env VAULT_TEST_REQUIRE_TOOLS=1 venv/bin/python -m pytest -q
```

That host has `rg`, `tesseract` and `poppler-utils`. The dev machine does not.

### 6. Real artifacts as fixtures

When the subject is a processing pipeline, the fixture comes from reality: copy a real PNG
or PDF into a temp vault. A hand-rolled minimal file tends to fail validation upstream of
the code under test, and the test then passes without reaching it.

If a synthetic fixture is unavoidable, assert the precondition (the OCR command produced
text, ripgrep emitted a match) before asserting the conclusion.

### 7. Path inventory for security fixes

Closing a class of bug means enumerating every path that reaches the sink and proving each
one, not fixing the paths the report happened to mention. v0.10.0 closed three of four read
paths because it followed the report; the fourth was the one production uses.

Read paths that can return file-derived data today:

- `vault.read_file` (also the read half of `vault_edit`, `vault_append`,
  `vault_batch_frontmatter_update`, `vault_write(merge_frontmatter=True)`)
- `_search_ripgrep`
- `_search_python`
- `_search_filenames`
- `_get_frontmatter_excerpt`
- the frontmatter index
- the semantic index
- canvas and analytics readers

A guard that does not cover all of them is not a guard.
