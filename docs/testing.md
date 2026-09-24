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

### 8. Test cases come from the surface, not from the diff

A test written from the implementation only covers what the code already knows about. For
anything reachable from outside, enumerate the inputs before writing a line: methods, path
shapes including percent-encoding and a trailing slash, query keys encoded
(`%73ignature=`), headers missing or hostile, non-ASCII values, bodies with and without a
declared length.

Two examples from the reviews of upstream #80, both found by the maintainer and not here:

- The upload namespace was guarded with three probe paths, so a literal
  `Route("/upload/export")` passed the guard while the exemption regex matched it. The
  test only tried parameterized routes, the shape the implementation handled.
- `hmac.compare_digest` raises `TypeError` on a non-ASCII `str`, so `signature=%C3%A9`
  answered 500. Every test used a well-formed hex signature.

### 9. Attack your own surface once, by hand

Before submitting a new externally reachable path, spend a session probing it yourself:
wrong method, wrong content type, the id that does not exist, the directory swapped for a
symlink after the grant, the same URL twice. The maintainer did exactly this on #80 and
found three issues the green suite did not.

### 10. Someone else's repository, someone else's rules

Read `CONTRIBUTING.md` before choosing defaults, and look for the precedent in the target
project for every new setting. Upstream validates the audit log path at startup and
requires new security-relevant features to be off until enabled; #80 shipped with a
feature on by default and settings that silently fell back to their defaults, because it
carried the fork's habits instead of the project's rules.

### 11. Test tooling is code with its own risk

`tests/_live_server.py` started a real server from `**os.environ`. A developer with
`VAULT_AUDIT_LOG_PATH` exported got test writes in their real audit log,
`VAULT_MCP_HEARTBEAT_URL` pinged their real monitor, and `VAULT_MCP_HOST=0.0.0.0`
published a server with a token from this repository. Helpers build their environment
from scratch, point `HOME` into `tmp_path`, and never depend on the developer's machine.

### 12. The client is a model

Asking "can this code corrupt data" is not enough; ask what a model does next. `vault_read`
returning OCR text with nothing marking it as extracted is safe in the write path and still
dangerous: a model that gets the safe error from `vault_edit` falls back to `vault_write`
and replaces the PDF with the text it was just shown. Hence `metadata["extracted"]`.

### 13. A claim in prose needs the same proof as a line of code

The #80 description said `/upload/x/y` was rejected. No test covered it, and the guard did
not reject it. If there is no test, the claim comes out of the text.

### 14. A public function that takes a function: test the function's shapes

`run_audited(operation, func)` was made public for extensions (upstream #93). The names
passed to it were attacked by hand; the shape of `func` was not. The maintainer found that
an `async` function returns before its body runs, so the audit record said "success" for a
call that raised afterwards. Six fork tools have been coroutines since v0.10.0, so this was
foreseeable. When a public function takes a callable, the input matrix includes a
synchronous one, an async one, one that raises, one that returns the wrong type, and every
path the wrapper has (passthrough, single call, batch).

### 15. A negative control that passes is a finding

The partial-OCR wrapper test passed against the old wrapper too, which could not do what
the test claimed. The assertion only checked that the OCR text appeared somewhere; the old
wrapper put the whole document on page 2 and the text still appeared. Assert where the
text is, not only that it is there, and treat a control that does not fail as a bug in the
test.

### 16. Between processes, match by label, not by position

The first partial-OCR contract matched output blocks to pages by position and relied on
tesseract's form feeds. tesseract 5.3 prints none, and a wrapper that ignores the page list
prints one stream; by position that stream is a perfect match for one missing page. The
contract now labels every block with its page number and rejects anything unlabelled,
unrequested or duplicated. Position-based matching fails silently exactly when the other
side answers differently than assumed.
