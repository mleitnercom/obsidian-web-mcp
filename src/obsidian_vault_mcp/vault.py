"""Core filesystem operations for the Obsidian vault."""

import fnmatch
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from . import config

UNSUPPORTED_BINARY_EXTENSIONS = frozenset({
    ".7z",
    ".avi",
    ".bmp",
    ".doc",
    ".docx",
    ".eml",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".msg",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".tar",
    ".tiff",
    ".wav",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
})


class _DateAwareEncoder(json.JSONEncoder):
    """JSON encoder that serialises date/datetime objects to ISO 8601 strings.

    PyYAML's safe_load converts bare YAML dates (e.g. ``created: 2026-04-05``)
    into ``datetime.date`` objects.  The stdlib ``json`` module cannot handle
    these, so we provide a thin wrapper that converts them on the fly.
    """

    def default(self, obj: object) -> str:
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


def vault_json_dumps(obj: object, **kwargs) -> str:
    """``json.dumps`` replacement that handles ``datetime.date`` values."""
    return json.dumps(obj, cls=_DateAwareEncoder, **kwargs)


def resolve_vault_path(relative_path: str) -> Path:
    """Resolve a relative path against the vault root, with safety checks.

    Raises ValueError if the path escapes the vault, contains null bytes,
    or touches dotfile/dot-directory components.
    """
    if "\x00" in relative_path:
        raise ValueError("Path contains null bytes")

    # Check for dot-prefixed components (blocks .obsidian, .trash, dotfiles)
    parts = Path(relative_path).parts
    for part in parts:
        if part.startswith("."):
            raise ValueError(
                f"Path component '{part}' starts with '.'; dotfiles and hidden directories are not allowed"
            )

    resolved = (config.VAULT_PATH / relative_path).resolve()
    vault_root = config.VAULT_PATH.resolve()

    if not str(resolved).startswith(str(vault_root) + os.sep) and resolved != vault_root:
        raise ValueError("Path resolves outside the vault root")

    return resolved


def _iso_timestamp(ts: float) -> str:
    """Convert a Unix timestamp to an ISO 8601 string in UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _reject_unsupported_binary(path: Path) -> None:
    """Reject known binary formats before attempting UTF-8 text reads."""
    suffix = path.suffix.lower()
    if suffix in UNSUPPORTED_BINARY_EXTENSIONS:
        raise ValueError(
            f"Binary file type {suffix} is not supported by vault_read. "
            "Use a dedicated binary/PDF reader."
        )


def _read_pdf_file(path: Path) -> tuple[str, dict]:
    """Extract text and metadata from a PDF file."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            "PDF reading support requires pypdf to be installed"
        ) from e

    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ValueError(f"Failed to open PDF file: {e}") from e

    if getattr(reader, "is_encrypted", False):
        try:
            decrypt_result = reader.decrypt("")
        except Exception as e:
            raise ValueError("Encrypted PDF files are not supported by vault_read") from e
        if decrypt_result == 0 or getattr(reader, "is_encrypted", False):
            raise ValueError("Encrypted PDF files are not supported by vault_read")

    page_texts: list[str] = []
    extracted_page_count = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if text:
            extracted_page_count += 1
            page_texts.append(text)

    stat = path.stat()
    metadata = {
        "size": stat.st_size,
        "modified": _iso_timestamp(stat.st_mtime),
        "created": _iso_timestamp(stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_ctime),
        "type": "pdf",
        "content_source": "pdf_text_extraction",
        "pages": len(reader.pages),
        "pages_with_text": extracted_page_count,
        "extractable_text": extracted_page_count > 0,
    }

    return "\n\n".join(page_texts), metadata


def read_file(relative_path: str) -> tuple[str, dict]:
    """Read a file and return (content, metadata).

    Metadata keys: size (int), modified (ISO str), created (ISO str).
    """
    path = resolve_vault_path(relative_path)

    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {relative_path}")

    if path.suffix.lower() == ".pdf":
        return _read_pdf_file(path)

    _reject_unsupported_binary(path)

    stat = path.stat()
    content = path.read_text(encoding="utf-8")

    metadata = {
        "size": stat.st_size,
        "modified": _iso_timestamp(stat.st_mtime),
        "created": _iso_timestamp(stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_ctime),
    }

    return content, metadata


def write_file_atomic(
    relative_path: str, content: str, create_dirs: bool = True
) -> tuple[bool, int]:
    """Write content to a file atomically.

    Returns (is_new_file, bytes_written). Writes to a tempfile in the same
    directory then replaces the target, so readers never see a partial write.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > config.MAX_CONTENT_SIZE:
        raise ValueError(
            f"Content size {len(encoded)} bytes exceeds limit of {config.MAX_CONTENT_SIZE} bytes"
        )

    path = resolve_vault_path(relative_path)
    is_new = not path.exists()

    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory, then atomic-replace.
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return is_new, len(encoded)


def write_bytes_atomic(
    relative_path: str,
    content: bytes,
    create_dirs: bool = True,
    overwrite: bool = True,
) -> tuple[bool, int]:
    """Write raw bytes to a file atomically."""
    if len(content) > config.MAX_BINARY_SIZE:
        raise ValueError(
            f"Content size {len(content)} bytes exceeds limit of {config.MAX_BINARY_SIZE} bytes"
        )

    path = resolve_vault_path(relative_path)
    is_new = not path.exists()

    if not overwrite and not is_new:
        raise FileExistsError(f"File already exists: {relative_path}")

    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return is_new, len(content)


def move_path(
    source: str, destination: str, create_dirs: bool = True
) -> bool:
    """Move a file or directory from source to destination.

    Both paths are relative to the vault root. Raises if the destination
    already exists.
    """
    src = resolve_vault_path(source)
    dst = resolve_vault_path(destination)

    if not src.exists():
        raise FileNotFoundError(f"Source does not exist: {source}")

    if dst.exists():
        raise FileExistsError(f"Destination already exists: {destination}")

    if create_dirs:
        dst.parent.mkdir(parents=True, exist_ok=True)

    shutil.move(str(src), str(dst))
    return True


def delete_path(relative_path: str) -> bool:
    """Soft-delete by moving the path into .trash/ at the vault root.

    Refuses to delete non-empty directories.
    """
    path = resolve_vault_path(relative_path)

    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {relative_path}")

    if path.is_dir() and any(path.iterdir()):
        raise ValueError(f"Refusing to delete non-empty directory: {relative_path}")

    trash_dir = config.VAULT_PATH.resolve() / ".trash"
    trash_dir.mkdir(exist_ok=True)

    dest = trash_dir / path.name

    # Avoid collisions in .trash by appending a timestamp
    if dest.exists():
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = trash_dir / f"{path.stem}_{ts}{path.suffix}"

    shutil.move(str(path), str(dest))
    return True


def list_directory(
    relative_path: str,
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> list[dict]:
    """List directory contents recursively up to *depth* levels.

    Returns a list of dicts with keys: name, path (relative to vault),
    type ("file" or "dir"), size, modified.
    """
    depth = min(depth, config.MAX_LIST_DEPTH)

    root = resolve_vault_path(relative_path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")

    vault_root = config.VAULT_PATH.resolve()
    results: list[dict] = []

    def _walk(dir_path: Path, current_depth: int) -> None:
        if current_depth > depth:
            return

        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            return

        for entry in entries:
            # Skip excluded directories at every level
            if entry.name in config.EXCLUDED_DIRS:
                continue
            if entry.is_symlink():
                continue

            is_dir = entry.is_dir()

            if is_dir and not include_dirs:
                # Still recurse even if we're not listing dirs
                _walk(entry, current_depth + 1)
                continue

            if not is_dir and not include_files:
                continue

            # Apply glob pattern filter
            if pattern and not fnmatch.fnmatch(entry.name, pattern):
                if is_dir:
                    _walk(entry, current_depth + 1)
                continue

            try:
                stat = entry.stat()
            except OSError:
                continue

            rel = str(entry.relative_to(vault_root))

            results.append({
                "name": entry.name,
                "path": rel,
                "type": "dir" if is_dir else "file",
                "size": stat.st_size,
                "modified": _iso_timestamp(stat.st_mtime),
            })

            if is_dir:
                _walk(entry, current_depth + 1)

    _walk(root, 1)
    return results


def scan_markdown_encoding_issues(
    relative_path: str = "",
    max_results: int = 100,
) -> list[dict]:
    """Return markdown files under the vault that are not valid UTF-8."""
    root = resolve_vault_path(relative_path) if relative_path else config.VAULT_PATH.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")

    vault_root = config.VAULT_PATH.resolve()
    issues: list[dict] = []

    for path in root.rglob("*.md"):
        if any(part in config.EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            issues.append(
                {
                    "path": str(path.relative_to(vault_root)),
                    "position": e.start,
                    "reason": e.reason,
                }
            )
            if len(issues) >= max_results:
                break

    return issues


def repair_markdown_encoding_issues(
    relative_path: str = "",
    max_files: int = 50,
    source_encoding: str = "cp1252",
    dry_run: bool = False,
) -> dict:
    """Repair markdown files that are not valid UTF-8 using a chosen source encoding."""
    root = resolve_vault_path(relative_path) if relative_path else config.VAULT_PATH.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")

    vault_root = config.VAULT_PATH.resolve()
    repaired: list[dict] = []
    failed: list[dict] = []

    for path in root.rglob("*.md"):
        if any(part in config.EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue

        raw = path.read_bytes()
        try:
            raw.decode("utf-8")
            continue
        except UnicodeDecodeError:
            pass

        rel = str(path.relative_to(vault_root))
        try:
            decoded = raw.decode(source_encoding)
            if not dry_run:
                path.write_text(decoded, encoding="utf-8")
            repaired.append(
                {
                    "path": rel,
                    "source_encoding": source_encoding,
                    "bytes_before": len(raw),
                    "bytes_after": len(decoded.encode("utf-8")),
                    "changed": not dry_run,
                }
            )
        except UnicodeDecodeError as e:
            failed.append(
                {
                    "path": rel,
                    "source_encoding": source_encoding,
                    "position": e.start,
                    "reason": e.reason,
                }
            )

        if len(repaired) + len(failed) >= max_files:
            break

    return {
        "path_prefix": relative_path,
        "source_encoding": source_encoding,
        "dry_run": dry_run,
        "repaired_count": len(repaired),
        "failed_count": len(failed),
        "repaired": repaired,
        "failed": failed,
        "truncated": len(repaired) + len(failed) >= max_files,
    }


def delete_directory_path(relative_path: str, only_if_empty: bool = True) -> bool:
    """Soft-delete a directory by moving it into .trash/ at the vault root."""
    path = resolve_vault_path(relative_path)

    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {relative_path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")
    if only_if_empty and any(path.iterdir()):
        raise ValueError(f"Refusing to delete non-empty directory: {relative_path}")

    trash_dir = config.VAULT_PATH.resolve() / ".trash"
    trash_dir.mkdir(exist_ok=True)

    dest = trash_dir / path.name
    if dest.exists():
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = trash_dir / f"{path.name}_{ts}"

    shutil.move(str(path), str(dest))
    return True
