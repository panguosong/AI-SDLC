"""实现阶段共用的窄资源锁；沿用既有锁键和跨进程语义。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class _ImplementationWriteLockError(RuntimeError):
    """实现循环写操作无法取得互斥锁。"""


@contextmanager
def _stage_write_guard(root: Path, loop_type: str, loop_id: str) -> Iterator[None]:
    """各阶段共用原锁实现；Implementation 键保持兼容，其他阶段互不串扰。"""

    key = loop_id if loop_type == "implementation" else f"{loop_type}:{loop_id}"
    with _implementation_write_guard(root, key):
        yield


@contextmanager
def _implementation_write_guard(root: Path, loop_id: str) -> Iterator[None]:
    """跨进程串行化同一实现循环的读取、校验与写入。"""

    file_descriptor = -1
    try:
        lock_dir = _implementation_lock_dir(root)
        lock_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_key = f"{root.resolve()}\0{loop_id}".encode()
        lock_name = hashlib.sha256(lock_key).hexdigest()
        lock_path = lock_dir / f"implementation-{lock_name}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        file_descriptor = os.open(lock_path, flags, 0o600)
        _acquire_implementation_file_lock(file_descriptor)
    except OSError as exc:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        raise _ImplementationWriteLockError(
            f"Implementation loop write lock is unavailable: {loop_id}."
        ) from exc
    try:
        yield
    finally:
        _release_implementation_file_lock(file_descriptor)
        os.close(file_descriptor)


def _implementation_lock_dir(root: Path) -> Path:
    git_marker = root / ".git"
    if git_marker.is_dir():
        return git_marker / "ai-sdlc-locks"
    if git_marker.is_file():
        try:
            marker = git_marker.read_text(encoding="utf-8").strip()
        except OSError:
            marker = ""
        if marker.lower().startswith("gitdir:"):
            value = marker.split(":", 1)[1].strip()
            git_dir = Path(value)
            if not git_dir.is_absolute():
                git_dir = root / git_dir
            return git_dir.resolve() / "ai-sdlc-locks"
    if hasattr(os, "getuid"):
        user_key = str(os.getuid())
    else:  # pragma: no cover - Windows temp directories are already user-scoped
        user_key = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"ai-sdlc-loop-locks-{user_key}"


def _acquire_implementation_file_lock(file_descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - Windows CI exercises this branch
        import msvcrt

        if os.fstat(file_descriptor).st_size == 0:
            os.write(file_descriptor, b"\0")
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt.__dict__["locking"](
            file_descriptor,
            msvcrt.__dict__["LK_LOCK"],
            1,
        )
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_EX)


def _release_implementation_file_lock(file_descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - Windows CI exercises this branch
        import msvcrt

        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt.__dict__["locking"](
            file_descriptor,
            msvcrt.__dict__["LK_UNLCK"],
            1,
        )
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_UN)
