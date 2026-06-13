"""Pydantic input models for obsidian-vault-mcp tool endpoints."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import (
    CONTEXT_LINES,
    DEFAULT_SEARCH_RESULTS,
    MAX_BATCH_SIZE,
    MAX_BINARY_SIZE,
    MAX_CONTENT_SIZE,
    MAX_FRONTMATTER_SEARCH_RESULTS,
    MAX_LIST_DEPTH,
    MAX_SEARCH_RESULTS,
    SEMANTIC_MAX_RESULTS,
    MAX_TREE_DEPTH,
    VAULT_UPLOAD_URL_MAX_TTL_SECONDS,
)


class VaultReadInput(BaseModel):
    """Read a single file from the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root (e.g. 'projects/acme/notes.md')",
        min_length=1,
        max_length=500,
    )


class VaultWriteInput(BaseModel):
    """Write or overwrite a file in the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root",
        min_length=1,
        max_length=500,
    )
    content: str = Field(
        ...,
        description="Full file content to write",
        max_length=MAX_CONTENT_SIZE,
    )
    create_dirs: bool = Field(
        default=True,
        description="Create parent directories if they don't exist",
    )
    merge_frontmatter: bool = Field(
        default=False,
        description="If true, merge YAML frontmatter with existing file's frontmatter instead of replacing",
    )


class VaultWriteBinaryInput(BaseModel):
    """Write or overwrite an allowed binary file in the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root including filename and extension",
        min_length=1,
        max_length=500,
    )
    data: str = Field(
        ...,
        description="Base64-encoded binary content",
        min_length=1,
        max_length=((MAX_BINARY_SIZE + 2) // 3) * 4 + 1024,
    )
    media_type: str = Field(
        ...,
        description="MIME type of the binary content",
        min_length=3,
        max_length=200,
    )
    overwrite: bool = Field(
        default=False,
        description="If true, allow replacing an existing file",
    )
    create_dirs: bool = Field(
        default=True,
        description="Create parent directories if they don't exist",
    )


class VaultRequestUploadUrlInput(BaseModel):
    """Create a signed direct HTTP upload URL for binary content."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="Relative path from vault root including filename and extension", min_length=1, max_length=500)
    media_type: str = Field(..., description="MIME type of the binary content", min_length=3, max_length=200)
    max_size_bytes: int = Field(..., ge=1, le=MAX_BINARY_SIZE, description="Maximum byte size accepted by the signed upload URL")
    overwrite: bool = Field(default=False, description="If true, allow replacing an existing file")
    create_dirs: bool = Field(default=True, description="Create parent directories if they do not exist")
    expected_sha256: str | None = Field(default=None, description="Optional SHA-256 checksum of the uploaded content", min_length=64, max_length=64)
    ttl_seconds: int | None = Field(default=None, ge=1, le=VAULT_UPLOAD_URL_MAX_TTL_SECONDS, description="Optional URL lifetime in seconds")


class VaultImportUrlInput(BaseModel):
    """Import an allowed binary file from a URL."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="Relative path from vault root including filename and extension", min_length=1, max_length=500)
    url: str = Field(..., description="HTTP or HTTPS URL to download from", min_length=1, max_length=2000)
    media_type: str = Field(..., description="Expected MIME type of the downloaded content", min_length=3, max_length=200)
    overwrite: bool = Field(default=False, description="If true, allow replacing an existing file")
    create_dirs: bool = Field(default=True, description="Create parent directories if they do not exist")
    expected_sha256: str | None = Field(default=None, description="Optional SHA-256 checksum of the downloaded content", min_length=64, max_length=64)


class VaultImportFileInput(BaseModel):
    """Import an allowed binary file from a local source path."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="Relative path from vault root including filename and extension", min_length=1, max_length=500)
    source_path: str = Field(..., description="Absolute or mounted local filesystem path to import from", min_length=1, max_length=2000)
    media_type: str = Field(..., description="Expected MIME type of the source file", min_length=3, max_length=200)
    overwrite: bool = Field(default=False, description="If true, allow replacing an existing file")
    create_dirs: bool = Field(default=True, description="Create parent directories if they do not exist")
    expected_sha256: str | None = Field(default=None, description="Optional SHA-256 checksum of the source file", min_length=64, max_length=64)


class VaultStrReplaceInput(BaseModel):
    """Replace exactly one unique string in an existing file."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root",
        min_length=1,
        max_length=500,
    )
    old_string: str = Field(
        ...,
        description="Exact string to replace; must occur exactly once",
        min_length=1,
        max_length=MAX_CONTENT_SIZE,
    )
    new_string: str = Field(
        default="",
        description="Replacement string; empty string deletes the matched text",
        max_length=MAX_CONTENT_SIZE,
    )
    replace_all: bool = Field(
        default=False,
        description="If true, replace every occurrence of old_string instead of requiring a unique match",
    )


class VaultBatchReplaceInput(BaseModel):
    """Replace exact strings across multiple files."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    updates: list[dict] = Field(
        ...,
        description="List of updates, each with path, old_str, optional new_str, and optional replace_all",
        min_length=1,
        max_length=MAX_BATCH_SIZE,
    )

    @field_validator("updates")
    @classmethod
    def validate_updates(cls, v: list[dict]) -> list[dict]:
        for i, item in enumerate(v):
            if "path" not in item or not isinstance(item["path"], str):
                raise ValueError(f"updates[{i}] must contain a 'path' key with a string value")
            if "old_str" not in item or not isinstance(item["old_str"], str) or not item["old_str"]:
                raise ValueError(f"updates[{i}] must contain a non-empty 'old_str' string")
            if "new_str" in item and not isinstance(item["new_str"], str):
                raise ValueError(f"updates[{i}] 'new_str' must be a string when provided")
            if "replace_all" in item and not isinstance(item["replace_all"], bool):
                raise ValueError(f"updates[{i}] 'replace_all' must be a boolean when provided")
        return v


class VaultPatchInput(BaseModel):
    """Apply a targeted unique patch to a file."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    old_text: str = Field(..., description="Unique exact text to replace", min_length=1, max_length=MAX_CONTENT_SIZE)
    new_text: str = Field(default="", description="Replacement text", max_length=MAX_CONTENT_SIZE)


class VaultAppendInput(BaseModel):
    """Append content to the end of a file."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    path: str = Field(..., description="Relative path from vault root", min_length=1, max_length=500)
    content: str = Field(..., description="Text to append", max_length=MAX_CONTENT_SIZE)
    create_if_missing: bool = Field(
        default=False,
        description="If true, create the file when it does not already exist",
    )


class VaultDailyNoteAppendInput(BaseModel):
    """Append content to today's configured daily note."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    content: str = Field(
        ...,
        description=(
            "Text to append to today's daily note. If the note is missing, it is created "
            "using VAULT_DAILY_NOTES_TEMPLATE before this content."
        ),
        max_length=MAX_CONTENT_SIZE,
    )


class VaultTemplateListInput(BaseModel):
    """List simple template files from the configured template folder."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    folder: str | None = Field(
        default=None,
        description="Optional vault-relative template folder override. Defaults to VAULT_TEMPLATER_FOLDER.",
        max_length=500,
    )
    recursive: bool = Field(
        default=True,
        description="If true, include markdown templates in nested folders.",
    )


class VaultTemplateRenderInput(BaseModel):
    """Render one template with simple variable substitution."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    template_path: str = Field(
        ...,
        description="Template path relative to VAULT_TEMPLATER_FOLDER or the vault root.",
        min_length=1,
        max_length=500,
    )
    target_path_hint: str | None = Field(
        default=None,
        description="Optional intended output path used for built-in {{target_path}} and {{title}} tokens.",
        max_length=500,
    )
    variables: dict[str, str | int | float | bool | None] | None = Field(
        default=None,
        description="Optional values for {{key}} or {{variables.key}} tokens. Missing variables fail hard.",
    )
    engine: Literal["simple"] = Field(
        default="simple",
        description="Allowed value: simple. Simple variable substitution only; not full Templater execution.",
    )


class VaultTemplateApplyInput(BaseModel):
    """Render one simple template and write the resulting note."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    template_path: str = Field(
        ...,
        description="Template path relative to VAULT_TEMPLATER_FOLDER or the vault root.",
        min_length=1,
        max_length=500,
    )
    target_path: str = Field(
        ...,
        description="Vault-relative note path to create or overwrite.",
        min_length=1,
        max_length=500,
    )
    variables: dict[str, str | int | float | bool | None] | None = Field(
        default=None,
        description="Optional values for {{key}} or {{variables.key}} tokens. Missing variables fail hard.",
    )
    overwrite: bool = Field(
        default=False,
        description="If true, allow replacing an existing target file.",
    )
    engine: Literal["simple"] = Field(
        default="simple",
        description="Allowed value: simple. Simple variable substitution only; not full Templater execution.",
    )


class VaultDataviewQueryInput(BaseModel):
    """Run a Dataview DQL TABLE query through Obsidian Local REST API."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Dataview DQL TABLE query. TABLE WITHOUT ID is not supported.",
        min_length=1,
        max_length=10_000,
    )
    query_type: Literal["dql"] = Field(
        default="dql",
        description="Allowed value: dql.",
    )
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        le=120,
        description="Optional per-query timeout in seconds. Defaults to VAULT_DATAVIEW_TIMEOUT.",
    )


class VaultAnalyticsSummaryInput(BaseModel):
    """Build a compact analytics summary for a vault path."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path_prefix: str | None = Field(
        default=None,
        description="Optional folder prefix to restrict the analysis",
        max_length=500,
    )
    required_frontmatter: list[str] | None = Field(
        default=None,
        description="Optional required frontmatter fields to validate",
        max_length=20,
    )
    max_examples: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum example findings to include per category",
    )


class VaultAnalyticsFindingsInput(BaseModel):
    """Return detailed findings for one analytics category."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    category: Literal[
        "frontmatter_missing",
        "required_frontmatter_missing",
        "broken_wikilinks",
        "suspicious_tag_variants",
        "encoding_issues",
    ] = Field(
        ...,
        description="Analytics finding category to return",
    )
    path_prefix: str | None = Field(
        default=None,
        description="Optional folder prefix to restrict the analysis",
        max_length=500,
    )
    required_frontmatter: list[str] | None = Field(
        default=None,
        description="Optional required frontmatter fields to validate",
        max_length=20,
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of findings to return",
    )


class VaultListInput(BaseModel):
    """List files and directories under a vault path."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        default="",
        description="Relative directory path from vault root; empty string for root",
        max_length=500,
    )
    depth: int = Field(
        default=1,
        ge=1,
        le=MAX_LIST_DEPTH,
        description="How many levels deep to recurse",
    )
    include_files: bool = Field(
        default=True,
        description="Include files in the listing",
    )
    include_dirs: bool = Field(
        default=True,
        description="Include directories in the listing",
    )
    pattern: str | None = Field(
        default=None,
        description="Optional glob pattern to filter results (e.g. '*.md')",
        max_length=100,
    )
    include_ocr_sidecars: bool = Field(
        default=False,
        description="Include generated PDF OCR sidecar files such as '*.pdf.ocr.txt'",
    )


class VaultMoveInput(BaseModel):
    """Move or rename a file/directory within the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    source: str = Field(
        ...,
        description="Current relative path of the file or directory",
        min_length=1,
        max_length=500,
    )
    destination: str = Field(
        ...,
        description="New relative path for the file or directory",
        min_length=1,
        max_length=500,
    )
    create_dirs: bool = Field(
        default=True,
        description="Create destination parent directories if they don't exist",
    )


class VaultTreeInput(BaseModel):
    """Return a compact nested directory tree for a vault path."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        default="",
        description="Relative directory path from vault root; empty string for root",
        max_length=500,
    )
    depth: int = Field(
        default=3,
        ge=1,
        le=MAX_TREE_DEPTH,
        description="How many directory levels deep to include in the tree",
    )


class VaultDeleteInput(BaseModel):
    """Delete a file from the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path of the file to delete",
        min_length=1,
        max_length=500,
    )
    confirm: bool = Field(
        ...,
        description="Must be true to execute deletion -- safety gate to prevent accidental deletes",
    )


class VaultDeleteDirectoryInput(BaseModel):
    """Delete a directory from the vault."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path of the directory to delete",
        min_length=1,
        max_length=500,
    )
    confirm: bool = Field(
        ...,
        description="Must be true to execute deletion -- safety gate to prevent accidental deletes",
    )
    only_if_empty: bool = Field(
        default=True,
        description="Require the directory to be empty before moving it to .trash/",
    )


class VaultSearchInput(BaseModel):
    """Full-text search across vault files."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Search string to find in file contents",
        min_length=1,
        max_length=200,
    )
    path_prefix: str | None = Field(
        default=None,
        description="Limit search to files under this directory prefix",
        max_length=500,
    )
    file_pattern: str = Field(
        default="*.md",
        description="Glob pattern for files to search (e.g. '*.md', '*.canvas')",
        max_length=50,
    )
    max_results: int = Field(
        default=DEFAULT_SEARCH_RESULTS,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description="Maximum number of matching files to return",
    )
    context_lines: int = Field(
        default=CONTEXT_LINES,
        ge=0,
        le=10,
        description="Number of lines of context to show around each match",
    )


class FrontmatterSearchFilterInput(BaseModel):
    """One frontmatter filter condition."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    field: str = Field(
        ...,
        description="Frontmatter field name to search (e.g. 'status', 'tags', 'publish-date')",
        min_length=1,
        max_length=100,
    )
    value: str | int | float | bool | list[Any] | dict[str, Any] = Field(
        default="",
        description=(
            "Value to match against. For 'in' and 'list_*' operators pass an array. "
            "For comparison operators pass a scalar such as an ISO date string, number, or plain string. "
            "Ignored when match_type is 'exists'."
        ),
    )
    match_type: Literal[
        "exact",
        "contains",
        "exists",
        "lte",
        "gte",
        "lt",
        "gt",
        "in",
        "list_contains",
        "list_any",
        "list_all",
    ] = Field(
        default="exact",
        description=(
            "How to match: exact equality, substring contains, field existence, "
            "numeric/date comparisons, scalar membership, or list membership semantics. "
            "Allowed values: exact, contains, exists, lte, gte, lt, gt, in, list_contains, list_any, list_all."
        ),
    )


class VaultSearchFrontmatterInput(BaseModel):
    """Search vault files by YAML frontmatter field values with comparison, list-membership, and AND filters."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    field: str = Field(
        ...,
        description="Frontmatter field name to search (e.g. 'status', 'tags', 'publish-date')",
        min_length=1,
        max_length=100,
    )
    value: str | int | float | bool | list[Any] | dict[str, Any] = Field(
        default="",
        description=(
            "Value to match against. For 'in' and 'list_*' operators pass an array. "
            "For comparison operators pass a scalar such as an ISO date string, number, or plain string. "
            "Ignored when match_type is 'exists'."
        ),
    )
    match_type: Literal[
        "exact",
        "contains",
        "exists",
        "lte",
        "gte",
        "lt",
        "gt",
        "in",
        "list_contains",
        "list_any",
        "list_all",
    ] = Field(
        default="exact",
        description=(
            "How to match: exact equality, substring contains, field existence, "
            "numeric/date comparisons, scalar membership, or list membership semantics. "
            "Allowed values: exact, contains, exists, lte, gte, lt, gt, in, list_contains, list_any, list_all."
        ),
    )
    filters: list[FrontmatterSearchFilterInput] | None = Field(
        default=None,
        description=(
            "Optional additional AND filters to apply in the same query. "
            "Each entry uses the same {field, match_type, value} schema as the top-level filter."
        ),
        max_length=10,
    )
    path_prefix: str | None = Field(
        default=None,
        description="Limit search to files under this directory prefix",
        max_length=500,
    )
    max_results: int = Field(
        default=DEFAULT_SEARCH_RESULTS,
        ge=1,
        le=MAX_FRONTMATTER_SEARCH_RESULTS,
        description="Maximum number of matching files to return",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of matching files to skip before returning results",
    )


class VaultSemanticSearchInput(BaseModel):
    """Semantic or hybrid retrieval across vault markdown content."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Natural-language search query",
        min_length=1,
        max_length=300,
    )
    path_prefix: str | None = Field(
        default=None,
        description="Optional folder prefix to restrict semantic results",
        max_length=500,
    )
    filter_tags: list[str] | None = Field(
        default=None,
        description="Optional tag filter; all tags must be present in a chunk",
        max_length=20,
    )
    search_mode: Literal["hybrid", "semantic", "keyword"] = Field(
        default="hybrid",
        description="Ranking mode: blend semantic and keyword scores, or use only one signal",
    )
    max_results: int = Field(
        default=10,
        ge=1,
        le=SEMANTIC_MAX_RESULTS,
        description="Maximum number of semantic matches to return",
    )
    min_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum hybrid score required for returned results",
    )


class VaultReindexInput(BaseModel):
    """Rebuild the semantic search index from the current vault contents."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    full: bool = Field(
        default=True,
        description="Rebuild the semantic index from scratch",
    )


class VaultBatchReadInput(BaseModel):
    """Read multiple vault files in a single request."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    paths: list[str] = Field(
        ...,
        description="List of relative paths to read",
        min_length=1,
        max_length=MAX_BATCH_SIZE,
    )
    include_content: bool = Field(
        default=True,
        description="If false, return metadata only (frontmatter, size) without file body",
    )


class VaultBatchFrontmatterUpdateInput(BaseModel):
    """Update YAML frontmatter on multiple files in one request."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    updates: list[dict] = Field(
        ...,
        description="List of updates, each a dict with 'path' (str) and 'fields' (dict of key-value pairs to set)",
        min_length=1,
        max_length=MAX_BATCH_SIZE,
    )

    @field_validator("updates")
    @classmethod
    def validate_updates(cls, v: list[dict]) -> list[dict]:
        for i, item in enumerate(v):
            if "path" not in item or not isinstance(item["path"], str):
                raise ValueError(f"updates[{i}] must contain a 'path' key with a string value")
            if "fields" not in item or not isinstance(item["fields"], dict):
                raise ValueError(f"updates[{i}] must contain a 'fields' key with a dict value")
        return v


class VaultEditOperationInput(BaseModel):
    """One exact text replacement inside a file (upstream vault_edit contract)."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def normalize_str_replace_aliases(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for canonical, alias in (("old_text", "old_str"), ("new_text", "new_str")):
            if canonical in normalized and alias in normalized:
                raise ValueError(f"Use either '{canonical}' or '{alias}', not both")
            if alias in normalized:
                normalized[canonical] = normalized.pop(alias)
        return normalized

    old_text: str = Field(
        ...,
        description="Exact existing text fragment to replace; must appear exactly once",
        min_length=1,
        max_length=MAX_CONTENT_SIZE,
    )
    new_text: str = Field(
        ...,
        description="Replacement text for old_text (empty string deletes the matched text)",
        max_length=MAX_CONTENT_SIZE,
    )


class VaultEditInput(BaseModel):
    """Patch an existing file with ordered exact text replacements."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root",
        min_length=1,
        max_length=500,
    )
    edits: list[VaultEditOperationInput] = Field(
        ...,
        description="Ordered exact text replacements to apply without resending the full file",
        min_length=1,
        max_length=MAX_BATCH_SIZE,
    )
    dry_run: bool = Field(
        default=False,
        description="Preview the patch and unified diff without writing the file",
    )
