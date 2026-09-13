"""可信文件读取的路径身份回归测试。"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_sdlc.core import stable_file_read


def test_portable_read_rejects_opened_handle_outside_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / "trusted.json"
    candidate.write_text('{"trusted": true}', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text('{"trusted": false}', encoding="utf-8")
    original_open = Path.open

    def swapped_open(path: Path, *args, **kwargs):
        target = outside if path == candidate else path
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swapped_open)
    monkeypatch.setattr(
        stable_file_read,
        "_opened_file_path",
        lambda _file_descriptor: outside,
        raising=False,
    )

    with pytest.raises(ValueError, match="opened handle"):
        stable_file_read._read_portable(root, Path("trusted.json"))


def test_windows_opened_handle_keeps_full_pointer_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FinalPath:
        argtypes: tuple[object, ...] = ()
        restype: object = None

        def __call__(self, *args: object) -> int:
            calls.append(args)
            return 0

    final_path = FinalPath()
    kernel32 = SimpleNamespace(GetFinalPathNameByHandleW=final_path)
    handle = 2**48 + 17
    monkeypatch.setattr(stable_file_read, "_IS_WINDOWS", True)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(get_osfhandle=lambda _descriptor: handle),
    )
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )

    assert stable_file_read._opened_file_path(7) is None
    assert final_path.argtypes[0] is ctypes.c_void_p
    assert isinstance(calls[0][0], ctypes.c_void_p)
    assert calls[0][0].value == handle
