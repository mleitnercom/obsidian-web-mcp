"""Two production defects found while porting OCR to the extension package (24.09.).

1. PDFs that carry only an owner password (print or copy restrictions) open with the
   empty user password. pypdf keeps ``is_encrypted`` True after that succeeds, and
   read_file read that flag as failure: such PDFs were refused, text layer or not, and a
   scanned one never reached OCR. In the vault: a 26-page contract scan.

2. The OCR commands and ripgrep parse files anyone with vault write access can place
   there, and they ran with a copy of the server's environment, bearer token and OAuth
   secret included.

The PDFs here are real encrypted files written by pypdf, and the OCR command is a real
child process that reports the environment it was given.
"""

import io
import json
import os
import shutil
import subprocess
import sys

import pytest
from pypdf import PdfReader, PdfWriter

from obsidian_vault_mcp import config
from obsidian_vault_mcp.tools.search import vault_search
from obsidian_vault_mcp.vault import read_file

from .conftest import build_simple_pdf_bytes

SECRETS = {
    "VAULT_MCP_TOKEN": "leak-token",
    "VAULT_OAUTH_CLIENT_SECRET": "leak-oauth-secret",
    "VAULT_OAUTH_AUTH_PASSWORD": "leak-password",
    "AWS_SECRET_ACCESS_KEY": "leak-aws",
}
REQUIRE = os.environ.get("VAULT_TEST_REQUIRE_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}


def _encrypted(source: bytes | None, *, user: str, owner: str) -> bytes:
    writer = PdfWriter()
    if source is None:
        writer.add_blank_page(width=300, height=200)  # a scan: no text layer
    else:
        writer.append(PdfReader(io.BytesIO(source)))
    writer.encrypt(user_password=user, owner_password=owner)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def env_reporting_ocr(tmp_path, monkeypatch):
    """A real OCR command that prints the environment it received as its 'text'."""
    script = tmp_path / "report_env.py"
    script.write_text("import json, os\nprint(json.dumps(dict(os.environ)))\n", encoding="utf-8")
    command = f"{sys.executable} {script}"
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("VAULT_PDF_OCR_DPI", "150")

    monkeypatch.setattr(config, "VAULT_PDF_OCR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_CMD", command)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_TIMEOUT", 60)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_LANGUAGES", "deu+eng")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_CMD", command)
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_TIMEOUT", 60)
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_LANGUAGES", "deu+eng")
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_SIDECAR_ENABLED", True)
    return command


# --- 1. owner-password PDFs ---------------------------------------------------------------

def test_the_hazard_is_real_pypdf_keeps_the_flag_after_decrypting(vault_dir):
    """Without this quirk the old check would have been right."""
    reader = PdfReader(io.BytesIO(_encrypted(build_simple_pdf_bytes("x"), user="", owner="restrict")))
    assert reader.decrypt("") != 0
    assert reader.is_encrypted is True


def test_owner_password_pdf_with_text_is_read(vault_dir):
    (vault_dir / "restricted.pdf").write_bytes(
        _encrypted(build_simple_pdf_bytes("Vertragslaufzeit 36 Monate"), user="", owner="restrict")
    )

    content, metadata = read_file("restricted.pdf", extract_binary=True)

    assert "36 Monate" in content
    assert metadata["extractable_text"] is True


def test_owner_password_scan_reaches_ocr(vault_dir, env_reporting_ocr):
    (vault_dir / "scan.pdf").write_bytes(_encrypted(None, user="", owner="restrict"))

    content, metadata = read_file("scan.pdf", extract_binary=True)

    assert metadata["ocr"]["applied"] is True
    assert json.loads(content)["VAULT_PDF_PATH"].endswith("scan.pdf")


def test_a_user_password_is_still_refused(vault_dir, env_reporting_ocr):
    (vault_dir / "locked.pdf").write_bytes(_encrypted(build_simple_pdf_bytes("secret"), user="open-me", owner="o"))

    with pytest.raises(ValueError, match="Encrypted PDF"):
        read_file("locked.pdf", extract_binary=True)
    assert not (vault_dir / "locked.pdf.ocr.txt").exists()


# --- 2. the environment of content parsers ------------------------------------------------

def _assert_no_secrets(env: dict) -> None:
    leaked = {name for name in SECRETS if name in env} | {
        name for name, value in env.items() if value in SECRETS.values()
    }
    assert not leaked, f"secrets reached the child process: {sorted(leaked)}"


def test_pdf_ocr_gets_no_secrets_but_what_it_needs(vault_dir, env_reporting_ocr):
    (vault_dir / "scan.pdf").write_bytes(_encrypted(None, user="", owner="restrict"))

    env = json.loads(read_file("scan.pdf", extract_binary=True)[0])

    _assert_no_secrets(env)
    assert env["PATH"] == os.environ["PATH"]
    assert env["VAULT_PDF_OCR_LANGUAGES"] == "deu+eng"
    assert env["VAULT_PDF_OCR_DPI"] == "150"  # the prod wrapper reads its tuning from here


def test_image_ocr_gets_no_secrets(vault_dir, env_reporting_ocr):
    from .test_image_ocr import png_bytes

    (vault_dir / "shot.png").write_bytes(png_bytes(1280, 720))

    env = json.loads(read_file("shot.png", extract_binary=True)[0])

    _assert_no_secrets(env)
    assert env["VAULT_IMAGE_PATH"].endswith("shot.png")


def test_ripgrep_does_not_inherit_the_server_environment(vault_dir, monkeypatch, tmp_path):
    """Real rg: a ripgrep config reachable through the server's environment must not
    steer the search. Before the fix RIPGREP_CONFIG_PATH was inherited, so a config
    skipping every file emptied the result."""
    if shutil.which("rg") is None:
        if REQUIRE:
            pytest.fail("rg missing under VAULT_TEST_REQUIRE_TOOLS=1")
        pytest.skip("needs ripgrep; unproven here, proven on the server run")
    (vault_dir / "rg-probe.md").write_text("Zaehlerstand im Keller\n", encoding="utf-8")
    rc = tmp_path / "ripgreprc"
    rc.write_text("--max-filesize=1\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(rc))
    # Control: the config does take effect on an rg that inherits the environment.
    inherited = subprocess.run(["rg", "-l", "-e", "Zaehlerstand", "--", str(vault_dir)], capture_output=True, text=True)
    assert inherited.returncode == 1, "control failed: rg ignored RIPGREP_CONFIG_PATH"

    result = json.loads(vault_search("Zaehlerstand", file_pattern="*.md"))

    assert any(r["path"] == "rg-probe.md" for r in result["results"]), result
