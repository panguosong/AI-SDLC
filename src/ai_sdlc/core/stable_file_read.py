"""在项目根目录内无跟随、稳定地读取可信输入文件。"""

from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

_READ_SIZE = 64 * 1024
_REPARSE_POINT = 0x400
_IS_WINDOWS = os.name == "nt"


def read_stable_bytes(root: Path, path: Path) -> bytes:
    """读取普通文件，并拒绝越界、链接和读取期间发生的替换。"""

    chunks: list[bytes] = []
    consume_stable_chunks(root, path, chunks.append)
    return b"".join(chunks)


def consume_stable_chunks(
    root: Path,
    path: Path,
    consumer: Callable[[bytes], None],
) -> None:
    """逐块消费稳定普通文件，避免为扫描一次性分配完整文件。"""

    canonical_root, relative = _lexical_relative(root, path)
    if os.name != "nt" and hasattr(os, "O_NOFOLLOW"):
        _consume_posix(canonical_root, relative, consumer)
        return
    _consume_portable(canonical_root, relative, consumer)


def read_stable_text(
    root: Path,
    path: Path,
    *,
    encoding: str = "utf-8",
) -> str:
    return read_stable_bytes(root, path).decode(encoding)


def _stable_regular_file_exists(root: Path, path: Path) -> bool:
    """区分真正缺失的文件，并拒绝链接、重解析点和非普通文件。"""

    canonical_root, relative = _lexical_relative(root, path)
    current = canonical_root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError(
                f"trusted file is unavailable: {relative.as_posix()}"
            ) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE_POINT:
            raise ValueError(f"trusted file uses a symlink: {relative.as_posix()}")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"trusted file must be regular: {relative.as_posix()}")
        if not is_leaf and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"trusted file parent is invalid: {relative.as_posix()}")
    return True


def _lexical_relative(root: Path, path: Path) -> tuple[Path, Path]:
    canonical_root = root.resolve(strict=True)
    candidate = path if path.is_absolute() else canonical_root / path
    try:
        relative = candidate.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError(f"trusted file must stay within project: {path}") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"trusted file path is not canonical: {path}")
    return canonical_root, relative


def _read_posix(root: Path, relative: Path) -> bytes:
    chunks: list[bytes] = []
    _consume_posix(root, relative, chunks.append)
    return b"".join(chunks)


def _consume_posix(
    root: Path,
    relative: Path,
    consumer: Callable[[bytes], None],
) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            _consume_descriptor(file_fd, relative, consumer)
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise ValueError(
            f"trusted file is unavailable or uses a symlink: {relative.as_posix()}"
        ) from exc
    finally:
        os.close(directory_fd)


def _read_descriptor(file_fd: int, relative: Path) -> bytes:
    chunks: list[bytes] = []
    _consume_descriptor(file_fd, relative, chunks.append)
    return b"".join(chunks)


def _consume_descriptor(
    file_fd: int,
    relative: Path,
    consumer: Callable[[bytes], None],
) -> None:
    before = os.fstat(file_fd)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"trusted file must be regular: {relative.as_posix()}")
    content_size = 0
    while chunk := os.read(file_fd, _READ_SIZE):
        consumer(chunk)
        content_size += len(chunk)
    after = os.fstat(file_fd)
    if (
        _stat_identity(before) != _stat_identity(after)
        or content_size != before.st_size
    ):
        raise ValueError(f"trusted file changed while reading: {relative.as_posix()}")


def _read_portable(root: Path, relative: Path) -> bytes:
    chunks: list[bytes] = []
    _consume_portable(root, relative, chunks.append)
    return b"".join(chunks)


def _consume_portable(
    root: Path,
    relative: Path,
    consumer: Callable[[bytes], None],
) -> None:
    candidate = root / relative
    _reject_link_path(root, relative)
    try:
        with candidate.open("rb") as stream:
            _validate_opened_handle(root, candidate, stream.fileno(), relative)
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"trusted file must be regular: {relative.as_posix()}")
            content_size = 0
            while chunk := stream.read(_READ_SIZE):
                consumer(chunk)
                content_size += len(chunk)
            after = os.fstat(stream.fileno())
            _validate_opened_handle(root, candidate, stream.fileno(), relative)
    except OSError as exc:
        raise ValueError(f"trusted file is unavailable: {relative.as_posix()}") from exc
    _reject_link_path(root, relative)
    if (
        _stat_identity(before) != _stat_identity(after)
        or content_size != before.st_size
    ):
        raise ValueError(f"trusted file changed while reading: {relative.as_posix()}")


def _validate_opened_handle(
    root: Path,
    candidate: Path,
    file_descriptor: int,
    relative: Path,
) -> None:
    opened = _opened_file_path(file_descriptor)
    if opened is None:
        if _IS_WINDOWS:
            raise ValueError(
                f"trusted file opened handle is unavailable: {relative.as_posix()}"
            )
        return
    expected = os.path.normcase(os.path.abspath(candidate))
    actual = os.path.normcase(os.path.abspath(opened))
    canonical_root = os.path.normcase(os.path.abspath(root))
    try:
        within_root = os.path.commonpath((canonical_root, actual)) == canonical_root
    except ValueError:
        within_root = False
    if not within_root or actual != expected:
        raise ValueError(
            f"trusted file opened handle escaped project: {relative.as_posix()}"
        )


def _opened_file_path(file_descriptor: int) -> Path | None:
    if not _IS_WINDOWS:
        return None
    try:
        import msvcrt

        get_osfhandle = cast(
            Callable[[int], int],
            msvcrt.__dict__["get_osfhandle"],
        )
        win_dll = cast(Callable[..., Any], ctypes.__dict__["WinDLL"])
        handle = get_osfhandle(file_descriptor)
        kernel32 = win_dll("kernel32", use_last_error=True)
        final_path = kernel32.GetFinalPathNameByHandleW
        final_path.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        final_path.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(
            ctypes.c_void_p(handle),
            buffer,
            len(buffer),
            0,
        )
    except (ImportError, OSError, ValueError):
        return None
    if not length or length >= len(buffer):
        return None
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = f"\\\\{value[8:]}"
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _reject_link_path(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ValueError(
                f"trusted file is unavailable: {relative.as_posix()}"
            ) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE_POINT:
            raise ValueError(f"trusted file uses a symlink: {relative.as_posix()}")


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


__all__ = ["consume_stable_chunks", "read_stable_bytes", "read_stable_text"]
