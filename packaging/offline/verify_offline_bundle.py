"""Verify offline bundle runtime invariants before shipping or accepting assets."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
from pathlib import Path, PurePosixPath


def _fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def _load_manifest(bundle_dir: Path) -> dict[str, object]:
    manifest_path = bundle_dir / "bundle-manifest.json"
    if not manifest_path.is_file():
        _fail(f"missing bundle manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_expected_package_version(
    bundle_dir: Path,
    manifest: dict[str, object],
    expected_version: str,
) -> None:
    actual_version = str(manifest.get("package_version") or "").strip()
    if actual_version != expected_version:
        _fail(
            "manifest package version does not match expected release version: "
            f"manifest={actual_version or '<missing>'}, expected={expected_version}"
        )
    expected_name = f"ai-sdlc-offline-{expected_version}"
    if bundle_dir.name != expected_name and not bundle_dir.name.startswith(
        expected_name + "-"
    ):
        _fail(
            "bundle directory does not match expected release version: "
            f"directory={bundle_dir.name}, expected_prefix={expected_name}"
        )
    wheels = sorted((bundle_dir / "wheels").glob("ai_sdlc-*.whl"))
    if len(wheels) != 1:
        _fail(
            "bundle must contain exactly one AI-SDLC wheel for release binding: "
            f"found={len(wheels)}"
        )
    if not wheels[0].name.startswith(f"ai_sdlc-{expected_version}-"):
        _fail(
            "AI-SDLC wheel version does not match expected release version: "
            f"wheel={wheels[0].name}, expected={expected_version}"
        )


def _assert_bundle_checksums(bundle_dir: Path) -> None:
    checksums_path = bundle_dir / "SHA256SUMS"
    if not checksums_path.is_file():
        _fail(f"missing bundle checksum manifest: {checksums_path}")
    expected: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        checksums_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line:
            continue
        parts = raw_line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            _fail(f"malformed SHA256SUMS line {line_number}")
        relative = PurePosixPath(parts[1])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            _fail(f"unsafe SHA256SUMS path on line {line_number}: {parts[1]}")
        relative_text = relative.as_posix()
        if relative_text == "SHA256SUMS" or relative_text in expected:
            _fail(f"duplicate or recursive SHA256SUMS entry: {relative_text}")
        candidate = bundle_dir.joinpath(*relative.parts)
        try:
            candidate.resolve().relative_to(bundle_dir.resolve())
        except ValueError:
            _fail(f"SHA256SUMS entry escapes bundle: {relative_text}")
        if not candidate.is_file():
            _fail(f"SHA256SUMS entry is missing from bundle: {relative_text}")
        expected[relative_text] = parts[0].lower()

    actual_files = {
        path.relative_to(bundle_dir).as_posix()
        for path in bundle_dir.rglob("*")
        if path.is_file() and path != checksums_path
    }
    expected_files = set(expected)
    if expected_files != actual_files:
        missing = sorted(actual_files - expected_files)
        unexpected = sorted(expected_files - actual_files)
        _fail(
            "SHA256SUMS file coverage mismatch: "
            f"missing_entries={missing}, unexpected_entries={unexpected}"
        )
    for relative_text in sorted(expected):
        actual_digest = _sha256(bundle_dir / relative_text)
        if not hmac.compare_digest(expected[relative_text], actual_digest):
            _fail(f"checksum mismatch for bundle file: {relative_text}")


def _assert_archive_checksum(archive_path: Path, checksum_path: Path) -> None:
    if not archive_path.is_file():
        _fail(f"archive does not exist: {archive_path}")
    if not checksum_path.is_file():
        _fail(f"archive checksum sidecar does not exist: {checksum_path}")
    lines = [
        line
        for line in checksum_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if len(lines) != 1:
        _fail(f"archive checksum sidecar must contain exactly one entry: {checksum_path}")
    parts = lines[0].split("  ", 1)
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        _fail(f"malformed archive checksum sidecar: {checksum_path}")
    if parts[1] != archive_path.name:
        _fail(
            "archive checksum sidecar filename does not match archive: "
            f"sidecar={parts[1]}, archive={archive_path.name}"
        )
    actual_digest = _sha256(archive_path)
    if not hmac.compare_digest(parts[0].lower(), actual_digest):
        _fail(f"archive checksum mismatch: {archive_path}")


def _runtime_candidates(runtime_root: Path) -> list[Path]:
    candidates: list[Path] = []
    candidates.append(runtime_root / "python.exe")
    candidates.append(runtime_root / "bin" / "python3")
    candidates.append(runtime_root / "bin" / "python")
    bin_dir = runtime_root / "bin"
    if bin_dir.is_dir():
        candidates.extend(sorted(bin_dir.glob("python3.*")))
        candidates.extend(sorted(bin_dir.glob("python*")))
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _pick_runtime_python(runtime_root: Path) -> Path:
    for candidate in _runtime_candidates(runtime_root):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    _fail(
        "python-runtime is bundled but has no executable Python entry "
        "(expected python-runtime/python.exe or python-runtime/bin/python*)"
    )


def _assert_no_escaping_symlinks(runtime_root: Path) -> None:
    if runtime_root.is_symlink():
        _fail(
            "python-runtime itself is a symlink; copy a real runtime directory "
            "into the bundle instead"
        )
    root_resolved = runtime_root.resolve()
    for path in runtime_root.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            _fail(
                "python-runtime contains a symlink that points outside the bundle: "
                f"{path.relative_to(runtime_root)} -> {os.readlink(path)}"
            )


def _assert_runtime_executes(runtime_python: Path, expected_version: str | None) -> None:
    probe = (
        "import sys, venv; "
        "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    )
    completed = subprocess.run(
        [str(runtime_python), "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        _fail(
            "bundled Python runtime is not executable or cannot import venv: "
            + (completed.stderr.strip() or completed.stdout.strip() or "no output")
        )
    actual_version = completed.stdout.strip().splitlines()[-1]
    if expected_version and actual_version != expected_version:
        _fail(
            "bundled Python runtime version does not match offline wheel ABI: "
            f"runtime={actual_version}, wheels={expected_version}"
        )


def _native_binary_kind(path: Path) -> str | None:
    try:
        header = path.read_bytes()[:4]
    except OSError:
        return None
    if header == b"\x7fELF":
        return "elf"
    if header[:2] == b"MZ":
        return "pe"
    if header in {
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
    }:
        return "macho"
    return None


def _reject_build_host_dependency(dep: str, runtime_root: Path, *, system: str) -> None:
    dep = dep.strip()
    if not dep or dep.startswith("@"):
        return
    if not dep.startswith("/"):
        return

    if system == "darwin" and (
        dep.startswith("/usr/lib/")
        or dep.startswith("/System/Library/")
    ):
        return
    if system == "linux" and (
        dep.startswith("/lib/")
        or dep.startswith("/lib64/")
        or dep.startswith("/usr/lib/")
        or dep.startswith("/usr/lib64/")
    ):
        return

    dep_path = Path(dep).resolve()
    try:
        dep_path.relative_to(runtime_root.resolve())
    except ValueError:
        inside_runtime = False
    else:
        inside_runtime = True

    if inside_runtime and system == "linux":
        return

    if inside_runtime:
        _fail(
            "bundled Python runtime has an absolute dependency inside the bundle. "
            "Use a relocatable runtime that references bundled libraries via "
            f"@loader_path/@rpath or relative loader config: {dep}"
        )

    _fail(
        "bundled Python runtime depends on a build-host absolute path and is not "
        f"portable: {dep}"
    )


def _assert_macos_dynamic_dependencies(runtime_python: Path, runtime_root: Path) -> None:
    completed = subprocess.run(
        ["otool", "-L", str(runtime_python)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        _fail(
            "could not inspect bundled Python macOS dependencies with otool: "
            + (completed.stderr.strip() or completed.stdout.strip() or "no output")
        )
    for raw_line in completed.stdout.splitlines()[1:]:
        dep = raw_line.strip().split(" (", 1)[0]
        _reject_build_host_dependency(dep, runtime_root, system="darwin")


def _assert_linux_dynamic_dependencies(runtime_python: Path, runtime_root: Path) -> None:
    completed = subprocess.run(
        ["ldd", str(runtime_python)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        _fail(
            "could not inspect bundled Python Linux dependencies with ldd: "
            + (output.strip() or "no output")
        )
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "not found" in line:
            _fail(f"bundled Python runtime has an unresolved Linux dependency: {line}")
        if "=>" in line:
            dep = line.split("=>", 1)[1].strip().split(" ", 1)[0]
        else:
            dep = line.split(" ", 1)[0]
        _reject_build_host_dependency(dep, runtime_root, system="linux")


def _assert_windows_runtime_dlls(runtime_root: Path, expected_version: str | None) -> None:
    if not expected_version:
        return
    major_minor = expected_version.replace(".", "")
    dll_name = f"python{major_minor}.dll"
    if not (runtime_root / dll_name).is_file() and not any(runtime_root.rglob(dll_name)):
        _fail(
            "bundled Windows Python runtime is missing its Python DLL "
            f"({dll_name}); copy the full redistributable runtime, not only python.exe"
        )


def _assert_portable_dynamic_dependencies(
    runtime_python: Path,
    runtime_root: Path,
    expected_version: str | None,
) -> None:
    system = platform.system().lower()
    binary_kind = _native_binary_kind(runtime_python)
    if system == "darwin" and binary_kind == "macho":
        _assert_macos_dynamic_dependencies(runtime_python, runtime_root)
    elif system == "linux" and binary_kind == "elf":
        _assert_linux_dynamic_dependencies(runtime_python, runtime_root)
    elif system == "windows" and binary_kind == "pe":
        _assert_windows_runtime_dlls(runtime_root, expected_version)


def _assert_install_log_used_bundled_runtime(log_path: Path) -> None:
    text = log_path.read_text(encoding="utf-8")
    if "Using bundled Python runtime" not in text:
        _fail(f"install log did not prove bundled runtime use: {log_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--require-bundled-runtime", action="store_true")
    parser.add_argument("--require-checksums", action="store_true")
    parser.add_argument("--expected-package-version")
    parser.add_argument(
        "--archive-checksum",
        action="append",
        nargs=2,
        metavar=("ARCHIVE", "CHECKSUM"),
        default=[],
    )
    parser.add_argument("--install-log", type=Path)
    args = parser.parse_args(argv)

    bundle_dir = args.bundle_dir.resolve()
    manifest = _load_manifest(bundle_dir)
    if args.expected_package_version:
        _assert_expected_package_version(
            bundle_dir,
            manifest,
            args.expected_package_version,
        )
    if args.require_checksums:
        _assert_bundle_checksums(bundle_dir)
    for archive, checksum in args.archive_checksum:
        _assert_archive_checksum(Path(archive).resolve(), Path(checksum).resolve())
    runtime_root = bundle_dir / "python-runtime"
    bundled = bool(manifest.get("python_runtime_bundled"))

    if args.require_bundled_runtime and not bundled:
        _fail("bundle manifest does not mark python_runtime_bundled=true")

    if bundled:
        if not runtime_root.is_dir():
            _fail("bundle manifest marks python_runtime_bundled=true but python-runtime/ is missing")
        runtime_python = _pick_runtime_python(runtime_root)
        _assert_no_escaping_symlinks(runtime_root)
        expected_version = str(manifest.get("wheel_python_version") or "").strip() or None
        _assert_runtime_executes(runtime_python, expected_version)
        _assert_portable_dynamic_dependencies(runtime_python, runtime_root, expected_version)
    elif runtime_root.exists():
        _fail("python-runtime/ exists but bundle manifest does not mark it as bundled")

    if args.install_log is not None:
        _assert_install_log_used_bundled_runtime(args.install_log)

    print("Offline bundle runtime verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
