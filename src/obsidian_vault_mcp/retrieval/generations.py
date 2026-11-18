"""Index generations: one complete, verified semantic cache per directory.

Until 0.19.0 the cache was five files written one after the other under fixed names. A
process killed between two writes left a mix: on 16.11.2026 the OOM killer ended a run
after faiss.index (99,134 vectors) and before chunks.json (99,015 chunks), and the next
run threw away every vector and embedded the whole vault again. Now:

    <cache>/CURRENT                      id of the generation readers use, replaced atomically
    <cache>/generations/<id>/            one complete generation; generation.json is its seal
    <cache>/generations/.staging-<id>/   a generation being written; never read
    <cache>/.writer.lock                 held by the one process that writes

A writer builds the whole generation in a staging directory, syncs it, verifies it like a
reader would, renames it into place and only then replaces CURRENT. A reader resolves
CURRENT once and reads every file from that one directory. Whatever moment a writer dies
at, CURRENT names a complete generation or nothing new.

Standard library only: the operator's integrity check and start guard load this file by
path with the system Python, without the server's virtualenv.
"""

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import time
from contextlib import contextmanager
from pathlib import Path

try:  # Linux, where the server runs
    import fcntl
except ImportError:  # pragma: no cover - the Windows dev machine
    fcntl = None

FORMAT = 2
POINTER = "CURRENT"
GENERATIONS = "generations"
LOCK = ".writer.lock"
SEAL = "generation.json"
VECTORS = "faiss.index"
CHUNKS = "chunks.jsonl"
PATH_INDEX = "path_index.json"
MANIFEST = "manifest.json"
DATA_FILES = (VECTORS, CHUNKS, PATH_INDEX, MANIFEST)

# A FAISS IndexFlatIP as faiss.write_index stores it: "IxFI", d (int32), ntotal (int64),
# two dummy int64, is_trained (uint8), metric (int32, 0 = inner product), then the vector
# data as a length-prefixed float array (uint64 count of floats, then the floats).
_FAISS_MAGIC = b"IxFI"
_FAISS_DUMMY = 1 << 20
FAISS_HEADER_SIZE = 45

_ID_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")
_BLOCK = 1 << 20

# Test hook: called with a step name at every point where a writer can die. Tests set it
# in a child process to SIGKILL that process at one step; nothing in production sets it.
_fault_hook = None


def _checkpoint(step: str) -> None:
    if _fault_hook is not None:
        _fault_hook(step)


class GenerationError(ValueError):
    """A generation, the pointer to it, or a legacy cache fails verification."""


class WriterBusy(RuntimeError):
    """Another process holds the writer lock."""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_generation_id() -> str:
    """Sortable by creation time: UTC timestamp to the microsecond, then random hex."""
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def generations_dir(cache_dir) -> Path:
    return Path(cache_dir) / GENERATIONS


def generation_dir(cache_dir, generation_id: str) -> Path:
    if not _ID_RE.match(generation_id or ""):
        raise GenerationError(f"invalid generation id {generation_id!r}")
    return generations_dir(cache_dir) / generation_id


def read_pointer(cache_dir) -> str | None:
    """The id CURRENT names, None without CURRENT; GenerationError if it names nothing valid."""
    path = Path(cache_dir) / POINTER
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise GenerationError(f"{POINTER} unreadable: {type(exc).__name__}") from exc
    generation_id = raw.strip()
    if not _ID_RE.match(generation_id):
        raise GenerationError(f"{POINTER} does not name a generation")
    return generation_id


def read_seal(gen_dir) -> dict:
    try:
        seal = json.loads((Path(gen_dir) / SEAL).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GenerationError("generation has no seal") from exc
    except (OSError, ValueError) as exc:
        raise GenerationError(f"seal unreadable: {type(exc).__name__}") from exc
    if not isinstance(seal, dict) or seal.get("format") != FORMAT:
        raise GenerationError(f"seal is not format {FORMAT}")
    return seal


def ancestors(cache_dir, generation_id: str, depth: int) -> list[str]:
    """The ids reachable from `generation_id` by its seal's parent links, nearest first."""
    found: list[str] = []
    current = generation_id
    for _ in range(depth):
        try:
            parent = read_seal(generation_dir(cache_dir, current)).get("parent")
        except GenerationError:
            break
        if not isinstance(parent, str) or not _ID_RE.match(parent) or parent in found:
            break
        found.append(parent)
        current = parent
    return found


# --- the vector file ---------------------------------------------------------------------


def faiss_header(dimension: int, ntotal: int) -> bytes:
    return (
        _FAISS_MAGIC
        + struct.pack("<iqqq", dimension, ntotal, _FAISS_DUMMY, _FAISS_DUMMY)
        + struct.pack("<Bi", 1, 0)
        + struct.pack("<Q", ntotal * dimension)
    )


def read_faiss_header(path) -> tuple[int, int]:
    """(dimension, ntotal) of a flat inner-product index file, checked against its size."""
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(FAISS_HEADER_SIZE)
    if len(header) != FAISS_HEADER_SIZE or header[:4] != _FAISS_MAGIC:
        raise GenerationError("vector file is not a flat inner-product FAISS index")
    dimension, ntotal, _a, _b = struct.unpack("<iqqq", header[4:32])
    trained, metric = struct.unpack("<Bi", header[32:37])
    (floats,) = struct.unpack("<Q", header[37:45])
    if not 0 < dimension <= 65536 or ntotal < 0 or trained != 1 or metric != 0 or floats != ntotal * dimension:
        raise GenerationError("vector file header is inconsistent")
    if path.stat().st_size != FAISS_HEADER_SIZE + floats * 4:
        raise GenerationError("vector file size does not match its header")
    return dimension, ntotal


class VectorWriter:
    """Writes a FAISS IndexFlatIP file block by block, without holding the index in memory."""

    def __init__(self, path):
        self.path = Path(path)
        self._stream = self.path.open("wb")
        self._stream.write(bytes(FAISS_HEADER_SIZE))
        self.dimension: int | None = None
        self.count = 0

    def add(self, block, rows: int, dimension: int) -> None:
        """Append `rows` vectors of `dimension` float32 values (any C-contiguous buffer)."""
        if self.dimension is None:
            self.dimension = dimension
        elif dimension != self.dimension:
            raise GenerationError(f"vector dimension {dimension} differs from {self.dimension}")
        view = memoryview(block).cast("B")
        if len(view) != rows * dimension * 4:
            raise GenerationError("vector block size does not match its shape")
        self._stream.write(view)
        self.count += rows

    def close(self, dimension: int | None = None) -> None:
        dim = self.dimension or dimension
        if not dim:
            raise GenerationError("vector dimension unknown")
        self._stream.seek(0)
        self._stream.write(faiss_header(dim, self.count))
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self.dimension = dim

    def abort(self) -> None:
        self._stream.close()


class ChunkWriter:
    """chunks.jsonl, one chunk per line in index-row order; never the whole list as one string."""

    def __init__(self, path):
        self.path = Path(path)
        self._stream = self.path.open("wb")
        self._ids = hashlib.sha256()
        self.count = 0

    def add(self, item: dict) -> None:
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._stream.write(line.encode("utf-8"))
        self._ids.update(item["id"].encode("utf-8") + b"\n")
        self.count += 1

    def close(self) -> str:
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        return self._ids.hexdigest()

    def abort(self) -> None:
        self._stream.close()


def write_json(path, payload) -> None:
    with Path(path).open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


def file_digest(path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while block := stream.read(_BLOCK):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


# --- verification ------------------------------------------------------------------------


class Verified:
    """What a successful verification read: the seal, the path index and the manifest."""

    def __init__(self, seal: dict, path_index: dict, manifest: dict):
        self.seal = seal
        self.path_index = path_index
        self.manifest = manifest

    def facts(self) -> dict:
        seal = self.seal
        return {
            "generation": seal["generation_id"],
            "created_utc": seal.get("created_utc"),
            "chunks": seal["chunk_count"],
            "files": seal["file_count"],
            "empty_files": seal["empty_files"],
            "dimension": seal["dimension"],
            "embedder": seal.get("embedder"),
        }


def _load_json_checked(path, expected: dict, deep: bool):
    raw = Path(path).read_bytes()
    if len(raw) != expected["size"] or (deep and hashlib.sha256(raw).hexdigest() != expected["sha256"]):
        raise GenerationError(f"{Path(path).name} does not match the seal")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise GenerationError(f"{Path(path).name} is not valid JSON") from exc
    del raw
    return payload


def _check_seal_shape(seal: dict, gen_dir: Path) -> None:
    if seal.get("generation_id") != gen_dir.name:
        raise GenerationError("seal names another generation")
    for key in ("chunk_count", "file_count", "empty_files", "dimension"):
        if not isinstance(seal.get(key), int) or isinstance(seal.get(key), bool) or seal[key] < 0:
            raise GenerationError(f"seal field {key} is invalid")
    if not isinstance(seal.get("chunk_ids_sha256"), str) or not isinstance(seal.get("embedder"), dict):
        raise GenerationError("seal lacks the chunk-id digest or the embedder")
    files = seal.get("files")
    if not isinstance(files, dict) or set(files) != set(DATA_FILES):
        raise GenerationError("seal does not list exactly the data files")
    for name, entry in files.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("size"), int) or not isinstance(entry.get("sha256"), str):
            raise GenerationError(f"seal entry for {name} is invalid")


def verify(gen_dir, deep: bool = True, on_chunk=None) -> Verified:
    """Check a generation the way a reader relies on it; raise GenerationError otherwise.

    Shallow: seal, file sizes, vector header, path index against manifest. Deep, in
    addition: every file's SHA-256, and chunks.jsonl line by line: unique ids, their
    ordered digest, each chunk's path and source hash against path index and manifest.
    `on_chunk(item)` sees every chunk dict in row order during the deep pass, so a
    loader builds its chunks while verifying, in one read.
    """
    gen_dir = Path(gen_dir)
    seal = read_seal(gen_dir)
    _check_seal_shape(seal, gen_dir)
    files = seal["files"]
    for name in DATA_FILES:
        try:
            size = (gen_dir / name).stat().st_size
        except OSError as exc:
            raise GenerationError(f"{name} missing") from exc
        if size != files[name]["size"]:
            raise GenerationError(f"{name} size does not match the seal")
    dimension, ntotal = read_faiss_header(gen_dir / VECTORS)
    if (dimension, ntotal) != (seal["dimension"], seal["chunk_count"]):
        raise GenerationError(
            f"vector file holds {ntotal} x {dimension}, the seal {seal['chunk_count']} x {seal['dimension']}"
        )
    path_index = _load_json_checked(gen_dir / PATH_INDEX, files[PATH_INDEX], deep)
    manifest = _load_json_checked(gen_dir / MANIFEST, files[MANIFEST], deep)
    if not isinstance(path_index, dict) or not isinstance(manifest, dict):
        raise GenerationError("path index or manifest is not an object")
    if set(path_index) != set(manifest):
        raise GenerationError("path index and manifest list different files")
    expected_ids = 0
    empty = 0
    for path, ids in path_index.items():
        if not isinstance(ids, list) or not isinstance(manifest[path], str):
            raise GenerationError("path index or manifest entry has the wrong type")
        expected_ids += len(ids)
        empty += not ids
    if (len(path_index), empty, expected_ids) != (seal["file_count"], seal["empty_files"], seal["chunk_count"]):
        raise GenerationError("path index does not match the seal's counts")
    if deep:
        size, digest = file_digest(gen_dir / VECTORS)
        if digest != files[VECTORS]["sha256"]:
            raise GenerationError(f"{VECTORS} does not match the seal")
        _verify_chunks(gen_dir / CHUNKS, files[CHUNKS], seal, path_index, manifest, on_chunk)
    return Verified(seal, path_index, manifest)


def _verify_chunks(path, expected, seal, path_index, manifest, on_chunk) -> None:
    file_hash = hashlib.sha256()
    ids_hash = hashlib.sha256()
    seen: set[str] = set()
    position: dict[str, int] = {}
    count = 0
    with Path(path).open("rb") as stream:
        for line in stream:
            file_hash.update(line)
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise GenerationError(f"{CHUNKS} line {count + 1} is not valid JSON") from exc
            if not isinstance(item, dict):
                raise GenerationError(f"{CHUNKS} line {count + 1} is not an object")
            chunk_id, chunk_path = item.get("id"), item.get("path")
            if not isinstance(chunk_id, str) or not isinstance(chunk_path, str) or not isinstance(item.get("text"), str):
                raise GenerationError(f"{CHUNKS} line {count + 1} lacks id, path or text")
            if chunk_id in seen:
                raise GenerationError(f"{CHUNKS} repeats a chunk id")
            ids = path_index.get(chunk_path)
            n = position.get(chunk_path, 0)
            if ids is None or n >= len(ids) or ids[n] != chunk_id:
                raise GenerationError(f"{CHUNKS} line {count + 1} is not where the path index puts it")
            if item.get("source_hash") != manifest[chunk_path]:
                raise GenerationError(f"{CHUNKS} line {count + 1} belongs to another version of its file")
            position[chunk_path] = n + 1
            seen.add(chunk_id)
            ids_hash.update(chunk_id.encode("utf-8") + b"\n")
            count += 1
            if on_chunk is not None:
                on_chunk(item)
    if file_hash.hexdigest() != expected["sha256"]:
        raise GenerationError(f"{CHUNKS} does not match the seal")
    if count != seal["chunk_count"] or ids_hash.hexdigest() != seal["chunk_ids_sha256"]:
        raise GenerationError(f"{CHUNKS} holds other chunks than the seal")
    if any(position.get(path, 0) != len(ids) for path, ids in path_index.items()):
        raise GenerationError("path index lists chunks that chunks.jsonl lacks")


# --- locks -------------------------------------------------------------------------------


@contextmanager
def writer_lock(cache_dir, timeout: float = 0.0):
    """Hold the cache's writer lock; wait up to `timeout` seconds, then raise WriterBusy."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stream = (cache_dir / LOCK).open("a+")
    try:
        if fcntl is not None:
            deadline = time.monotonic() + max(timeout, 0.0)
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WriterBusy("another semantic index writer is running") from None
                    time.sleep(0.2)
        yield
    finally:
        stream.close()


@contextmanager
def reader_lock(gen_dir):
    """Keep a generation from being cleaned up while it is read: a shared lock on its seal."""
    try:
        stream = (Path(gen_dir) / SEAL).open("rb")
    except FileNotFoundError as exc:
        raise GenerationError("generation has no seal") from exc
    try:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        yield
    finally:
        stream.close()


# --- publishing and cleanup ----------------------------------------------------------------


def _fsync_dir(path) -> None:
    if os.name != "posix":  # pragma: no cover - directories cannot be opened on Windows
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def staging_dir(cache_dir, generation_id: str) -> Path:
    path = generations_dir(cache_dir) / f".staging-{generation_id}"
    path.mkdir(parents=True)
    return path


def seal_generation(staging: Path, seal: dict) -> None:
    """Hash the data files into the seal and write it last."""
    seal = dict(seal)
    seal["files"] = {}
    for name in DATA_FILES:
        size, digest = file_digest(staging / name)
        seal["files"][name] = {"size": size, "sha256": digest}
    write_json(staging / SEAL, seal)
    _fsync_dir(staging)


def place(cache_dir, staging: Path, generation_id: str) -> Path:
    """Rename a sealed staging directory to its generation id. Nothing reads it yet:
    readers only follow CURRENT and its parent links."""
    final = generation_dir(cache_dir, generation_id)
    os.rename(staging, final)
    _fsync_dir(generations_dir(cache_dir))
    _checkpoint("renamed")
    return final


def point(cache_dir, generation_id: str) -> None:
    """Replace CURRENT atomically: readers see the old id or the new one, never a mix."""
    cache_dir = Path(cache_dir)
    tmp = cache_dir / f".{POINTER}.tmp-{generation_id}"
    with tmp.open("w", encoding="ascii") as stream:
        stream.write(generation_id + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _checkpoint("pointer-written")
    os.replace(tmp, cache_dir / POINTER)
    _fsync_dir(cache_dir)
    _checkpoint("published")


def discard(path) -> None:
    """Remove an unpublished generation or staging directory after a failed write."""
    shutil.rmtree(path, ignore_errors=True)


def read_pointer_or_none(cache_dir) -> str | None:
    try:
        return read_pointer(cache_dir)
    except GenerationError:
        return None


def list_generations(cache_dir) -> list[str]:
    """Ids of the generation directories present, newest first."""
    root = generations_dir(cache_dir)
    if not root.is_dir():
        return []
    return sorted((entry.name for entry in root.iterdir() if _ID_RE.match(entry.name)), reverse=True)


def cleanup(cache_dir, keep_ancestors: int = 1) -> list[str]:
    """Remove what no reader can need. The caller holds the writer lock.

    Kept: CURRENT and its nearest `keep_ancestors` by parent link, the last generations a
    reader falls back to. Removed: other generations nobody is reading (a reader holds a
    shared lock on the seal), staging directories and pointer temp files of dead writers.
    """
    cache_dir = Path(cache_dir)
    removed: list[str] = []
    try:
        current = read_pointer(cache_dir)
    except GenerationError:
        return removed  # never guess what to keep
    keep = set()
    if current:
        keep = {current, *ancestors(cache_dir, current, keep_ancestors)}
    for tmp in cache_dir.glob(f".{POINTER}.tmp-*"):
        tmp.unlink(missing_ok=True)
        removed.append(tmp.name)
    root = generations_dir(cache_dir)
    if not root.is_dir():
        return removed
    for entry in sorted(root.iterdir()):
        if entry.name in keep or not entry.is_dir():
            continue
        if entry.name.startswith(".staging-"):
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(entry.name)
            continue
        if not _ID_RE.match(entry.name) or not current:
            continue
        if _try_remove_unread(entry):
            removed.append(entry.name)
    return removed


def _try_remove_unread(gen_dir: Path) -> bool:
    """Remove a generation unless a reader holds its seal; True if it is gone."""
    if fcntl is None:  # pragma: no cover - Windows cannot delete an open file
        shutil.rmtree(gen_dir, ignore_errors=True)
        return not gen_dir.exists()
    try:
        stream = (gen_dir / SEAL).open("rb")
    except FileNotFoundError:
        shutil.rmtree(gen_dir, ignore_errors=True)
        return not gen_dir.exists()
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        # Holding the lock while deleting: a reader that opens the seal now finds it gone.
        shutil.rmtree(gen_dir, ignore_errors=True)
        return not gen_dir.exists()
    finally:
        stream.close()


# --- for the operator's checks --------------------------------------------------------------


def check_current(cache_dir, deep: bool = True) -> dict:
    """Integrity facts of the generation CURRENT names, for monitoring; never raises."""
    started = time.monotonic()
    facts: dict = {"ok": False, "scope": "generation", "format": FORMAT}
    try:
        generation_id = read_pointer(cache_dir)
        if generation_id is None:
            raise GenerationError(f"{POINTER} missing")
        gen_dir = generation_dir(cache_dir, generation_id)
        with reader_lock(gen_dir):
            verified = verify(gen_dir, deep=deep)
        facts.update(verified.facts())
        facts.update(
            ok=True,
            deep=deep,
            message=f"Generation {generation_id} vollständig geprüft: {verified.seal['chunk_count']} Textabschnitte"
            if deep else f"Generation {generation_id} strukturell geprüft: {verified.seal['chunk_count']} Textabschnitte",
        )
    except (GenerationError, OSError) as exc:
        facts["message"] = str(exc) if isinstance(exc, GenerationError) else f"Generation nicht lesbar: {type(exc).__name__}"
    facts["duration_seconds"] = round(time.monotonic() - started, 3)
    return facts
