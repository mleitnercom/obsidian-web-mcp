"""Pydantic input models for obsidian-vault-mcp tool endpoints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import (
    CONTEXT_LINES,
    DEFAULT_SEARCH_RESULTS,
    MAX_BATCH_SIZE,
    MAX_BINARY_SIZE,
    MAX_CONTENT_SIZE,
    MAX_LIST_DEPTH,
    MAX_SEARCH_RESULTS,
    SEMANTIC_MAX_RESULTS,
    MAX_TREE_DEPTH,
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
    media_type: Literal[
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/svg+xml",
        "application/pdf",
    ] = Field(
        ...,
        description="MIME type of the binary content",
    )
    overwrite: bool = Field(
        default=False,
        description="If true, allow replacing an existing file",
    )
    create_dirs: bool = Field(
        default=True,
        description="Create parent directories if they don't exist",
    )


class VaultStrReplaceInput(BaseModel):
    """Replace exactly one unique string in an existing file."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    path: str = Field(
        ...,
        description="Relative path from vault root",
        min_length=1,
        max_length=500,
    )
    old_str: str = Field(
        ...,
        description="Exact string to replace; must occur exactly once",
        min_length=1,
        max_length=MAX_CONTENT_SIZE,
    )
    new_str: str = Field(
        default="",
        description="Replacement string; empty string deletes the matched text",
        max_length=MAX_CONTENT_SIZE,
    )
    replace_all: bool = Field(
        default=False,
        description="If true, replace every occurrence of old_str instead of requiring a unique match",
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


class VaultSearchFrontmatterInput(BaseModel):
    """Search vault files by YAML frontmatter field values."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    field: str = Field(
        ...,
        description="Frontmatter field name to search (e.g. 'status', 'tags', 'publish-date')",
        min_length=1,
        max_length=100,
    )
    value: str = Field(
        default="",
        description="Value to match against; ignored when match_type is 'exists'",
        max_length=200,
    )
    match_type: Literal["exact", "contains", "exists"] = Field(
        default="exact",
        description="How to match: 'exact' for equality, 'contains' for substring, 'exists' to check field presence",
    )
    path_prefix: str | None = Field(
        default=None,
        description="Limit search to files under this directory prefix",
        max_length=500,
    )
    max_results: int = Field(
        default=DEFAULT_SEARCH_RESULTS,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description="Maximum number of matching files to return",
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
