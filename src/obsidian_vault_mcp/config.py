import json
import os
from pathlib import Path


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
VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS = _env_bool("VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", True)
VAULT_PUBLIC_BASE_URL = os.environ.get("VAULT_PUBLIC_BASE_URL", "").strip().rstrip("/")
TRUSTED_PROXY_IPS = os.environ.get("VAULT_TRUSTED_PROXY_IPS", "127.0.0.1,::1")
ALLOWED_HOSTS = _env_csv(
    "VAULT_ALLOWED_HOSTS",
    ["127.0.0.1:*", "localhost:*", "[::1]:*"],
)
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
