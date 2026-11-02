from __future__ import annotations

import errno
import json
import os
import socket
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX runtime
    msvcrt = None


class InstanceLockError(RuntimeError):
    """Base error for the process-wide Sayuki instance lock."""


class InstanceLockHeldError(InstanceLockError):
    """Raised when another process already owns the instance lock."""


class InstanceLock:
    """Owns an OS-level exclusive lock for the complete Bot lifetime."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            self._lock_descriptor(descriptor)
        except OSError as exc:
            owner = self._read_owner(descriptor)
            os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                detail = f"（目前持有者：{owner}）" if owner else ""
                raise InstanceLockHeldError(f"已有另一個Bot process正在執行{detail}") from exc
            raise InstanceLockError(f"無法取得Bot單例鎖：{type(exc).__name__}") from exc

        try:
            metadata = json.dumps(
                {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, metadata)
            os.fsync(descriptor)
        except BaseException:
            self._unlock_descriptor(descriptor)
            os.close(descriptor)
            raise

        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            self._unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    @staticmethod
    def _read_owner(descriptor: int) -> str:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            raw = os.read(descriptor, 4096).decode(
                "utf-8",
                errors="replace",
            )
            loaded = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ""
        if not isinstance(loaded, dict):
            return ""
        pid = loaded.get("pid")
        hostname = loaded.get("hostname")
        started_at = loaded.get("started_at")
        parts = []
        if pid is not None:
            parts.append(f"pid={pid}")
        if hostname:
            parts.append(f"host={hostname}")
        if started_at:
            parts.append(f"started_at={started_at}")
        return " ".join(parts)

    @staticmethod
    def _lock_descriptor(descriptor: int) -> None:
        if fcntl is not None:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            return
        if msvcrt is not None:  # pragma: no cover - Windows fallback
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        raise OSError(
            errno.ENOSYS,
            "目前平台不支援process file locking",
        )

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        if msvcrt is not None:  # pragma: no cover - Windows fallback
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


def database_lock_paths(database):
    """Cover legacy sidecars as well as the canonical file spelling."""
    path = Path(database)
    result = {}
    for item in (Path(str(path) + ".lock"), Path(str(path.resolve()) + ".lock")):
        result.setdefault(item.parent.resolve() / item.name, item)
    return list(result.values())


class DatabaseLock:
    """Cooperative lock keyed by SQLite's device/inode, on a separate file.

    Sidecars keep diagnostics and compatibility; the inode identity additionally
    covers reverse symlinks and hard links that share no discoverable pathname.
    Never flock the SQLite file itself: that conflicts with SQLite on macOS.
    All 4.6 writers take this before opening the database, including import.
    """

    def __init__(self, database):
        self.path = Path(database)
        self._sidecars = []
        self._identity = None
        self._created_info = None

    @property
    def acquired(self):
        return self._identity is not None

    @staticmethod
    def _identity_path(info):
        # POSIX processes must agree even when TMPDIR or pathname aliases differ.
        base = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
        uid = os.getuid() if hasattr(os, "getuid") else "local"
        directory = base / f"sayuki-database-locks-{uid}"
        directory.mkdir(mode=0o700, exist_ok=True)
        actual = directory.lstat()
        if (
            not stat.S_ISDIR(actual.st_mode)
            or actual.st_mode & 0o077
            or (hasattr(os, "getuid") and actual.st_uid != os.getuid())
        ):
            raise InstanceLockError("資料庫鎖目錄的擁有者或權限不正確")
        return directory / f"{info.st_dev:x}-{info.st_ino:x}.lock"

    def acquire(self):
        if self.acquired:
            return
        try:
            for path in database_lock_paths(self.path):
                lock = InstanceLock(path)
                lock.acquire()
                self._sidecars.append(lock)
            try:
                descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                self._created_info = os.fstat(descriptor)
            except FileExistsError:
                descriptor = os.open(self.path, os.O_RDWR)
            try:
                identity = InstanceLock(self._identity_path(os.fstat(descriptor)))
                identity.acquire()
                self._identity = identity
            finally:
                os.close(descriptor)
        except BaseException:
            self.release()
            raise

    def release(self):
        failure = None
        created, self._created_info = self._created_info, None
        if created is not None and self._identity is not None:
            try:
                actual = self.path.stat()
                if (actual.st_dev, actual.st_ino, actual.st_size) == (
                    created.st_dev,
                    created.st_ino,
                    0,
                ):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                failure = exc
        identity, self._identity = self._identity, None
        locks = ([identity] if identity is not None else []) + list(reversed(self._sidecars))
        self._sidecars.clear()
        for lock in locks:
            try:
                lock.release()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
