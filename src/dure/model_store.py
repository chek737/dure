from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .artifact_manifest import require_sha256_digest


DURE_MODEL_STORE_ROOT = Path("/var/lib/dure/model-store")
DURE_MODEL_CACHE_ROOT = Path("/var/lib/dure/models")
ATTEMPT_JOURNAL_SCHEMA_VERSION = 1
MAX_ATTEMPT_JOURNAL_BYTES = 16 * 1024
MAX_TRACKED_BYTES = (1 << 63) - 1
HASH_BUFFER_BYTES = 1024 * 1024

ATTEMPT_STATUSES = frozenset(
    {
        "PREPARING",
        "DOWNLOADING",
        "ASSEMBLING",
        "VERIFYING",
        "ACTIVATING",
        "SUCCEEDED",
        "FAILED",
    }
)
MODEL_STORE_FAILURE_CODES = frozenset(
    {
        "MODEL_STORE_INVALID",
        "MODEL_STORE_ROOT_UNSAFE",
        "MODEL_STORE_PATH_COLLISION",
        "MODEL_STORE_LOCK_BUSY",
        "MODEL_STORE_JOURNAL_CORRUPT",
        "MODEL_STORE_CHUNK_COLLISION",
        "MODEL_STORE_CHUNK_CORRUPT",
        "MODEL_STORE_IO_FAILED",
        "MODEL_STORE_DISK_INSUFFICIENT",
        "MODEL_STORE_DOWNLOAD_TIMEOUT",
        "MODEL_STORE_DOWNLOAD_INTERRUPTED",
        "MODEL_STORE_DOWNLOAD_REJECTED",
        "MODEL_STORE_DIGEST_MISMATCH",
    }
)
_JOURNAL_KEYS = frozenset(
    {
        "schema_version",
        "manifest_digest",
        "chunk_digest",
        "bytes_complete",
        "status",
        "failure_code",
    }
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class ModelStoreError(RuntimeError):
    _SAFE_MESSAGES = {
        "MODEL_STORE_INVALID": "model store input is invalid",
        "MODEL_STORE_ROOT_UNSAFE": "model store root is unsafe",
        "MODEL_STORE_PATH_COLLISION": "model store path collision detected",
        "MODEL_STORE_LOCK_BUSY": "model store lock is busy",
        "MODEL_STORE_JOURNAL_CORRUPT": "model store attempt journal is corrupt",
        "MODEL_STORE_CHUNK_COLLISION": "model store chunk path collision detected",
        "MODEL_STORE_CHUNK_CORRUPT": "model store chunk failed integrity validation",
        "MODEL_STORE_IO_FAILED": "model store I/O failed",
        "MODEL_STORE_DISK_INSUFFICIENT": "model store has insufficient disk space",
        "MODEL_STORE_DOWNLOAD_TIMEOUT": "model store download timed out",
        "MODEL_STORE_DOWNLOAD_INTERRUPTED": "model store download was interrupted",
        "MODEL_STORE_DOWNLOAD_REJECTED": "model store download response was rejected",
        "MODEL_STORE_DIGEST_MISMATCH": "model store content digest did not match",
    }

    def __init__(self, code: str) -> None:
        if code not in MODEL_STORE_FAILURE_CODES:
            raise ValueError("unsupported model store failure code")
        self.code = code
        self.failure_code = code
        super().__init__(self._SAFE_MESSAGES[code])


@dataclass(frozen=True)
class AttemptJournal:
    manifest_digest: str
    chunk_digest: str | None
    bytes_complete: int
    status: str
    failure_code: str | None = None

    def __post_init__(self) -> None:
        try:
            require_sha256_digest(self.manifest_digest, field="manifest_digest")
            if self.chunk_digest is not None:
                require_sha256_digest(self.chunk_digest, field="chunk_digest")
        except ValueError as exc:
            raise ModelStoreError("MODEL_STORE_INVALID") from exc
        if (
            type(self.bytes_complete) is not int
            or not 0 <= self.bytes_complete <= MAX_TRACKED_BYTES
            or type(self.status) is not str
            or self.status not in ATTEMPT_STATUSES
            or (
                self.failure_code is not None
                and (
                    type(self.failure_code) is not str
                    or self.failure_code not in MODEL_STORE_FAILURE_CODES
                )
            )
        ):
            raise ModelStoreError("MODEL_STORE_INVALID")
        if (self.status == "FAILED") != (self.failure_code is not None):
            raise ModelStoreError("MODEL_STORE_INVALID")

    def to_dict(self) -> dict:
        return {
            "schema_version": ATTEMPT_JOURNAL_SCHEMA_VERSION,
            "manifest_digest": self.manifest_digest,
            "chunk_digest": self.chunk_digest,
            "bytes_complete": self.bytes_complete,
            "status": self.status,
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_dict(cls, value: object) -> "AttemptJournal":
        if (
            type(value) is not dict
            or any(type(key) is not str for key in value)
            or set(value) != _JOURNAL_KEYS
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != ATTEMPT_JOURNAL_SCHEMA_VERSION
        ):
            raise ModelStoreError("MODEL_STORE_JOURNAL_CORRUPT")
        try:
            return cls(
                manifest_digest=value["manifest_digest"],
                chunk_digest=value["chunk_digest"],
                bytes_complete=value["bytes_complete"],
                status=value["status"],
                failure_code=value["failure_code"],
            )
        except ModelStoreError as exc:
            raise ModelStoreError("MODEL_STORE_JOURNAL_CORRUPT") from exc


def _digest_hex(digest: object, *, field: str = "digest") -> str:
    try:
        normalized = require_sha256_digest(digest, field=field)
    except ValueError as exc:
        raise ModelStoreError("MODEL_STORE_INVALID") from exc
    return normalized.removeprefix("sha256:")


def _normalized_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise ModelStoreError("MODEL_STORE_ROOT_UNSAFE")
    return Path(os.path.abspath(path))


def _reject_symlink_ancestors(path: Path) -> None:
    normalized = _normalized_absolute(path)
    for candidate in reversed((normalized, *normalized.parents)):
        try:
            observed = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_ROOT_UNSAFE") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise ModelStoreError("MODEL_STORE_ROOT_UNSAFE")


def _assert_safe_directory(path: Path, *, root: bool = False) -> None:
    try:
        observed = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ModelStoreError("MODEL_STORE_ROOT_UNSAFE") from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or resolved != _normalized_absolute(path)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & 0o022
    ):
        code = "MODEL_STORE_ROOT_UNSAFE" if root else "MODEL_STORE_PATH_COLLISION"
        raise ModelStoreError(code)


def _ensure_safe_directory(path: Path, *, root: bool = False) -> None:
    _reject_symlink_ancestors(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        code = "MODEL_STORE_ROOT_UNSAFE" if root else "MODEL_STORE_IO_FAILED"
        raise ModelStoreError(code) from exc
    _assert_safe_directory(path, root=root)


def _fsync_directory(path: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW)
        os.fsync(descriptor)
    except OSError as exc:
        raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class ContentAddressedModelStore:
    """Dure-owned content-addressed chunk state.

    Root overrides exist for unit tests and local embedding only.  Task payloads
    must never be allowed to populate either root.
    """

    def __init__(
        self,
        *,
        store_root: Path = DURE_MODEL_STORE_ROOT,
        model_root: Path = DURE_MODEL_CACHE_ROOT,
    ) -> None:
        self.store_root = _normalized_absolute(Path(store_root))
        self.model_root = _normalized_absolute(Path(model_root))
        self.chunk_root = self.store_root / "chunks" / "sha256"
        self.artifact_lock_root = self.store_root / "locks" / "artifacts"
        self.chunk_lock_root = self.store_root / "locks" / "chunks"
        self.attempt_root = self.store_root / "attempts"

    def initialize(self) -> None:
        _ensure_safe_directory(self.store_root, root=True)
        for path in (
            self.chunk_root,
            self.artifact_lock_root,
            self.chunk_lock_root,
            self.attempt_root,
        ):
            _ensure_safe_directory(path)

    def chunk_path(self, digest: str) -> Path:
        hexadecimal = _digest_hex(digest, field="chunk_digest")
        return self.chunk_root / hexadecimal[:2] / hexadecimal

    def chunk_partial_path(self, digest: str) -> Path:
        path = self.chunk_path(digest)
        return path.with_name(f"{path.name}.part")

    def ensure_chunk_directory(self, digest: str) -> Path:
        self.initialize()
        directory = self.chunk_path(digest).parent
        _ensure_safe_directory(directory)
        return directory

    def _lock_path(self, kind: str, digest: str) -> Path:
        hexadecimal = _digest_hex(digest)
        if kind == "artifact":
            return self.artifact_lock_root / f"{hexadecimal}.lock"
        if kind == "chunk":
            return self.chunk_lock_root / f"{hexadecimal}.lock"
        raise ModelStoreError("MODEL_STORE_INVALID")

    @contextmanager
    def _lock(
        self,
        kind: str,
        digest: str,
        *,
        blocking: bool,
    ) -> Iterator[Path]:
        self.initialize()
        path = self._lock_path(kind, digest)
        descriptor = -1
        acquired = False
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | _CLOEXEC | _NOFOLLOW,
                0o600,
            )
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or observed.st_nlink != 1
                or observed.st_mode & 0o077
            ):
                raise ModelStoreError("MODEL_STORE_PATH_COLLISION")
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as exc:
                raise ModelStoreError("MODEL_STORE_LOCK_BUSY") from exc
            acquired = True
            yield path
        except ModelStoreError:
            raise
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR}:
                raise ModelStoreError("MODEL_STORE_PATH_COLLISION") from exc
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        finally:
            if descriptor >= 0:
                if acquired:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(descriptor)

    def artifact_lock(
        self, manifest_digest: str, *, blocking: bool = True
    ) -> Iterator[Path]:
        return self._lock("artifact", manifest_digest, blocking=blocking)

    def chunk_lock(
        self, chunk_digest: str, *, blocking: bool = True
    ) -> Iterator[Path]:
        return self._lock("chunk", chunk_digest, blocking=blocking)

    def _verified_chunk_without_lock(
        self,
        chunk_digest: str,
        expected_size: int,
        *,
        allowed_link_counts: frozenset[int] = frozenset({1}),
    ) -> Path | None:
        if (
            type(expected_size) is not int
            or not 1 <= expected_size <= MAX_TRACKED_BYTES
        ):
            raise ModelStoreError("MODEL_STORE_INVALID")
        path = self.chunk_path(chunk_digest)
        try:
            parent_state = path.parent.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        if not stat.S_ISDIR(parent_state.st_mode):
            raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION")
        try:
            _assert_safe_directory(path.parent)
        except ModelStoreError as exc:
            raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION") from exc
        try:
            path_state = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        if path_state.st_nlink == 2 and allowed_link_counts == frozenset({1}):
            partial = self.chunk_partial_path(chunk_digest)
            try:
                partial_state = partial.lstat()
            except FileNotFoundError:
                partial_state = None
            except OSError as exc:
                raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
            if (
                partial_state is not None
                and stat.S_ISREG(partial_state.st_mode)
                and partial_state.st_dev == path_state.st_dev
                and partial_state.st_ino == path_state.st_ino
                and partial_state.st_nlink == 2
            ):
                self._verified_chunk_without_lock(
                    chunk_digest,
                    expected_size,
                    allowed_link_counts=frozenset({2}),
                )
                self._unlink_verified_partial(partial)
                return self._verified_chunk_without_lock(
                    chunk_digest, expected_size
                )
        if (
            not stat.S_ISREG(path_state.st_mode)
            or path_state.st_uid != os.geteuid()
            or path_state.st_nlink not in allowed_link_counts
            or path_state.st_mode & 0o022
            or path_state.st_size != expected_size
        ):
            raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION")

        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_dev != path_state.st_dev
                or before.st_ino != path_state.st_ino
                or before.st_uid != os.geteuid()
                or before.st_nlink not in allowed_link_counts
                or before.st_mode & 0o022
                or before.st_size != expected_size
            ):
                raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, HASH_BUFFER_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
        except ModelStoreError:
            raise
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR}:
                raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION") from exc
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        observed_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity != observed_after:
            raise ModelStoreError("MODEL_STORE_CHUNK_CORRUPT")
        if digest.hexdigest() != _digest_hex(chunk_digest, field="chunk_digest"):
            raise ModelStoreError("MODEL_STORE_CHUNK_CORRUPT")
        return path

    def verified_chunk(
        self,
        chunk_digest: str,
        expected_size: int,
        *,
        blocking: bool = True,
    ) -> Path | None:
        with self.chunk_lock(chunk_digest, blocking=blocking):
            return self._verified_chunk_without_lock(chunk_digest, expected_size)

    def open_chunk_partial(
        self, chunk_digest: str, expected_size: int
    ) -> tuple[Path, int, int]:
        if (
            type(expected_size) is not int
            or not 1 <= expected_size <= MAX_TRACKED_BYTES
        ):
            raise ModelStoreError("MODEL_STORE_INVALID")
        self.ensure_chunk_directory(chunk_digest)
        path = self.chunk_partial_path(chunk_digest)
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | _CLOEXEC | _NOFOLLOW,
                0o600,
            )
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or observed.st_nlink != 1
                or observed.st_mode & 0o077
                or observed.st_size > expected_size
            ):
                raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION")
            return path, descriptor, observed.st_size
        except ModelStoreError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR}:
                raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION") from exc
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc

    def _unlink_verified_partial(self, path: Path) -> None:
        try:
            path.unlink()
            _fsync_directory(path.parent)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc

    def publish_chunk_partial(
        self, chunk_digest: str, expected_size: int
    ) -> Path:
        """Publish an already fsynced and verified ``.part`` without overwrite.

        The caller must hold the matching chunk lock.  This method revalidates
        both paths so a collision is preserved for operator inspection.
        """

        partial = self.chunk_partial_path(chunk_digest)
        final = self.chunk_path(chunk_digest)
        try:
            partial_state = partial.lstat()
        except FileNotFoundError as exc:
            raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION") from exc
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        if (
            not stat.S_ISREG(partial_state.st_mode)
            or partial_state.st_uid != os.geteuid()
            or partial_state.st_nlink not in {1, 2}
            or partial_state.st_mode & 0o077
            or partial_state.st_size != expected_size
        ):
            raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION")

        try:
            final_state = final.lstat()
        except FileNotFoundError:
            final_state = None
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc

        if final_state is not None:
            same_publication = (
                stat.S_ISREG(final_state.st_mode)
                and final_state.st_dev == partial_state.st_dev
                and final_state.st_ino == partial_state.st_ino
                and final_state.st_nlink == partial_state.st_nlink == 2
            )
            if same_publication:
                self._verified_chunk_without_lock(
                    chunk_digest,
                    expected_size,
                    allowed_link_counts=frozenset({2}),
                )
                self._unlink_verified_partial(partial)
                verified = self._verified_chunk_without_lock(
                    chunk_digest, expected_size
                )
                if verified is None:  # pragma: no cover - lock excludes Dure races
                    raise ModelStoreError("MODEL_STORE_IO_FAILED")
                return verified

            if partial_state.st_nlink != 1:
                raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION")

            verified = self._verified_chunk_without_lock(
                chunk_digest, expected_size
            )
            if verified is None:  # pragma: no cover - lstat observed it above
                raise ModelStoreError("MODEL_STORE_IO_FAILED")
            self._unlink_verified_partial(partial)
            return verified

        if partial_state.st_nlink != 1:
            raise ModelStoreError("MODEL_STORE_CHUNK_COLLISION")

        try:
            os.link(partial, final, follow_symlinks=False)
            _fsync_directory(final.parent)
        except FileExistsError:
            verified = self._verified_chunk_without_lock(
                chunk_digest, expected_size
            )
            if verified is None:  # pragma: no cover - EEXIST guarantees an entry
                raise ModelStoreError("MODEL_STORE_IO_FAILED")
            self._unlink_verified_partial(partial)
            return verified
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc

        self._unlink_verified_partial(partial)
        verified = self._verified_chunk_without_lock(chunk_digest, expected_size)
        if verified is None:  # pragma: no cover - published under the chunk lock
            raise ModelStoreError("MODEL_STORE_IO_FAILED")
        return verified

    def attempt_journal_path(self, manifest_digest: str) -> Path:
        hexadecimal = _digest_hex(manifest_digest, field="manifest_digest")
        return self.attempt_root / hexadecimal / "journal.json"

    def _attempt_directory(self, manifest_digest: str) -> Path:
        self.initialize()
        path = self.attempt_journal_path(manifest_digest).parent
        _ensure_safe_directory(path)
        return path

    def read_attempt(self, manifest_digest: str) -> AttemptJournal | None:
        directory = self._attempt_directory(manifest_digest)
        path = directory / "journal.json"
        try:
            observed = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
            or observed.st_mode & 0o077
            or observed.st_size <= 0
            or observed.st_size > MAX_ATTEMPT_JOURNAL_BYTES
        ):
            raise ModelStoreError("MODEL_STORE_JOURNAL_CORRUPT")

        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
            payload = os.read(descriptor, MAX_ATTEMPT_JOURNAL_BYTES + 1)
            if os.read(descriptor, 1) or len(payload) > MAX_ATTEMPT_JOURNAL_BYTES:
                raise ModelStoreError("MODEL_STORE_JOURNAL_CORRUPT")

            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate JSON key")
                    value[key] = item
                return value

            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=unique_object,
            )
        except ModelStoreError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelStoreError("MODEL_STORE_JOURNAL_CORRUPT") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        journal = AttemptJournal.from_dict(value)
        if journal.manifest_digest != manifest_digest:
            raise ModelStoreError("MODEL_STORE_JOURNAL_CORRUPT")
        return journal

    def write_attempt(self, journal: AttemptJournal) -> Path:
        if type(journal) is not AttemptJournal:
            raise ModelStoreError("MODEL_STORE_INVALID")
        directory = self._attempt_directory(journal.manifest_digest)
        path = directory / "journal.json"
        try:
            path_state = path.lstat()
        except FileNotFoundError:
            path_state = None
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        if path_state is not None:
            if (
                not stat.S_ISREG(path_state.st_mode)
                or path_state.st_uid != os.geteuid()
                or path_state.st_nlink != 1
                or path_state.st_mode & 0o077
            ):
                raise ModelStoreError("MODEL_STORE_PATH_COLLISION")
            self.read_attempt(journal.manifest_digest)

        payload = (
            json.dumps(
                journal.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_ATTEMPT_JOURNAL_BYTES:
            raise ModelStoreError("MODEL_STORE_INVALID")
        temporary = directory / f".journal.{secrets.token_hex(8)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
                0o600,
            )
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short journal write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            _fsync_directory(directory)
        except ModelStoreError:
            raise
        except OSError as exc:
            raise ModelStoreError("MODEL_STORE_IO_FAILED") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return path
