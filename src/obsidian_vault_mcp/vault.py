"""Core filesystem operations for the Obsidian vault."""

import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
# Converged to upstream's serialization module (#9). vault_json_dumps is kept as a
# thin alias so existing call sites work unchanged; the canonical name is
# serialization.dumps (call-site rename happens at the Phase-2 re-fork).
from .serialization import dumps as vault_json_dumps

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


# vault_json_dumps is now imported from .serialization (see top of file, #9).


class OcrError(RuntimeError):
    """OCR-specific failure with a stable client-facing error code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _included_root_paths() -> list[Path]:
    """Return resolved subtree roots that MCP is allowed to access."""
    vault_root = config.VAULT_PATH.resolve()
    roots: list[Path] = []
    for root in config.INCLUDED_ROOTS:
        candidate = vault_root if root in {"", "."} else (vault_root / root).resolve()
        if candidate not in roots:
            roots.append(candidate)
    return roots or [vault_root]


def allowed_root_paths() -> list[Path]:
    """Return resolved vault subtree roots that are visible to MCP."""
    return list(_included_root_paths())


def _is_within_path(child: Path, parent: Path) -> bool:
    """Return whether *child* equals or is inside *parent*."""
    return child == parent or parent in child.parents


def _relative_to_vault_root(path: Path) -> str:
    """Return a normalized POSIX-style path relative to the vault root."""
    return path.relative_to(config.VAULT_PATH.resolve()).as_posix()


def _matches_excluded_prefix(rel_path: str) -> bool:
    """Return whether a relative path falls under an excluded prefix."""
    normalized = rel_path.strip("/")
    if not normalized:
        return False

    for prefix in config.EXCLUDED_PATH_PREFIXES:
        candidate = prefix.strip().strip("/")
        if not candidate:
            continue
        if normalized == candidate or normalized.startswith(f"{candidate}/"):
            return True
    return False


def _vault_policy_error(resolved: Path, vault_root: Path | None = None) -> str | None:
    """Return a policy error string for a resolved path, or None when allowed."""
    vault_root = vault_root or config.VAULT_PATH.resolve()

    if not _is_within_path(resolved, vault_root):
        return "Path resolves outside the vault root"

    rel = resolved.relative_to(vault_root)
    for part in rel.parts:
        if part.startswith("."):
            return (
                f"Path component '{part}' starts with '.'; dotfiles and hidden directories are not allowed"
            )

    if rel.parts and rel.parts[0] in config.EXCLUDED_DIRS:
        return f"Path is under an excluded directory: {rel.parts[0]}"

    rel_path = rel.as_posix()
    if _matches_excluded_prefix(rel_path):
        return f"Path is under an excluded prefix: {rel_path}"

    included_roots = _included_root_paths()
    if not any(_is_within_path(resolved, root) for root in included_roots):
        return "Path is outside the allowlisted vault subtrees (VAULT_INCLUDED_ROOTS)"

    return None


def is_vault_path_allowed(path: Path) -> bool:
    """Return whether a path passes the configured vault access policy."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return _vault_policy_error(resolved) is None


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
    error = _vault_policy_error(resolved, vault_root)
    if error is not None:
        raise ValueError(error)

    return resolved


def _iso_timestamp(ts: float) -> str:
    """Convert a Unix timestamp to an ISO 8601 string in UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _iso_timestamp_seconds(ts: float) -> str:
    """Convert a Unix timestamp to a second-precision UTC ISO string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ocr_sidecar_suffix() -> str:
    """Return the configured sidecar suffix."""
    suffix = config.VAULT_PDF_OCR_SIDECAR_SUFFIX.strip()
    return suffix or ".ocr.txt"


def is_ocr_sidecar_name(name: str) -> bool:
    """Return whether a filename looks like a generated PDF OCR sidecar."""
    return name.endswith(_ocr_sidecar_suffix())


def _pdf_source_fingerprint(path: Path) -> tuple[str, str]:
    """Return (mtime_iso, first-64KB SHA-256 prefix) for OCR cache identity."""
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(64 * 1024))
    return _iso_timestamp_seconds(stat.st_mtime), digest.hexdigest()[:16]


def _ocr_sidecar_path(path: Path) -> Path:
    """Return the sidecar path for a PDF path."""
    return path.with_name(f"{path.name}{_ocr_sidecar_suffix()}")


def _ocr_lock_path(sidecar_path: Path) -> Path:
    """Return a dot-prefixed lock path next to the sidecar."""
    return sidecar_path.with_name(f".{sidecar_path.name}.lock")


def _read_valid_ocr_sidecar(path: Path, sidecar_path: Path) -> str | None:
    """Read a sidecar only if its metadata matches the current PDF."""
    if not sidecar_path.is_file():
        return None
    try:
        lines = sidecar_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None

    mtime_iso, digest = _pdf_source_fingerprint(path)
    expected_source = f"# Source: {mtime_iso}:{digest}"
    if lines[1].strip() != expected_source:
        return None
    return "\n".join(lines[2:]).lstrip("\n")


def _write_ocr_sidecar(path: Path, sidecar_path: Path, content: str) -> int:
    """Atomically write OCR sidecar content with sync-friendly dotfile temp naming."""
    mtime_iso, digest = _pdf_source_fingerprint(path)
    generated_at = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = (
        f"# OCR generated {generated_at}\n"
        f"# Source: {mtime_iso}:{digest}\n\n"
        f"{content.rstrip()}\n"
    )
    encoded = payload.encode("utf-8")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=sidecar_path.parent,
        prefix=f".{sidecar_path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.replace(tmp_path, sidecar_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return len(encoded)


class _OcrSidecarLock:
    """Small cross-platform lock based on exclusive lock-file creation."""

    def __init__(self, lock_path: Path, timeout_seconds: int):
        self.lock_path = lock_path
        self.timeout_seconds = max(1, timeout_seconds)

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"{os.getpid()}\n")
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise OcrError("ocr_timeout", f"OCR sidecar lock timed out after {self.timeout_seconds} seconds")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


def has_extra_hard_links(path: Path) -> bool:
    """Return whether a file has more than one directory entry pointing at its inode.

    A hardlink inside the vault to a file outside it is a real directory entry: there is
    no link to follow, so path containment cannot see it and the file reads as an
    ordinary note. Refusing st_nlink > 1 is the only cheap test that catches it, at the
    cost of not supporting legitimate in-vault hardlinks -- which nothing here creates:
    the write tools write files, imports copy, and Obsidian Sync does not make links.
    (upstream issue #53)
    """
    try:
        return path.stat().st_nlink > 1
    except OSError:
        return False


def refuse_hard_linked_file(path: Path) -> None:
    """Raise for a hardlinked file. Fail closed, like the frontmatter and OAuth paths."""
    if has_extra_hard_links(path):
        raise ValueError(
            "Refusing to read a hardlinked file; in-vault hardlinks are not supported "
            "because they can point outside the vault"
        )


def _reject_unsupported_binary(path: Path) -> None:
    """Reject known binary formats before attempting UTF-8 text reads."""
    suffix = path.suffix.lower()
    if suffix in UNSUPPORTED_BINARY_EXTENSIONS:
        raise ValueError(
            f"Binary file type {suffix} is not supported by vault_read. "
            "Use a dedicated binary/PDF reader."
        )


IMAGE_OCR_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """Return (width, height) by reading the file header only.

    Deliberately not Pillow: the server would gain an image dependency for two integers.
    Returns None for anything unrecognised -- dimensions are a nice-to-have in the
    metadata, never a reason to fail a read.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return (
                    int.from_bytes(head[16:20], "big"),
                    int.from_bytes(head[20:24], "big"),
                )
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return (
                    int.from_bytes(head[6:8], "little"),
                    int.from_bytes(head[8:10], "little"),
                )
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8X":
                    handle.seek(24)
                    payload = handle.read(6)
                    if len(payload) == 6:
                        width = int.from_bytes(payload[0:3], "little") + 1
                        height = int.from_bytes(payload[3:6], "little") + 1
                        return width, height
                if chunk == b"VP8 ":
                    handle.seek(26)
                    payload = handle.read(4)
                    if len(payload) == 4:
                        return (
                            int.from_bytes(payload[0:2], "little") & 0x3FFF,
                            int.from_bytes(payload[2:4], "little") & 0x3FFF,
                        )
                if chunk == b"VP8L":
                    handle.seek(21)
                    payload = handle.read(4)
                    if len(payload) == 4:
                        bits = int.from_bytes(payload, "little")
                        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
                return None
            if head[:2] == b"\xff\xd8":
                # JPEG: walk the segment chain to the frame header that carries the size.
                handle.seek(2)
                while True:
                    marker = handle.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    if marker[1] in (0xD8, 0xD9) or 0xD0 <= marker[1] <= 0xD7:
                        continue
                    length_bytes = handle.read(2)
                    if len(length_bytes) < 2:
                        return None
                    length = int.from_bytes(length_bytes, "big")
                    # SOF0..SOF15, excluding the non-frame markers DHT/JPG/DAC.
                    if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                        frame = handle.read(5)
                        if len(frame) < 5:
                            return None
                        return (
                            int.from_bytes(frame[3:5], "big"),
                            int.from_bytes(frame[1:3], "big"),
                        )
                    handle.seek(length - 2, os.SEEK_CUR)
    except OSError:
        return None
    return None


def _run_image_ocr(path: Path) -> dict | None:
    """Run the configured OCR command for an image file.

    A sibling of _run_pdf_ocr rather than a shared generalisation: the PDF path is
    load-bearing and explicitly out of scope for this change, so it is left untouched.
    Worth folding together later, once both have run in production for a while.
    """
    if not config.VAULT_IMAGE_OCR_ENABLED or not config.VAULT_IMAGE_OCR_CMD:
        return None

    argv = shlex.split(config.VAULT_IMAGE_OCR_CMD, posix=os.name != "nt")
    if not argv:
        return None
    if any("{path}" in arg for arg in argv):
        argv = [arg.format(path=str(path)) for arg in argv]
    else:
        argv = [*argv, str(path)]

    env = os.environ.copy()
    env["VAULT_IMAGE_PATH"] = str(path)
    env["VAULT_IMAGE_OCR_LANGUAGES"] = config.VAULT_IMAGE_OCR_LANGUAGES

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=config.VAULT_IMAGE_OCR_TIMEOUT,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        raise OcrError("ocr_tool_unavailable", f"OCR command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise OcrError(
            "ocr_timeout",
            f"OCR command timed out after {config.VAULT_IMAGE_OCR_TIMEOUT} seconds",
        )

    content = result.stdout.strip()
    metadata = {
        "applied": result.returncode == 0 and bool(content),
        "engine": "external_command",
        "command": Path(argv[0]).name,
        "languages": config.VAULT_IMAGE_OCR_LANGUAGES.split("+"),
        "returncode": result.returncode,
    }
    if result.stderr.strip():
        metadata["stderr"] = result.stderr.strip()[:500]
    if result.returncode != 0:
        message = f"OCR command exited with status {result.returncode}"
        if metadata.get("stderr"):
            message = f"{message}: {metadata['stderr']}"
        raise OcrError("ocr_failed", message)
    if not content:
        raise OcrError(
            "ocr_failed",
            "OCR command returned no text for this image. Use vault_request_download_url "
            "to fetch the image itself.",
        )
    metadata["content"] = content
    return metadata


def _image_metadata(path: Path) -> dict:
    stat = path.stat()
    metadata = {
        "size": stat.st_size,
        "modified": _iso_timestamp(stat.st_mtime),
        "created": _iso_timestamp(stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_ctime),
        "type": "image",
    }
    dimensions = _image_dimensions(path)
    if dimensions:
        metadata["width"], metadata["height"] = dimensions
    return metadata


def _read_image_file(path: Path) -> tuple[str, dict]:
    """Return OCR text for an image, using the same sidecar cache as scanned PDFs.

    The payoff is not only that an agent can read a screenshot note. The sidecar is a
    real file in the vault, so vault_search finds text inside images from then on --
    that keeps paying after the session that generated it is gone.
    """
    metadata = _image_metadata(path)
    sidecar_path = _ocr_sidecar_path(path)

    if config.VAULT_IMAGE_OCR_SIDECAR_ENABLED:
        cached_content = _read_valid_ocr_sidecar(path, sidecar_path)
        if cached_content is not None:
            metadata["content_source"] = "image_ocr_sidecar"
            metadata["ocr"] = {
                "applied": True,
                "engine": "sidecar_cache",
                "sidecar_path": _relative_to_vault_root(sidecar_path),
                "cache_hit": True,
            }
            return cached_content, metadata

        lock_path = _ocr_lock_path(sidecar_path)
        with _OcrSidecarLock(lock_path, config.VAULT_IMAGE_OCR_TIMEOUT * 2):
            # Re-check inside the lock: another reader may have written it while we waited.
            cached_content = _read_valid_ocr_sidecar(path, sidecar_path)
            if cached_content is not None:
                metadata["content_source"] = "image_ocr_sidecar"
                metadata["ocr"] = {
                    "applied": True,
                    "engine": "sidecar_cache",
                    "sidecar_path": _relative_to_vault_root(sidecar_path),
                    "cache_hit": True,
                }
                return cached_content, metadata

            ocr_metadata = _run_image_ocr(path)
            if ocr_metadata is not None:
                metadata["ocr"] = {k: v for k, v in ocr_metadata.items() if k != "content"}
                if ocr_metadata.get("applied") and ocr_metadata.get("content"):
                    bytes_written = _write_ocr_sidecar(path, sidecar_path, ocr_metadata["content"])
                    metadata["ocr"]["sidecar_path"] = _relative_to_vault_root(sidecar_path)
                    metadata["ocr"]["sidecar_bytes"] = bytes_written
                    metadata["ocr"]["cache_hit"] = False
                    metadata["content_source"] = "image_ocr_sidecar"
                    return ocr_metadata["content"], metadata

    ocr_metadata = _run_image_ocr(path)
    if ocr_metadata is not None:
        metadata["ocr"] = {k: v for k, v in ocr_metadata.items() if k != "content"}
        if ocr_metadata.get("applied") and ocr_metadata.get("content"):
            metadata["content_source"] = "image_ocr_fallback"
            return ocr_metadata["content"], metadata

    # _run_image_ocr either returns applied content or raises OcrError, so reaching
    # here means it reported success without text. Belt and braces, and it answers with
    # the same stable error_code as every other OCR failure rather than a bare ValueError.
    raise OcrError(
        "ocr_failed",
        f"Image OCR produced no text for {_relative_to_vault_root(path)}. "
        "Use vault_request_download_url to fetch the image itself.",
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

    if extracted_page_count == 0:
        sidecar_path = _ocr_sidecar_path(path)
        if config.VAULT_PDF_OCR_ENABLED and config.VAULT_PDF_OCR_CMD and config.VAULT_PDF_OCR_SIDECAR_ENABLED:
            cached_content = _read_valid_ocr_sidecar(path, sidecar_path)
            if cached_content is not None:
                metadata["content_source"] = "pdf_ocr_sidecar"
                metadata["ocr"] = {
                    "applied": True,
                    "engine": "sidecar_cache",
                    "sidecar_path": _relative_to_vault_root(sidecar_path),
                    "cache_hit": True,
                }
                return cached_content, metadata

            lock_path = _ocr_lock_path(sidecar_path)
            with _OcrSidecarLock(lock_path, config.VAULT_PDF_OCR_TIMEOUT * 2):
                cached_content = _read_valid_ocr_sidecar(path, sidecar_path)
                if cached_content is not None:
                    metadata["content_source"] = "pdf_ocr_sidecar"
                    metadata["ocr"] = {
                        "applied": True,
                        "engine": "sidecar_cache",
                        "sidecar_path": _relative_to_vault_root(sidecar_path),
                        "cache_hit": True,
                    }
                    return cached_content, metadata

                ocr_metadata = _run_pdf_ocr(path)
                if ocr_metadata is not None:
                    metadata["ocr"] = {k: v for k, v in ocr_metadata.items() if k != "content"}
                    if ocr_metadata.get("applied") and ocr_metadata.get("content"):
                        bytes_written = _write_ocr_sidecar(path, sidecar_path, ocr_metadata["content"])
                        metadata["ocr"]["sidecar_path"] = _relative_to_vault_root(sidecar_path)
                        metadata["ocr"]["sidecar_bytes"] = bytes_written
                        metadata["ocr"]["cache_hit"] = False
                        metadata["content_source"] = "pdf_ocr_sidecar"
                        return ocr_metadata["content"], metadata

        ocr_metadata = _run_pdf_ocr(path)
        if ocr_metadata is not None:
            metadata["ocr"] = {k: v for k, v in ocr_metadata.items() if k != "content"}
            if ocr_metadata.get("applied") and ocr_metadata.get("content"):
                metadata["content_source"] = "pdf_ocr_fallback"
                return ocr_metadata["content"], metadata

    return "\n\n".join(page_texts), metadata


def _run_pdf_ocr(path: Path) -> dict | None:
    """Optionally run an external OCR command for image-only PDFs."""
    if not config.VAULT_PDF_OCR_ENABLED or not config.VAULT_PDF_OCR_CMD:
        return None

    argv = shlex.split(config.VAULT_PDF_OCR_CMD, posix=os.name != "nt")
    if not argv:
        return None
    if any("{path}" in arg for arg in argv):
        argv = [arg.format(path=str(path)) for arg in argv]
    else:
        argv = [*argv, str(path)]

    env = os.environ.copy()
    env["VAULT_PDF_PATH"] = str(path)
    env["VAULT_PDF_OCR_LANGUAGES"] = config.VAULT_PDF_OCR_LANGUAGES

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=config.VAULT_PDF_OCR_TIMEOUT,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        raise OcrError("ocr_tool_unavailable", f"OCR command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise OcrError("ocr_timeout", f"OCR command timed out after {config.VAULT_PDF_OCR_TIMEOUT} seconds")

    content = result.stdout.strip()
    metadata = {
        "applied": result.returncode == 0 and bool(content),
        "engine": "external_command",
        "command": Path(argv[0]).name,
        "languages": config.VAULT_PDF_OCR_LANGUAGES.split("+"),
        "returncode": result.returncode,
    }
    if result.stderr.strip():
        metadata["stderr"] = result.stderr.strip()[:500]
    if result.returncode != 0:
        message = f"OCR command exited with status {result.returncode}"
        if metadata.get("stderr"):
            message = f"{message}: {metadata['stderr']}"
        raise OcrError("ocr_failed", message)
    if not content:
        raise OcrError("ocr_failed", "OCR command returned no text")
    metadata["content"] = content
    return metadata


def read_file(relative_path: str) -> tuple[str, dict]:
    """Read a file and return (content, metadata).

    Metadata keys: size (int), modified (ISO str), created (ISO str).
    """
    path = resolve_vault_path(relative_path)

    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {relative_path}")

    # Before any dispatch: a hardlinked PDF or image would otherwise be handed to OCR
    # and leak just as readily as a hardlinked note.
    refuse_hard_linked_file(path)

    if path.suffix.lower() == ".pdf":
        return _read_pdf_file(path)

    # Images are readable only where OCR is configured. With it off, they fall through
    # to the same rejection as before, so enabling the feature is the only thing that
    # changes behaviour.
    if (
        path.suffix.lower() in IMAGE_OCR_EXTENSIONS
        and config.VAULT_IMAGE_OCR_ENABLED
        and config.VAULT_IMAGE_OCR_CMD
    ):
        return _read_image_file(path)

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

    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        if overwrite:
            os.replace(tmp_path, path)
        else:
            # Atomic no-clobber: os.link fails if the target exists, closing the
            # check-then-write race that exists()-then-os.replace would leave open.
            try:
                os.link(tmp_path, path)
            except FileExistsError:
                raise FileExistsError(f"File already exists: {relative_path}")
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            is_new = True
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
    include_ocr_sidecars: bool = False,
) -> list[dict]:
    """List directory contents recursively up to *depth* levels.

    Returns a list of dicts with keys: name, path (relative to vault),
    type ("file" or "dir"), size, modified.
    """
    depth = min(depth, config.MAX_LIST_DEPTH)
    vault_root = config.VAULT_PATH.resolve()

    if relative_path in {"", "."} and config.INCLUDED_ROOTS != ["."]:
        results: list[dict] = []
        seen: set[str] = set()
        for root in _included_root_paths():
            if not root.exists() or not root.is_dir():
                continue
            if _vault_policy_error(root, vault_root) is not None:
                continue

            rel = _relative_to_vault_root(root)
            if rel in seen:
                continue
            seen.add(rel)

            stat = root.stat()
            results.append({
                "name": root.name,
                "path": rel,
                "type": "dir",
                "size": stat.st_size,
                "modified": _iso_timestamp(stat.st_mtime),
            })
        return results

    root = resolve_vault_path(relative_path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")

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
            if not is_vault_path_allowed(entry):
                continue

            is_dir = entry.is_dir()

            if is_dir and not include_dirs:
                # Still recurse even if we're not listing dirs
                _walk(entry, current_depth + 1)
                continue

            if not is_dir and not include_files:
                continue
            if not is_dir and not include_ocr_sidecars and is_ocr_sidecar_name(entry.name):
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

            rel = _relative_to_vault_root(entry)

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
    vault_root = config.VAULT_PATH.resolve()
    issues: list[dict] = []

    roots = [resolve_vault_path(relative_path)] if relative_path else [
        root for root in _included_root_paths() if root.exists() and root.is_dir()
    ]
    if not roots:
        return issues

    for root in roots:
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {relative_path}")

        for path in root.rglob("*.md"):
            if any(part in config.EXCLUDED_DIRS for part in path.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if not is_vault_path_allowed(path):
                continue
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                issues.append(
                    {
                        "path": _relative_to_vault_root(path),
                        "position": e.start,
                        "reason": e.reason,
                    }
                )
                if len(issues) >= max_results:
                    return issues

    return issues


def repair_markdown_encoding_issues(
    relative_path: str = "",
    max_files: int = 50,
    source_encoding: str = "cp1252",
    dry_run: bool = False,
) -> dict:
    """Repair markdown files that are not valid UTF-8 using a chosen source encoding."""
    vault_root = config.VAULT_PATH.resolve()
    repaired: list[dict] = []
    failed: list[dict] = []

    roots = [resolve_vault_path(relative_path)] if relative_path else [
        root for root in _included_root_paths() if root.exists() and root.is_dir()
    ]
    if not roots:
        return {
            "path_prefix": relative_path,
            "source_encoding": source_encoding,
            "dry_run": dry_run,
            "repaired_count": 0,
            "failed_count": 0,
            "repaired": [],
            "failed": [],
            "truncated": False,
        }

    for root in roots:
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {relative_path}")

        for path in root.rglob("*.md"):
            if any(part in config.EXCLUDED_DIRS for part in path.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if not is_vault_path_allowed(path):
                continue

            raw = path.read_bytes()
            try:
                raw.decode("utf-8")
                continue
            except UnicodeDecodeError:
                pass

            rel = _relative_to_vault_root(path)
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
                return {
                    "path_prefix": relative_path,
                    "source_encoding": source_encoding,
                    "dry_run": dry_run,
                    "repaired_count": len(repaired),
                    "failed_count": len(failed),
                    "repaired": repaired,
                    "failed": failed,
                    "truncated": True,
                }

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
