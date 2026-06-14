import json
import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)


def _env_alias_raw(canonical: str, alias: str) -> str:
    """Return the canonical env var; fall back to a deprecated alias.

    Convergence toward upstream's ``VAULT_MCP_*`` server/transport names while
    keeping the fork's older names working. The canonical name wins; the legacy
    alias is honored with a one-line deprecation warning so existing
    deployments (systemd units, .env files) do not break.
    """
    value = os.environ.get(canonical, "")
    if value.strip():
        return value
    legacy = os.environ.get(alias, "")
    if legacy.strip():
        _logger.warning("Env var %s is deprecated; use %s instead.", alias, canonical)
        return legacy
    return ""


def _env_csv_with_alias(canonical: str, alias: str, default: list[str]) -> list[str]:
    """Comma-separated env var with canonical/deprecated-alias resolution."""
    raw = _env_alias_raw(canonical, alias)
    if not raw.strip():
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable with a safe fallback."""
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    """Parse a lowercased string environment variable constrained to allowed values."""
    value = os.environ.get(name, default).strip().lower()
    if value in allowed:
        return value
    return default


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var with a conservative default."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: list[str]) -> list[str]:
    """Parse a comma-separated env var into a list of non-empty trimmed values."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_binary_media_type_map(name: str) -> dict[str, set[str]]:
    """Parse a JSON object mapping MIME types to allowed file extensions."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be a JSON object mapping media types to extension lists")

    normalized: dict[str, set[str]] = {}
    for media_type, extensions in payload.items():
        if not isinstance(media_type, str) or not media_type.strip():
            raise ValueError(f"{name} contains an invalid media type key")
        if not isinstance(extensions, list) or not extensions:
            raise ValueError(f"{name} entry for {media_type!r} must be a non-empty list of extensions")
        cleaned: set[str] = set()
        for extension in extensions:
            if not isinstance(extension, str) or not extension.strip():
                raise ValueError(f"{name} entry for {media_type!r} contains an invalid extension")
            normalized_extension = extension.strip().lower()
            if not normalized_extension.startswith(".") or normalized_extension == ".":
                raise ValueError(f"{name} extension {extension!r} must start with '.'")
            cleaned.add(normalized_extension)
        normalized[media_type.strip().lower()] = cleaned
    return normalized

# Vault configuration
VAULT_PATH = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/Obsidian/MyVault")))
VAULT_MCP_TOKEN = os.environ.get("VAULT_MCP_TOKEN", "")
VAULT_MCP_PORT = _env_int("VAULT_MCP_PORT", 8420)
VAULT_MCP_HEARTBEAT_URL = os.environ.get("VAULT_MCP_HEARTBEAT_URL", "").strip()
VAULT_MCP_HEARTBEAT_INTERVAL = _env_int("VAULT_MCP_HEARTBEAT_INTERVAL", 60)
VAULT_HEALTH_ALLOW_REMOTE_DETAILS = _env_bool("VAULT_HEALTH_ALLOW_REMOTE_DETAILS", False)


def validate_heartbeat() -> int | None:
    """Validate the heartbeat config; return the interval (seconds) when enabled.

    Returns None when the heartbeat is disabled (no URL). Raises ValueError (so
    server.main() can exit non-zero and fail CLOSED) when the URL scheme is not http(s)
    or the interval is not a positive integer -- a typo must not boot a server that
    silently never pings or tight-loops on interval 0. Error messages never echo the raw
    values: the heartbeat URL is a capability URL (the secret is in the path), and a
    misconfigured operator might swap the URL/interval env vars. (Hardening from
    upstream #45.)
    """
    url = VAULT_MCP_HEARTBEAT_URL
    if not url:
        return None

    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(url)
        _ = parsed.port  # raises ValueError on a malformed port
    except ValueError:
        raise ValueError("VAULT_MCP_HEARTBEAT_URL has a malformed port")
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("VAULT_MCP_HEARTBEAT_URL must be an http(s) URL with a host")
    if VAULT_MCP_HEARTBEAT_INTERVAL <= 0:
        raise ValueError("VAULT_MCP_HEARTBEAT_INTERVAL must be a positive integer")
    return VAULT_MCP_HEARTBEAT_INTERVAL
VAULT_MCP_POST_WRITE_CMD = os.environ.get("VAULT_MCP_POST_WRITE_CMD", "").strip()
VAULT_MCP_POST_WRITE_TIMEOUT = _env_int("VAULT_MCP_POST_WRITE_TIMEOUT", 30)
VAULT_PDF_OCR_ENABLED = _env_bool("VAULT_PDF_OCR_ENABLED", False)
VAULT_PDF_OCR_CMD = os.environ.get("VAULT_PDF_OCR_CMD", "").strip()
VAULT_PDF_OCR_TIMEOUT = _env_int("VAULT_PDF_OCR_TIMEOUT", 120)
VAULT_PDF_OCR_LANGUAGES = os.environ.get("VAULT_PDF_OCR_LANGUAGES", "deu+eng").strip()
VAULT_PDF_OCR_SIDECAR_ENABLED = _env_bool("VAULT_PDF_OCR_SIDECAR_ENABLED", VAULT_PDF_OCR_ENABLED)
VAULT_PDF_OCR_SIDECAR_SUFFIX = os.environ.get("VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt").strip() or ".ocr.txt"

# OAuth 2.0 client credentials (for Claude app integration)
VAULT_OAUTH_CLIENT_ID = os.environ.get("VAULT_OAUTH_CLIENT_ID", "vault-mcp-client")
VAULT_OAUTH_CLIENT_SECRET = os.environ.get("VAULT_OAUTH_CLIENT_SECRET", "")
VAULT_OAUTH_AUTH_USERNAME = os.environ.get("VAULT_OAUTH_AUTH_USERNAME", "")
VAULT_OAUTH_AUTH_PASSWORD = os.environ.get("VAULT_OAUTH_AUTH_PASSWORD", "")
VAULT_OAUTH_SESSION_SECRET = os.environ.get("VAULT_OAUTH_SESSION_SECRET", "")
VAULT_OAUTH_REQUIRE_APPROVAL = _env_bool("VAULT_OAUTH_REQUIRE_APPROVAL", True)
# Fail closed by default: when no login credentials are configured the
# /oauth/authorize endpoint refuses to issue authorization codes. Set this to
# true only for local development/testing where unauthenticated auto-approval
# is acceptable.
VAULT_OAUTH_ALLOW_NO_AUTH = _env_bool("VAULT_OAUTH_ALLOW_NO_AUTH", False)
VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS = _env_bool("VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", True)
# Server/transport config converged to upstream's VAULT_MCP_* names; the older
# fork names remain as deprecated aliases (see _env_alias_raw).
VAULT_PUBLIC_BASE_URL = _env_alias_raw("VAULT_MCP_PUBLIC_URL", "VAULT_PUBLIC_BASE_URL").strip().rstrip("/")
TRUSTED_PROXY_IPS = _env_alias_raw("VAULT_MCP_FORWARDED_ALLOW_IPS", "VAULT_TRUSTED_PROXY_IPS") or "127.0.0.1,::1"
# Loopback is ALWAYS allowed; operator hosts from VAULT_MCP_ALLOWED_HOSTS are
# APPENDED, never replace it. This avoids a lockout footgun where setting the env
# var without re-listing loopback would drop it. (Matches upstream #34.)
ALLOWED_HOSTS_LOOPBACK_DEFAULTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
# Operator-supplied extra hostnames only (default empty); composed in
# effective_allowed_hosts(). Deprecated alias VAULT_ALLOWED_HOSTS still honored.
ALLOWED_HOSTS = _env_csv_with_alias("VAULT_MCP_ALLOWED_HOSTS", "VAULT_ALLOWED_HOSTS", [])


def effective_allowed_hosts() -> list[str]:
    """Loopback defaults plus operator hosts, de-duplicated, order preserved.

    Loopback can never be dropped, so an operator who sets only their tunnel
    hostname does not lock themselves out.
    """
    merged = [*ALLOWED_HOSTS_LOOPBACK_DEFAULTS, *ALLOWED_HOSTS]
    seen: set[str] = set()
    result: list[str] = []
    for host in merged:
        if host not in seen:
            seen.add(host)
            result.append(host)
    return result
# Bind host. Default 0.0.0.0 preserves the fork's behavior (cloudflared runs on
# a separate VM and reaches the server over the LAN); upstream defaults to
# loopback. Matching the VAULT_MCP_HOST name keeps the knob aligned.
VAULT_MCP_HOST = os.environ.get("VAULT_MCP_HOST", "0.0.0.0").strip() or "0.0.0.0"
INCLUDED_ROOTS = _env_csv("VAULT_INCLUDED_ROOTS", ["."])
EXCLUDED_PATH_PREFIXES = _env_csv("VAULT_EXCLUDED_PATH_PREFIXES", [])
EXTRA_BINARY_MEDIA_TYPES = _env_binary_media_type_map("VAULT_EXTRA_BINARY_MEDIA_TYPES_JSON")
IMPORT_FILE_ALLOWED_ROOTS = _env_csv("VAULT_IMPORT_FILE_ALLOWED_ROOTS", [])
VAULT_UPLOAD_URL_SECRET = os.environ.get("VAULT_UPLOAD_URL_SECRET", "").strip()
VAULT_UPLOAD_URL_TTL_SECONDS = _env_int("VAULT_UPLOAD_URL_TTL_SECONDS", 15 * 60)
VAULT_UPLOAD_URL_MAX_TTL_SECONDS = _env_int("VAULT_UPLOAD_URL_MAX_TTL_SECONDS", 60 * 60)
VAULT_DAILY_NOTES_FOLDER = os.environ.get("VAULT_DAILY_NOTES_FOLDER", "").strip().strip("/\\")
VAULT_DAILY_NOTES_FORMAT = os.environ.get("VAULT_DAILY_NOTES_FORMAT", "%Y-%m-%d").strip() or "%Y-%m-%d"
VAULT_DAILY_NOTES_TEMPLATE = os.environ.get("VAULT_DAILY_NOTES_TEMPLATE", "")
VAULT_AUDIT_LOG_PATH = os.environ.get("VAULT_AUDIT_LOG_PATH", "").strip()
VAULT_AUDIT_LOG_INCLUDE_READS = _env_bool("VAULT_AUDIT_LOG_INCLUDE_READS", False)

# Recurring task materialization
VAULT_RECURRING_ENABLED = _env_bool("VAULT_RECURRING_ENABLED", False)
VAULT_RECURRING_TEMPLATES_FOLDER = os.environ.get(
    "VAULT_RECURRING_TEMPLATES_FOLDER", ""
).strip().strip("/\\")
VAULT_RECURRING_INTERVAL = _env_int("VAULT_RECURRING_INTERVAL", 0)
VAULT_RECURRING_DONE_STATUS = os.environ.get(
    "VAULT_RECURRING_DONE_STATUS", "done"
).strip() or "done"
VAULT_RECURRING_CATCHUP_MODE = _env_choice(
    "VAULT_RECURRING_CATCHUP_MODE",
    "next",
    {"next", "all"},
)
VAULT_OBSIDIAN_REST_URL = os.environ.get("VAULT_OBSIDIAN_REST_URL", "").strip().rstrip("/")
VAULT_OBSIDIAN_REST_API_KEY = os.environ.get("VAULT_OBSIDIAN_REST_API_KEY", "").strip()
VAULT_OBSIDIAN_REST_VERIFY_TLS = _env_bool("VAULT_OBSIDIAN_REST_VERIFY_TLS", False)
VAULT_OBSIDIAN_REST_TIMEOUT = _env_int("VAULT_OBSIDIAN_REST_TIMEOUT", 15)
VAULT_TEMPLATER_FOLDER = os.environ.get("VAULT_TEMPLATER_FOLDER", "").strip().strip("/\\")
VAULT_DATAVIEW_TIMEOUT = _env_int("VAULT_DATAVIEW_TIMEOUT", 15)

# Optional semantic search
SEMANTIC_SEARCH_ENABLED = os.environ.get("VAULT_SEMANTIC_SEARCH_ENABLED", "").lower() in {
    "1", "true", "yes", "on",
}
SEMANTIC_EMBED_BACKEND = _env_choice(
    "VAULT_SEMANTIC_EMBED_BACKEND",
    "fastembed",
    {"auto", "sentence", "fastembed"},
)
SEMANTIC_EMBED_MODEL = os.environ.get("VAULT_SEMANTIC_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
SEMANTIC_CACHE_PATH = Path(
    os.environ.get(
        "VAULT_SEMANTIC_CACHE_PATH",
        str(VAULT_PATH / ".obsidian-vault-mcp"),
    )
)
VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH = Path(
    os.environ.get(
        "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH",
        str(SEMANTIC_CACHE_PATH / "oauth_registered_clients.json"),
    )
)
SEMANTIC_AUTO_REINDEX = _env_bool("VAULT_SEMANTIC_AUTO_REINDEX", False)
SEMANTIC_BUILD_ON_DEMAND = _env_bool("VAULT_SEMANTIC_BUILD_ON_DEMAND", False)
SEMANTIC_ALLOW_MCP_REINDEX = _env_bool("VAULT_SEMANTIC_ALLOW_MCP_REINDEX", False)
SEMANTIC_ALLOW_MCP_FULL_REINDEX = _env_bool("VAULT_SEMANTIC_ALLOW_MCP_FULL_REINDEX", False)
SEMANTIC_CHUNK_SIZE = _env_int("VAULT_SEMANTIC_CHUNK_SIZE", 900)
SEMANTIC_CHUNK_OVERLAP = _env_int("VAULT_SEMANTIC_CHUNK_OVERLAP", 150)
SEMANTIC_EMBED_BATCH_SIZE = _env_int("VAULT_SEMANTIC_EMBED_BATCH_SIZE", 64)
SEMANTIC_MAX_RESULTS = _env_int("VAULT_SEMANTIC_MAX_RESULTS", 20)
SEMANTIC_UPDATE_DEBOUNCE_SECONDS = _env_int("VAULT_SEMANTIC_UPDATE_DEBOUNCE_SECONDS", 4)

# Safety limits
MAX_CONTENT_SIZE = _env_int("VAULT_MAX_CONTENT_SIZE", 1_000_000)
MAX_BINARY_SIZE = _env_int("VAULT_MAX_BINARY_SIZE", 10 * 1024 * 1024)
IMPORT_URL_TIMEOUT_SECONDS = _env_int("VAULT_IMPORT_URL_TIMEOUT_SECONDS", 30)
IMPORT_URL_ALLOW_PRIVATE = _env_bool("VAULT_IMPORT_URL_ALLOW_PRIVATE", False)
MAX_BATCH_SIZE = _env_int("VAULT_MAX_BATCH_SIZE", 20)
MAX_SEARCH_RESULTS = _env_int("VAULT_MAX_SEARCH_RESULTS", 50)
MAX_FRONTMATTER_SEARCH_RESULTS = _env_int("VAULT_MAX_FRONTMATTER_SEARCH_RESULTS", 500)
MAX_FRONTMATTER_RESPONSE_BYTES = _env_int("VAULT_MAX_FRONTMATTER_RESPONSE_BYTES", 200_000)
DEFAULT_SEARCH_RESULTS = _env_int("VAULT_DEFAULT_SEARCH_RESULTS", 20)
MAX_LIST_DEPTH = _env_int("VAULT_MAX_LIST_DEPTH", 5)
MAX_TREE_DEPTH = _env_int("VAULT_MAX_TREE_DEPTH", 10)
CONTEXT_LINES = _env_int("VAULT_CONTEXT_LINES", 2)

# Directories to never expose or modify
EXCLUDED_DIRS = {".obsidian", ".trash", ".git", ".DS_Store", ".obsidian-vault-mcp"}

# Frontmatter index refresh interval (seconds)
FRONTMATTER_INDEX_DEBOUNCE = 5.0

# Rate limiting (requests per minute) -- track in-memory, enforce per-token
RATE_LIMIT_READ = _env_int("VAULT_RATE_LIMIT_READ", 100)
RATE_LIMIT_WRITE = _env_int("VAULT_RATE_LIMIT_WRITE", 30)
RATE_LIMIT_OAUTH_AUTHORIZE = _env_int("VAULT_RATE_LIMIT_OAUTH_AUTHORIZE", 30)
RATE_LIMIT_OAUTH_TOKEN = _env_int("VAULT_RATE_LIMIT_OAUTH_TOKEN", 30)
RATE_LIMIT_OAUTH_REGISTER = _env_int("VAULT_RATE_LIMIT_OAUTH_REGISTER", 10)

# Dynamic OAuth client registration limits
REGISTERED_CLIENT_TTL_SECONDS = _env_int("VAULT_REGISTERED_CLIENT_TTL_SECONDS", 0)
MAX_REGISTERED_CLIENTS = _env_int("VAULT_MAX_REGISTERED_CLIENTS", 128)
