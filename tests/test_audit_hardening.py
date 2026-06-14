"""Audit hardening backported from upstream review jimprosser/obsidian-web-mcp#56:

  #2 reject an audit log that resolves inside the vault (it would be tamperable),
  #3 batch mutations emit one record per file with correct per-file status + snapshots,
  #4 the BearerAuthMiddleware -> sync tool principal propagation is exercised over HTTP,
  and a dry-run edit is no longer recorded as a mutation.
"""

import json

import pytest

from obsidian_vault_mcp import audit, config, server
from obsidian_vault_mcp.rate_limit import (
    current_auth_principal,
    reset_current_auth_principal,
    reset_current_request_metadata,
    set_current_auth_principal,
    set_current_request_metadata,
)

PRINCIPAL = "audit-hardening-token"


@pytest.fixture
def audit_log(vault_dir, tmp_path, monkeypatch):
    log_path = tmp_path / "audit" / "log.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(log_path))
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_INCLUDE_READS", False)
    p = set_current_auth_principal(PRINCIPAL)
    m = set_current_request_metadata({"client_family": "pytest", "request_id": "req-1"})
    yield log_path
    reset_current_request_metadata(m)
    reset_current_auth_principal(p)


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- #2: in-vault audit log rejected ---

def test_audit_path_inside_vault_detected(vault_dir, monkeypatch):
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(vault_dir / "audit.jsonl"))
    assert audit.audit_path_inside_vault() is True
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(vault_dir / "subfolder" / "a.jsonl"))
    assert audit.audit_path_inside_vault() is True


def test_audit_path_outside_vault_ok(vault_dir, tmp_path, monkeypatch):
    # tmp_path is the vault's parent, so a sibling dir is outside the vault.
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(tmp_path / "outside" / "a.jsonl"))
    assert audit.audit_path_inside_vault() is False


# --- #3: batch mutations record per-file status, not one thin record ---

def test_batch_frontmatter_one_record_per_file(audit_log):
    server.vault_write("a.md", "---\nx: 1\n---\nbody")
    updates = [
        {"path": "a.md", "fields": {"status": "done"}},
        {"path": "missing.md", "fields": {"status": "done"}},
    ]
    server.vault_batch_frontmatter_update(updates)
    recs = [r for r in _records(audit_log) if r["operation"] == "vault_batch_frontmatter_update"]
    assert len(recs) == 2                       # one per file, not one for the call
    by_path = {r["target_path"]: r for r in recs}
    assert by_path["a.md"]["operation_status"] == "success"
    assert by_path["a.md"]["checksum_after"] is not None    # real snapshot, not null
    assert by_path["missing.md"]["operation_status"] == "error"   # partial failure surfaced
    assert by_path["missing.md"]["error"]


def test_batch_replace_one_record_per_file(audit_log):
    server.vault_write("r.md", "hello world")
    updates = [
        {"path": "r.md", "old_str": "hello", "new_str": "hi"},
        {"path": "gone.md", "old_str": "x", "new_str": "y"},
    ]
    server.vault_batch_replace(updates)
    recs = [r for r in _records(audit_log) if r["operation"] == "vault_batch_replace"]
    assert len(recs) == 2
    by_path = {r["target_path"]: r for r in recs}
    assert by_path["r.md"]["operation_status"] == "success"
    assert by_path["gone.md"]["operation_status"] == "error"


# --- dry-run edit is not recorded as a mutation ---

def test_dry_run_edit_not_audited(audit_log):
    server.vault_write("e.md", "alpha beta")
    before = len(_records(audit_log))
    server.vault_edit("e.md", [{"old_text": "alpha", "new_text": "ALPHA"}], dry_run=True)
    assert len(_records(audit_log)) == before          # dry run logged nothing
    server.vault_edit("e.md", [{"old_text": "alpha", "new_text": "ALPHA"}])
    assert len(_records(audit_log)) == before + 1       # the real edit IS audited


# --- #4: principal reaches the sync tool layer through the real middleware ---

def test_principal_propagates_to_sync_handler(monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from obsidian_vault_mcp import auth as auth_module

    monkeypatch.setattr(auth_module, "VAULT_MCP_TOKEN", "secret-token")

    def sync_probe(request):  # sync on purpose: mirrors how FastMCP runs vault_* tools
        return JSONResponse({"principal": current_auth_principal()})

    app = Starlette(routes=[Route("/probe", sync_probe)])
    app.add_middleware(auth_module.BearerAuthMiddleware)
    client = TestClient(app)

    r = client.get("/probe", headers={"Authorization": "Bearer secret-token", "User-Agent": "p/1"})
    assert r.status_code == 200, r.text
    assert r.json()["principal"] == "secret-token"   # null here => token_id_hash null in prod
