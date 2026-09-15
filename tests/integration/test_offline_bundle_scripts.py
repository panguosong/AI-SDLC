"""Integration tests for offline bundle packaging scripts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OFFLINE_DIR = _REPO_ROOT / "packaging" / "offline"
_PACKAGING_DIR = _REPO_ROOT / "packaging"
_DEBIAN12_OS_RELEASE = """PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
VERSION="12 (bookworm)"
VERSION_CODENAME=bookworm
ID=debian
HOME_URL="https://www.debian.org/"
SUPPORT_URL="https://www.debian.org/support"
BUG_REPORT_URL="https://bugs.debian.org/"
"""
_UBUNTU2204_OS_RELEASE = """PRETTY_NAME="Ubuntu 22.04.5 LTS"
NAME="Ubuntu"
VERSION_ID="22.04"
VERSION="22.04.5 LTS (Jammy Jellyfish)"
ID=ubuntu
ID_LIKE=debian
HOME_URL="https://www.ubuntu.com/"
SUPPORT_URL="https://help.ubuntu.com/"
BUG_REPORT_URL="https://bugs.launchpad.net/ubuntu/"
PRIVACY_POLICY_URL="https://www.ubuntu.com/legal/terms-and-policies/privacy-policy"
UBUNTU_CODENAME=jammy
LOGO=ubuntu-logo
"""


def test_release_checklist_matches_the_standard_release_workflow() -> None:
    checklist = (_OFFLINE_DIR / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

    assert "`v3.2.1`" in checklist
    assert "`upload_to_release`" in checklist
    assert "PR1 的三个发布开关" not in checklist
    assert "v1.0.4 上传与发布动作保持禁止" not in checklist


def test_online_installers_default_to_the_exact_public_git_tag(tmp_path: Path) -> None:
    expected = "git+https://github.com/panguosong/AI-SDLC.git@v3.2.1"
    powershell = (_PACKAGING_DIR / "install_online.ps1").read_text(encoding="utf-8")
    script_path = tmp_path / "install_online.sh"
    shutil.copy2(_PACKAGING_DIR / "install_online.sh", script_path)
    script_path.chmod(0o755)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    _make_fake_git(wrapper_dir)
    fake_python = _make_fake_python(wrapper_dir)
    _make_path_alias(fake_python, wrapper_dir / "python3.11")
    install_log = tmp_path / "pip-install.log"
    env = dict(os.environ)
    _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env["FAKE_PIP_INSTALL_LOG"] = str(install_log)

    assert expected in powershell
    assert "ai-sdlc==" not in powershell

    result = subprocess.run(
        [_bash_command(), str(script_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected in install_log.read_text(encoding="utf-8")
    assert "ai-sdlc==" not in install_log.read_text(encoding="utf-8")


def _load_verify_offline_bundle_module():
    spec = importlib.util.spec_from_file_location(
        "verify_offline_bundle",
        _OFFLINE_DIR / "verify_offline_bundle.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _find_cygpath_command() -> str | None:
    cygpath = shutil.which("cygpath")
    if cygpath:
        return cygpath
    bash = shutil.which("bash")
    if not bash:
        return None
    git_bin = Path(bash).resolve().parent
    for candidate_dir in (git_bin, git_bin.parent / "usr" / "bin"):
        for name in ("cygpath.exe", "cygpath"):
            candidate = candidate_dir / name
            if candidate.is_file():
                return str(candidate)
    return None


def _bash_shebang_python() -> str:
    if os.name != "nt":
        return sys.executable
    cygpath = _find_cygpath_command()
    if not cygpath:
        return sys.executable
    result = subprocess.run(
        [cygpath, "-u", sys.executable],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else sys.executable


def _bash_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    cygpath = _find_cygpath_command()
    if not cygpath:
        return str(path)
    result = subprocess.run(
        [cygpath, "-u", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else str(path)


def _bash_wrapper_path(wrapper_dir: Path) -> str:
    if os.name != "nt":
        return str(wrapper_dir)
    bash = shutil.which("bash")
    git_paths: list[str] = []
    if bash:
        git_bin = Path(bash).resolve().parent
        git_paths = [str(git_bin.parent / "usr" / "bin"), str(git_bin)]
    return os.pathsep.join([str(wrapper_dir), *git_paths])


def test_cygpath_discovery_falls_back_to_the_git_bash_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_root = tmp_path / "Git"
    bash = git_root / "bin" / "bash.exe"
    cygpath = git_root / "usr" / "bin" / "cygpath.exe"
    bash.parent.mkdir(parents=True)
    cygpath.parent.mkdir(parents=True)
    bash.touch()
    cygpath.touch()

    def fake_which(command: str) -> str | None:
        if command == "bash":
            return str(bash)
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert _find_cygpath_command() == str(cygpath)


def test_cygpath_discovery_uses_the_git_bash_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_usr_bin = tmp_path / "Git" / "usr" / "bin"
    bash = git_usr_bin / "bash.exe"
    cygpath = git_usr_bin / "cygpath.exe"
    git_usr_bin.mkdir(parents=True)
    bash.touch()
    cygpath.touch()

    def fake_which(command: str) -> str | None:
        if command == "bash":
            return str(bash)
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert _find_cygpath_command() == str(cygpath)


def _bash_command() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    pytest.skip("bash is required to execute POSIX shell installer tests")


def _set_env_path(env: dict[str, str], value: str) -> None:
    for key in list(env):
        if key.lower() == "path":
            env.pop(key)
    env["PATH"] = value


def _set_bash_wrapper_env(env: dict[str, str], wrapper_dir: Path, cwd: Path) -> None:
    _set_env_path(env, _bash_wrapper_path(wrapper_dir))
    if os.name == "nt":
        bash_env = cwd / ".test-bash-env"
        bash_env.write_text(
            f'export PATH="{_bash_path(wrapper_dir)}:/usr/bin:/bin"\n',
            encoding="utf-8",
        )
        env["BASH_ENV"] = _bash_path(bash_env)


def _make_fake_python(wrapper_dir: Path) -> Path:
    wrapper_path = wrapper_dir / "fake-python"
    real_python = sys.executable
    shebang_python = _bash_shebang_python()
    wrapper = f"""#!{shebang_python}
import os
import shutil
import subprocess
import sys
from pathlib import Path

REAL_PYTHON = {real_python!r}


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


args = sys.argv[1:]
if len(args) >= 2 and args[0] == "-c" and "sys.version_info" in args[1]:
    if "print(" in args[1]:
        print(f"{sys.version_info.major}.{sys.version_info.minor}")
    raise SystemExit(0)
if args[:3] == ["-m", "pip", "--version"]:
    print("pip 24.0 from fake-python")
    raise SystemExit(0)
if args[:3] == ["-m", "pip", "download"]:
    dest = Path(args[args.index("-d") + 1])
    dest.mkdir(parents=True, exist_ok=True)
    wheel = Path(args[-1])
    shutil.copy2(wheel, dest / wheel.name)
    raise SystemExit(0)
if args[:2] == ["-m", "venv"]:
    target = Path(args[2])
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write(
        bin_dir / "activate",
        f'VIRTUAL_ENV="{{target}}"\\nexport VIRTUAL_ENV\\nPATH="{{bin_dir}}:$PATH"\\nexport PATH\\n',
    )
    _write(
        bin_dir / "python",
        f'''#!{shebang_python}
from pathlib import Path
import sys
import os
if sys.argv[1:4] == ["-m", "pip", "install"]:
    if log_path := os.environ.get("FAKE_PIP_INSTALL_LOG"):
        Path(log_path).write_text(" ".join(sys.argv[4:]), encoding="utf-8")
    cli = Path(__file__).resolve().parent / "ai-sdlc"
    cli.write_text("#!/usr/bin/env bash\\\\necho ai-sdlc stub\\\\n", encoding="utf-8")
    cli.chmod(0o755)
    raise SystemExit(0)
raise SystemExit(0)
''',
    )
    _write(
        bin_dir / "pip",
        f'''#!{shebang_python}
from pathlib import Path
import sys

if len(sys.argv) >= 2 and sys.argv[1] == "install":
    cli = Path(__file__).resolve().parent / "ai-sdlc"
    cli.write_text("#!/usr/bin/env bash\\\\necho ai-sdlc stub\\\\n", encoding="utf-8")
    cli.chmod(0o755)
    raise SystemExit(0)
raise SystemExit(0)
''',
    )
    raise SystemExit(0)

completed = subprocess.run([REAL_PYTHON, *sys.argv[1:]], check=False)
raise SystemExit(completed.returncode)
"""
    return _write_executable(wrapper_path, wrapper)


def _make_fake_git(wrapper_dir: Path) -> Path:
    return _write_executable(
        wrapper_dir / "git",
        f"""#!{_bash_shebang_python()}
print("git version 2.49.0")
""",
    )


def _make_fake_portable_python(runtime_dir: Path) -> Path:
    bin_dir = runtime_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = bin_dir / "python3"
    real_python = sys.executable
    shebang_python = _bash_shebang_python()
    wrapper = f"""#!{shebang_python}
import os
import sys
from pathlib import Path

REAL_PYTHON = {real_python!r}


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


args = sys.argv[1:]
if args[:2] == ["-m", "venv"]:
    target = Path(args[2])
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write(
        bin_dir / "activate",
        f'VIRTUAL_ENV="{{target}}"\\nexport VIRTUAL_ENV\\nPATH="{{bin_dir}}:$PATH"\\nexport PATH\\n',
    )
    _write(
        bin_dir / "python",
        f'''#!{shebang_python}
from pathlib import Path
import sys
if sys.argv[1:4] == ["-m", "pip", "install"]:
    cli = Path(__file__).resolve().parent / "ai-sdlc"
    cli.write_text("#!/usr/bin/env bash\\\\necho ai-sdlc stub\\\\n", encoding="utf-8")
    cli.chmod(0o755)
raise SystemExit(0)
''',
    )
    _write(
        bin_dir / "pip",
        f'''#!{shebang_python}
from pathlib import Path
import sys

if len(sys.argv) >= 2 and sys.argv[1] == "install":
    cli = Path(__file__).resolve().parent / "ai-sdlc"
    cli.write_text("#!/usr/bin/env bash\\\\necho ai-sdlc stub\\\\n", encoding="utf-8")
    cli.chmod(0o755)
    raise SystemExit(0)
raise SystemExit(0)
''',
    )
    raise SystemExit(0)

os.execv(REAL_PYTHON, [REAL_PYTHON, *sys.argv[1:]])
"""
    return _write_executable(wrapper_path, wrapper)


def _make_verifiable_portable_python(runtime_dir: Path) -> Path:
    if os.name == "nt":
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target = runtime_dir / "python.exe"
        shutil.copy2(sys.executable, target)
        dll_name = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
        dll_search_roots = [
            Path(sys.executable).parent,
            Path(getattr(sys, "_base_executable", sys.executable)).parent,
            Path(sys.base_prefix),
            Path(sys.exec_prefix),
        ]
        for root in dll_search_roots:
            python_dll = root / dll_name
            if python_dll.is_file():
                shutil.copy2(python_dll, runtime_dir / python_dll.name)
                break
        pyvenv_cfg = Path(sys.executable).resolve().parents[1] / "pyvenv.cfg"
        if pyvenv_cfg.is_file():
            shutil.copy2(pyvenv_cfg, runtime_dir / "pyvenv.cfg")
        return target
    return _make_fake_portable_python(runtime_dir)


def _make_fake_portable_python_versioned(runtime_dir: Path) -> Path:
    bin_dir = runtime_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = bin_dir / "python3.11"
    real_python = sys.executable
    shebang_python = _bash_shebang_python()
    wrapper = f"""#!{shebang_python}
import os
import sys
from pathlib import Path

REAL_PYTHON = {real_python!r}


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


args = sys.argv[1:]
if args[:2] == ["-m", "venv"]:
    target = Path(args[2])
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write(
        bin_dir / "activate",
        f'VIRTUAL_ENV="{{target}}"\\nexport VIRTUAL_ENV\\nPATH="{{bin_dir}}:$PATH"\\nexport PATH\\n',
    )
    _write(
        bin_dir / "python",
        f'''#!{shebang_python}
from pathlib import Path
import sys
if sys.argv[1:4] == ["-m", "pip", "install"]:
    cli = Path(__file__).resolve().parent / "ai-sdlc"
    cli.write_text("#!/usr/bin/env bash\\\\necho ai-sdlc stub\\\\n", encoding="utf-8")
    cli.chmod(0o755)
raise SystemExit(0)
''',
    )
    _write(
        bin_dir / "pip",
        f'''#!{shebang_python}
from pathlib import Path
import sys

if len(sys.argv) >= 2 and sys.argv[1] == "install":
    cli = Path(__file__).resolve().parent / "ai-sdlc"
    cli.write_text("#!/usr/bin/env bash\\\\necho ai-sdlc stub\\\\n", encoding="utf-8")
    cli.chmod(0o755)
    raise SystemExit(0)
raise SystemExit(0)
''',
    )
    raise SystemExit(0)

os.execv(REAL_PYTHON, [REAL_PYTHON, *sys.argv[1:]])
"""
    return _write_executable(wrapper_path, wrapper)


def _make_fake_uv(wrapper_dir: Path) -> Path:
    uv_path = wrapper_dir / "uv"
    content = """#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "build" ]]; then
  echo "unsupported fake uv command: $*" >&2
  exit 1
fi

VERSION="$(awk -F'"' '/^version =/ {print $2; exit}' pyproject.toml)"
mkdir -p dist
printf 'fake wheel\\n' > "dist/ai_sdlc-${VERSION}-py3-none-any.whl"
"""
    return _write_executable(uv_path, content)


def _prepare_fake_bundle_repo(tmp_path: Path, version: str = "1.2.0") -> Path:
    repo = tmp_path / "offline-repo"
    offline_dir = repo / "packaging" / "offline"
    offline_dir.mkdir(parents=True)
    for name in (
        "build_offline_bundle.sh",
        "install_offline.sh",
        "install_offline.ps1",
        "install_offline.bat",
        "verify_offline_bundle.py",
        "README_BUNDLE.txt",
    ):
        shutil.copy2(_OFFLINE_DIR / name, offline_dir / name)
    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "ai_sdlc"
            version = "{version}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return repo


def _script_env(wrapper_dir: Path, fake_python: Path) -> dict[str, str]:
    env = dict(os.environ)
    wrapper_path = str(wrapper_dir) if os.name == "nt" else _bash_path(wrapper_dir)
    _set_env_path(env, os.pathsep.join([wrapper_path, os.environ.get("PATH", "")]))
    env["PYTHON"] = _bash_path(fake_python)
    return env


def _make_path_alias(source: Path, target: Path) -> Path:
    shutil.copy2(source, target)
    target.chmod(0o755)
    return target


def _write_basic_bundle(bundle_dir: Path, version: str = "1.2.0") -> None:
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / f"ai_sdlc-{version}-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": version,
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
            }
        ),
        encoding="utf-8",
    )


def _make_upgrade_existing_python(
    bin_dir: Path,
    version: str = "1.2.0",
    *,
    cli_version: str | None = None,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = bin_dir / "python"
    shebang_python = _bash_shebang_python()
    emitted_cli_version = cli_version or version
    wrapper = f"""#!{shebang_python}
from pathlib import Path
import sys

BIN_DIR = Path({str(bin_dir)!r})
VERSION = {version!r}
CLI_VERSION = {emitted_cli_version!r}
MARKER = BIN_DIR / "installed-version.txt"


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


args = sys.argv[1:]
if len(args) >= 2 and args[0] == "-c" and "sys.version_info" in args[1]:
    raise SystemExit(0)
if len(args) >= 2 and args[0] == "-c" and "importlib.metadata" in args[1]:
    print(MARKER.read_text(encoding="utf-8").strip() if MARKER.exists() else "1.1.0")
    raise SystemExit(0)
if len(args) >= 3 and args[:3] == ["-m", "pip", "install"]:
    MARKER.write_text(VERSION, encoding="utf-8")
    _write(
        BIN_DIR / "ai-sdlc",
        f'''#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
  echo "{{CLI_VERSION}}"
  exit 0
fi
if [[ "$1" == "self-update" && "$2" == "install" && "$3" == "--help" ]]; then
  echo "self-update install help"
  exit 0
fi
echo "ai-sdlc {{VERSION}}"
''',
    )
    raise SystemExit(0)
raise SystemExit(0)
"""
    return _write_executable(wrapper_path, wrapper)


def test_build_offline_bundle_emits_platform_manifest_and_archives(
    tmp_path: Path,
) -> None:
    repo = _prepare_fake_bundle_repo(tmp_path)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_fake_uv(wrapper_dir)

    result = subprocess.run(
        [
            _bash_command(),
            str(repo / "packaging" / "offline" / "build_offline_bundle.sh"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_script_env(wrapper_dir, fake_python),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Offline bundle runtime verification passed." in result.stdout
    bundle_root = repo / "dist-offline" / "ai-sdlc-offline-1.2.0"
    manifest_path = bundle_root / "bundle-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["package_version"] == "1.2.0"
    assert manifest["platform_os"]
    assert manifest["platform_machine"]
    assert manifest["wheel_python_version"]
    assert manifest["wheel_python_tag"].startswith("cp")
    assert manifest["supported_python_versions"] == [manifest["wheel_python_version"]]
    assert manifest["supported_wheel_python_tags"] == [manifest["wheel_python_tag"]]
    checksums_path = bundle_root / "SHA256SUMS"
    assert checksums_path.is_file()
    assert "bundle-manifest.json" in checksums_path.read_text(encoding="utf-8")

    tar_path = repo / "dist-offline" / "ai-sdlc-offline-1.2.0.tar.gz"
    zip_path = repo / "dist-offline" / "ai-sdlc-offline-1.2.0.zip"
    assert tar_path.is_file()
    assert zip_path.is_file()
    with tarfile.open(tar_path, "r:gz") as archive:
        assert "ai-sdlc-offline-1.2.0/bundle-manifest.json" in archive.getnames()
    with zipfile.ZipFile(zip_path) as archive:
        assert "ai-sdlc-offline-1.2.0/bundle-manifest.json" in archive.namelist()
        assert "ai-sdlc-offline-1.2.0/SHA256SUMS" in archive.namelist()
    for archive_path in (tar_path, zip_path):
        sidecar = archive_path.with_name(archive_path.name + ".sha256")
        assert sidecar.is_file()
        digest, filename = sidecar.read_text(encoding="utf-8").strip().split(maxsplit=1)
        assert filename == archive_path.name
        assert digest == hashlib.sha256(archive_path.read_bytes()).hexdigest()


def test_build_offline_bundle_rejects_release_tag_version_mismatch(
    tmp_path: Path,
) -> None:
    repo = _prepare_fake_bundle_repo(tmp_path)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_fake_uv(wrapper_dir)
    env = _script_env(wrapper_dir, fake_python)
    env["RELEASE_TAG"] = "v1.2.1"

    result = subprocess.run(
        [
            _bash_command(),
            str(repo / "packaging" / "offline" / "build_offline_bundle.sh"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "does not match package version 1.2.0" in result.stderr
    assert not (repo / "dist-offline").exists()


def test_verify_offline_bundle_rejects_tampered_checksum_payload(
    tmp_path: Path,
) -> None:
    repo = _prepare_fake_bundle_repo(tmp_path)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_fake_uv(wrapper_dir)

    build = subprocess.run(
        [
            _bash_command(),
            str(repo / "packaging" / "offline" / "build_offline_bundle.sh"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_script_env(wrapper_dir, fake_python),
        check=False,
    )
    assert build.returncode == 0, build.stderr
    bundle_root = repo / "dist-offline" / "ai-sdlc-offline-1.2.0"
    (bundle_root / "README.txt").write_text("tampered\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_OFFLINE_DIR / "verify_offline_bundle.py"),
            str(bundle_root),
            "--require-checksums",
            "--expected-package-version",
            "1.2.0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr


def test_verify_offline_bundle_rejects_expected_package_version_mismatch(
    tmp_path: Path,
) -> None:
    repo = _prepare_fake_bundle_repo(tmp_path)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_fake_uv(wrapper_dir)

    build = subprocess.run(
        [
            _bash_command(),
            str(repo / "packaging" / "offline" / "build_offline_bundle.sh"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_script_env(wrapper_dir, fake_python),
        check=False,
    )
    assert build.returncode == 0, build.stderr
    bundle_root = repo / "dist-offline" / "ai-sdlc-offline-1.2.0"

    result = subprocess.run(
        [
            sys.executable,
            str(_OFFLINE_DIR / "verify_offline_bundle.py"),
            str(bundle_root),
            "--require-checksums",
            "--expected-package-version",
            "1.2.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "manifest package version" in result.stderr


def test_build_offline_bundle_embeds_portable_python_runtime_when_configured(
    tmp_path: Path,
) -> None:
    repo = _prepare_fake_bundle_repo(tmp_path)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_fake_uv(wrapper_dir)
    portable_runtime = tmp_path / "portable-python"
    portable_python = _make_verifiable_portable_python(portable_runtime)

    env = _script_env(wrapper_dir, fake_python)
    env["AI_SDLC_OFFLINE_PYTHON_RUNTIME"] = str(portable_runtime)

    result = subprocess.run(
        [
            _bash_command(),
            str(repo / "packaging" / "offline" / "build_offline_bundle.sh"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    bundle_root = repo / "dist-offline" / "ai-sdlc-offline-1.2.0"
    assert (
        bundle_root / "python-runtime" / portable_python.relative_to(portable_runtime)
    ).is_file()
    manifest = json.loads(
        (bundle_root / "bundle-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["python_runtime_bundled"] is True
    assert "Bundled Python runtime: included" in result.stdout
    assert "Offline bundle runtime verification passed." in result.stdout


def test_verify_offline_bundle_rejects_runtime_symlinks_that_escape_bundle(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    runtime_dir = bundle_dir / "python-runtime"
    bin_dir = runtime_dir / "bin"
    wheels_dir = bundle_dir / "wheels"
    bin_dir.mkdir(parents=True)
    wheels_dir.mkdir()
    outside_python = tmp_path / "outside-python"
    _write_executable(
        outside_python,
        f"#!{_bash_shebang_python()}\nimport sys\nprint('outside')\n",
    )
    (bin_dir / "python3").symlink_to(outside_python)
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
                "python_runtime_bundled": True,
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_OFFLINE_DIR / "verify_offline_bundle.py"),
            str(bundle_dir),
            "--require-bundled-runtime",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "points outside the bundle" in result.stderr


def test_verify_offline_bundle_rejects_runtime_root_symlink(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    real_runtime = tmp_path / "real-python-runtime"
    _make_fake_portable_python(real_runtime)
    (bundle_dir).mkdir()
    (bundle_dir / "python-runtime").symlink_to(real_runtime)
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
                "python_runtime_bundled": True,
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_OFFLINE_DIR / "verify_offline_bundle.py"),
            str(bundle_dir),
            "--require-bundled-runtime",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "python-runtime itself is a symlink" in result.stderr


def test_verify_offline_bundle_accepts_install_log_with_bundled_runtime(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    runtime_dir = bundle_dir / "python-runtime"
    _make_verifiable_portable_python(runtime_dir)
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
                "python_runtime_bundled": True,
                "wheel_python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            }
        ),
        encoding="utf-8",
    )
    install_log = tmp_path / "install.log"
    install_log.write_text("Using bundled Python runtime: python-runtime/bin/python3\n")

    result = subprocess.run(
        [
            sys.executable,
            str(_OFFLINE_DIR / "verify_offline_bundle.py"),
            str(bundle_dir),
            "--require-bundled-runtime",
            "--install-log",
            str(install_log),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_runtime_probe_disables_bytecode_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verify_offline_bundle_module()
    runtime_python = tmp_path / "python3"
    runtime_python.write_text("", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "3.11\n", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._assert_runtime_executes(runtime_python, "3.11")

    assert isinstance(captured["env"], dict)
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_verify_offline_bundle_rejects_macos_framework_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verify_offline_bundle_module()
    runtime_root = tmp_path / "python-runtime"
    bin_dir = runtime_root / "bin"
    bin_dir.mkdir(parents=True)
    runtime_python = bin_dir / "python3"
    runtime_python.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 16)
    runtime_python.chmod(0o755)

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["otool", "-L", str(runtime_python)],
            returncode=0,
            stdout=(
                f"{runtime_python}:\n"
                "\t/Library/Frameworks/Python.framework/Versions/3.11/Python "
                "(compatibility version 3.11.0, current version 3.11.0)\n"
                "\t/usr/lib/libSystem.B.dylib "
                "(compatibility version 1.0.0, current version 1311.0.0)\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        module._assert_portable_dynamic_dependencies(
            runtime_python,
            runtime_root,
            "3.11",
        )

    assert "build-host absolute path" in str(exc_info.value)
    assert "/Library/Frameworks/Python.framework" in str(exc_info.value)


def test_verify_offline_bundle_accepts_linux_ldd_dependency_inside_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verify_offline_bundle_module()
    runtime_root = tmp_path / "python-runtime"
    bin_dir = runtime_root / "bin"
    lib_dir = runtime_root / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    runtime_python = bin_dir / "python3"
    bundled_libpython = lib_dir / "libpython3.11.so.1.0"
    runtime_python.write_bytes(b"\x7fELF" + b"\x00" * 16)
    bundled_libpython.write_text("fake libpython\n", encoding="utf-8")
    runtime_python.chmod(0o755)

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["ldd", str(runtime_python)],
            returncode=0,
            stdout=(
                f"libpython3.11.so.1.0 => {bundled_libpython} "
                "(0x00007f0000000000)\n"
                "libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 "
                "(0x00007f0000000000)\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._assert_portable_dynamic_dependencies(
        runtime_python,
        runtime_root,
        "3.11",
    )


def test_build_offline_bundle_uses_relative_zip_paths_for_cross_platform_python() -> (
    None
):
    script = (_OFFLINE_DIR / "build_offline_bundle.sh").read_text(encoding="utf-8")

    assert 'root = Path("dist-offline")' in script
    assert 'dst = root / f"{out_basename}.zip"' in script


def test_build_offline_bundle_can_suffix_platform_release_assets(
    tmp_path: Path,
) -> None:
    repo = _prepare_fake_bundle_repo(tmp_path)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_fake_uv(wrapper_dir)

    env = _script_env(wrapper_dir, fake_python)
    if os.name == "nt":
        _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env["AI_SDLC_OFFLINE_ASSET_SUFFIX"] = "-windows-amd64"

    result = subprocess.run(
        [
            _bash_command(),
            str(repo / "packaging" / "offline" / "build_offline_bundle.sh"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    bundle_root = repo / "dist-offline" / "ai-sdlc-offline-1.2.0-windows-amd64"
    assert (bundle_root / "bundle-manifest.json").is_file()
    assert (
        repo / "dist-offline" / "ai-sdlc-offline-1.2.0-windows-amd64.tar.gz"
    ).is_file()
    zip_path = repo / "dist-offline" / "ai-sdlc-offline-1.2.0-windows-amd64.zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as archive:
        assert (
            "ai-sdlc-offline-1.2.0-windows-amd64/bundle-manifest.json"
            in archive.namelist()
        )


def test_build_offline_bundle_can_include_multiple_python_abis(tmp_path: Path) -> None:
    repo = _prepare_fake_bundle_repo(tmp_path)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_fake_uv(wrapper_dir)

    env = _script_env(wrapper_dir, fake_python)
    env["AI_SDLC_OFFLINE_ASSET_SUFFIX"] = "-windows-amd64"
    env["AI_SDLC_OFFLINE_PYTHON_VERSIONS"] = "3.11,3.12"
    env["AI_SDLC_OFFLINE_TARGET_PLATFORM"] = "win_amd64"
    if os.name == "nt":
        _set_bash_wrapper_env(env, wrapper_dir, tmp_path)

    result = subprocess.run(
        [
            _bash_command(),
            str(repo / "packaging" / "offline" / "build_offline_bundle.sh"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest_path = (
        repo
        / "dist-offline"
        / "ai-sdlc-offline-1.2.0-windows-amd64"
        / "bundle-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["wheel_python_version"] == "3.11"
    assert manifest["supported_python_versions"] == ["3.11", "3.12"]
    assert manifest["supported_wheel_python_tags"] == ["cp311", "cp312"]


def test_install_offline_rejects_platform_manifest_mismatch(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / "ai_sdlc-1.2.0-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": "definitely-not-this-os",
                "platform_machine": "definitely-not-this-cpu",
            }
        ),
        encoding="utf-8",
    )

    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh")],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=_script_env(wrapper_dir, fake_python),
        check=False,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "platform mismatch" in combined.lower()


def test_install_offline_accepts_matching_platform_manifest(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / "ai_sdlc-1.2.0-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
            }
        ),
        encoding="utf-8",
    )

    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh")],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=_script_env(wrapper_dir, fake_python),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (bundle_dir / ".venv" / "bin" / "activate").is_file()
    assert (bundle_dir / ".venv" / "bin" / "ai-sdlc").is_file()
    assert "当前结果 / Result" in result.stdout
    expected_python = _bash_path(bundle_dir / ".venv" / "bin" / "python")
    assert f'"{expected_python}" -m ai_sdlc init .' in result.stdout
    assert "Use the full command above" in result.stdout
    assert "PATH was not changed" not in result.stdout
    assert "Bare ai-sdlc may still resolve an older install" not in result.stdout
    assert "Current bare ai-sdlc" not in result.stdout
    assert "--upgrade-existing" in result.stdout


def test_install_offline_add_to_path_enables_bare_cli_guidance(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_basic_bundle(bundle_dir)
    home_dir = tmp_path / "home"
    wrapper_dir = tmp_path / "wrappers"
    home_dir.mkdir()
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)

    env = _script_env(wrapper_dir, fake_python)
    env["HOME"] = str(home_dir)
    env["SHELL"] = "/bin/bash"

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh"), "--add-to-path"],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (bundle_dir / ".venv" / "bin" / "ai-sdlc").is_file()
    assert (home_dir / ".local" / "bin" / "ai-sdlc").is_symlink()
    assert "cd <your-project> && ai-sdlc init ." in result.stdout
    assert "New terminals can run ai-sdlc directly." in result.stdout
    assert "PATH entry added" not in result.stdout
    assert f'export PATH="{_bash_path(home_dir / ".local" / "bin")}:$PATH"' in (
        home_dir / ".bashrc"
    ).read_text(encoding="utf-8")


def test_install_offline_rejects_python_abi_manifest_mismatch(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / "ai_sdlc-1.2.0-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
                "wheel_python_version": "9.9",
            }
        ),
        encoding="utf-8",
    )

    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh")],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=_script_env(wrapper_dir, fake_python),
        check=False,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "python=9.9 wheel ABI" in combined


def test_install_offline_accepts_current_python_in_supported_abi_list(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / "ai_sdlc-1.2.0-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    other_version = "3.99" if current_version != "3.99" else "3.98"
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
                "wheel_python_version": other_version,
                "supported_python_versions": [other_version, current_version],
            }
        ),
        encoding="utf-8",
    )

    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh")],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=_script_env(wrapper_dir, fake_python),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Validated offline bundle platform manifest." in result.stdout


def test_install_offline_reports_bundled_runtime_startup_crash(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    wheels_dir = bundle_dir / "wheels"
    runtime_bin = bundle_dir / "python-runtime" / "bin"
    wheels_dir.mkdir(parents=True)
    runtime_bin.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / "ai_sdlc-1.2.0-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    _write_executable(
        runtime_bin / "python3",
        "#!/bin/sh\n"
        "echo 'dyld: Library not loaded: /Library/Frameworks/Python.framework/Versions/3.11/Python' >&2\n"
        "exit 134\n",
    )

    env = dict(os.environ)
    _set_env_path(env, "/usr/bin:/bin")
    env.pop("PYTHON", None)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh")],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "bundled Python runtime is not executable" in combined
    assert "/Library/Frameworks/Python.framework" in combined
    assert "need Python >= 3.11" not in combined


def test_install_offline_uses_bundled_python_runtime_when_system_python_missing(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / "ai_sdlc-1.2.0-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
            }
        ),
        encoding="utf-8",
    )
    portable_runtime = bundle_dir / "python-runtime"
    _make_fake_portable_python(portable_runtime)

    env = dict(os.environ)
    _set_env_path(env, "")
    env.pop("PYTHON", None)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh")],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Using bundled Python runtime" in result.stdout
    assert (bundle_dir / ".venv" / "bin" / "ai-sdlc").is_file()


def test_install_offline_upgrade_existing_uses_current_cli_runtime(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX upgrade-existing installer path is covered on POSIX runners")
    bundle_dir = tmp_path / "bundle"
    _write_basic_bundle(bundle_dir)
    existing_bin = tmp_path / "existing-bin"
    fake_python = _make_upgrade_existing_python(existing_bin)
    _write_executable(existing_bin / "ai-sdlc", f"#!{fake_python}\n")

    env = dict(os.environ)
    _set_env_path(env, os.pathsep.join([str(existing_bin), "/usr/bin", "/bin"]))
    env.pop("PYTHON", None)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh"), "--upgrade-existing"],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Using existing AI-SDLC runtime" in result.stdout
    assert "Upgrade completed" in result.stdout
    assert (existing_bin / "installed-version.txt").read_text(
        encoding="utf-8"
    ) == "1.2.0"
    assert not (bundle_dir / ".venv").exists()


def test_install_offline_upgrade_existing_rejects_stale_bare_cli_version(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX upgrade-existing installer path is covered on POSIX runners")
    bundle_dir = tmp_path / "bundle"
    _write_basic_bundle(bundle_dir)
    existing_bin = tmp_path / "existing-bin"
    fake_python = _make_upgrade_existing_python(existing_bin, cli_version="1.1.0")
    _write_executable(existing_bin / "ai-sdlc", f"#!{fake_python}\n")

    env = dict(os.environ)
    _set_env_path(env, os.pathsep.join([str(existing_bin), "/usr/bin", "/bin"]))
    env.pop("PYTHON", None)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh"), "--upgrade-existing"],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "current PATH resolves ai-sdlc 1.1.0, expected 1.2.0" in result.stderr


def test_install_offline_upgrade_existing_reads_distlib_shell_wrapper(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX upgrade-existing installer path is covered on POSIX runners")
    bundle_dir = tmp_path / "bundle"
    _write_basic_bundle(bundle_dir)
    existing_bin = tmp_path / "existing bin"
    fake_python = _make_upgrade_existing_python(existing_bin)
    _write_executable(
        existing_bin / "ai-sdlc",
        f"#!/bin/sh\n'''exec' \"{fake_python}\" \"$0\" \"$@\"\n' '''\n",
    )

    env = dict(os.environ)
    _set_env_path(env, os.pathsep.join([str(existing_bin), "/usr/bin", "/bin"]))
    env.pop("PYTHON", None)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh"), "--upgrade-existing"],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Using existing AI-SDLC runtime" in result.stdout
    assert (existing_bin / "installed-version.txt").read_text(
        encoding="utf-8"
    ) == "1.2.0"
    assert not (bundle_dir / ".venv").exists()


def test_install_offline_uses_versioned_bundled_python_runtime_when_only_python311_exists(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(parents=True)
    shutil.copy2(_OFFLINE_DIR / "install_offline.sh", bundle_dir / "install_offline.sh")
    (wheels_dir / "ai_sdlc-1.2.0-py3-none-any.whl").write_text(
        "fake wheel\n",
        encoding="utf-8",
    )
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "package_version": "1.2.0",
                "platform_os": platform.system().lower(),
                "platform_machine": platform.machine().lower(),
            }
        ),
        encoding="utf-8",
    )
    portable_runtime = bundle_dir / "python-runtime"
    _make_fake_portable_python_versioned(portable_runtime)

    env = dict(os.environ)
    _set_env_path(env, "")
    env.pop("PYTHON", None)

    result = subprocess.run(
        [_bash_command(), str(bundle_dir / "install_offline.sh")],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Using bundled Python runtime" in result.stdout
    assert "python-runtime/bin/python3.11" in result.stdout
    assert (bundle_dir / ".venv" / "bin" / "ai-sdlc").is_file()


def test_install_online_uses_detected_python_and_prints_bilingual_guidance(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "install_online.sh"
    shutil.copy2(_PACKAGING_DIR / "install_online.sh", script_path)
    script_path.chmod(0o755)

    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_path_alias(fake_python, wrapper_dir / "python3.11")

    env = dict(os.environ)
    _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env["AI_SDLC_PACKAGE_SPEC"] = "ai-sdlc==1.0.1"

    result = subprocess.run(
        [_bash_command(), str(script_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".venv" / "bin" / "ai-sdlc").is_file()
    assert "Using Python runtime: python3.11" in result.stdout
    assert "当前结果 / Result" in result.stdout
    assert "下一步 / Next" in result.stdout
    expected_python = _bash_path(tmp_path / ".venv" / "bin" / "python")
    assert f'"{expected_python}" -m ai_sdlc init .' in result.stdout
    assert "Use the full command above" in result.stdout
    assert "PATH was not changed" not in result.stdout
    assert "Bare ai-sdlc may still resolve an older install" not in result.stdout
    assert "ai-sdlc adapter status" not in result.stdout
    assert "ai-sdlc run --dry-run" not in result.stdout


def test_install_online_add_to_path_enables_bare_cli_guidance(tmp_path: Path) -> None:
    script_path = tmp_path / "install_online.sh"
    shutil.copy2(_PACKAGING_DIR / "install_online.sh", script_path)
    script_path.chmod(0o755)

    home_dir = tmp_path / "home"
    wrapper_dir = tmp_path / "wrappers"
    home_dir.mkdir()
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_path_alias(fake_python, wrapper_dir / "python3.11")

    env = dict(os.environ)
    _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env["AI_SDLC_PACKAGE_SPEC"] = "ai-sdlc==1.0.1"
    env["HOME"] = str(home_dir)
    env["SHELL"] = "/bin/bash"

    result = subprocess.run(
        [_bash_command(), str(script_path), "--add-to-path"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".venv" / "bin" / "ai-sdlc").is_file()
    assert (home_dir / ".local" / "bin" / "ai-sdlc").is_symlink()
    assert "cd <your-project> && ai-sdlc init ." in result.stdout
    assert "New terminals can run ai-sdlc directly." in result.stdout
    assert "PATH entry added" not in result.stdout
    assert f'export PATH="{_bash_path(home_dir / ".local" / "bin")}:$PATH"' in (
        home_dir / ".bashrc"
    ).read_text(encoding="utf-8")


def test_install_online_fails_before_creating_venv_when_git_package_lacks_git(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "install_online.sh"
    shutil.copy2(_PACKAGING_DIR / "install_online.sh", script_path)
    script_path.chmod(0o755)

    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    fake_python = _make_fake_python(wrapper_dir)
    _make_path_alias(fake_python, wrapper_dir / "python3.11")

    env = dict(os.environ)
    _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env.pop("AI_SDLC_PACKAGE_SPEC", None)

    result = subprocess.run(
        [_bash_command(), str(script_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "Git is required for the configured git+ package source" in result.stdout
    assert "Install Git, then rerun this installer" in result.stdout
    assert not (tmp_path / ".venv").exists()


def _copy_online_installer_with_os_release_fixture(
    tmp_path: Path, os_release: str | None
) -> Path:
    script_path = tmp_path / "install_online.sh"
    script = (_PACKAGING_DIR / "install_online.sh").read_text(encoding="utf-8")
    fixture_path = tmp_path / "os-release"
    if os_release is not None:
        fixture_path.write_bytes(os_release.encode("utf-8"))
    os_release_reads = script.count("/etc/os-release")
    assert os_release_reads == 1
    script_path.write_text(
        script.replace("/etc/os-release", _bash_path(fixture_path)), encoding="utf-8"
    )
    script_path.chmod(0o755)
    return script_path


def test_os_release_fixture_preserves_linux_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_text = Path.write_text

    def reject_text_mode_os_release_write(
        path: Path, data: str, *args: object, **kwargs: object
    ) -> int:
        if path.name == "os-release":
            raise AssertionError("Linux os-release fixtures must be written as bytes")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", reject_text_mode_os_release_write)

    _copy_online_installer_with_os_release_fixture(
        tmp_path, "ID=debian\nVERSION_ID=12\n"
    )

    assert (tmp_path / "os-release").read_bytes() == b"ID=debian\nVERSION_ID=12\n"


def _make_linux_identity_wrappers(
    wrapper_dir: Path, *, arch: str, libc: str, identity_log: Path
) -> None:
    _write_executable(
        wrapper_dir / "uname",
        f"""#!{_bash_shebang_python()}
import sys
from pathlib import Path

Path({str(identity_log)!r}).open("a", encoding="utf-8").write("uname " + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["-s"]:
    print("Linux")
elif sys.argv[1:] == ["-m"]:
    print({arch!r})
else:
    raise SystemExit(1)
""",
    )
    _write_executable(
        wrapper_dir / "getconf",
        f"""#!{_bash_shebang_python()}
import sys
from pathlib import Path

Path({str(identity_log)!r}).open("a", encoding="utf-8").write("getconf " + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["GNU_LIBC_VERSION"]:
    print({libc!r})
else:
    raise SystemExit(1)
""",
    )
    _write_executable(
        wrapper_dir / "id",
        f"""#!{_bash_shebang_python()}
import sys
if sys.argv[1:] == ["-u"]:
    print("0")
    raise SystemExit(0)
raise SystemExit(1)
""",
    )


def _make_fake_apt_bootstrap(
    wrapper_dir: Path, apt_log: Path, fake_python: Path | None
) -> None:
    install_body = ""
    if fake_python is not None:
        install_body = f"""
if sys.argv[1:2] == ["install"]:
    source = Path({str(fake_python)!r})
    target = Path({str(wrapper_dir / "python3.11")!r})
    shutil.copy2(source, target)
    target.chmod(0o755)
"""
    _write_executable(
        wrapper_dir / "apt-get",
        f"""#!{_bash_shebang_python()}
import shutil
import sys
from pathlib import Path

with Path({str(apt_log)!r}).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
{install_body}
raise SystemExit(0)
""",
    )


def _prepare_missing_python_linux_install(
    tmp_path: Path,
    *,
    os_release: str | None,
    arch: str,
    libc: str,
    install_python_after_apt: bool = False,
) -> tuple[Path, Path, Path, dict[str, str], Path, Path]:
    script_path = _copy_online_installer_with_os_release_fixture(tmp_path, os_release)
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    identity_log = tmp_path / "identity.log"
    apt_log = tmp_path / "apt.log"
    _make_linux_identity_wrappers(
        wrapper_dir, arch=arch, libc=libc, identity_log=identity_log
    )
    fake_python = _make_fake_python(wrapper_dir) if install_python_after_apt else None
    _make_fake_apt_bootstrap(wrapper_dir, apt_log, fake_python)
    _make_fake_git(wrapper_dir)
    env = dict(os.environ)
    _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env["AI_SDLC_PACKAGE_SPEC"] = "ai-sdlc==1.0.1"
    env.pop("PYTHON", None)
    return script_path, wrapper_dir, apt_log, env, identity_log, tmp_path / "project"


def _prepare_unsupported_install_home(
    home_dir: Path,
) -> tuple[bytes, bytes, str, str]:
    home_dir.mkdir()
    bashrc = home_dir / ".bashrc"
    profile = home_dir / ".profile"
    bashrc.write_bytes(b"# unchanged bashrc\\n")
    profile.write_bytes(b"# unchanged profile\\n")
    bashrc_before = bashrc.read_bytes()
    profile_before = profile.read_bytes()
    return (
        bashrc_before,
        profile_before,
        hashlib.sha256(bashrc_before).hexdigest(),
        hashlib.sha256(profile_before).hexdigest(),
    )


def _snapshot_project_tree(project_dir: Path) -> list[tuple[str, str, str]]:
    snapshot: list[tuple[str, str, str]] = []
    for path in sorted(
        project_dir.rglob("*"),
        key=lambda item: item.relative_to(project_dir).as_posix(),
    ):
        relative_path = path.relative_to(project_dir).as_posix()
        if path.is_symlink():
            snapshot.append((relative_path, "symlink", os.readlink(path)))
        elif path.is_dir():
            snapshot.append((relative_path, "directory", ""))
        elif path.is_file():
            snapshot.append(
                (relative_path, "file", hashlib.sha256(path.read_bytes()).hexdigest())
            )
        else:
            snapshot.append((relative_path, "other", ""))
    return snapshot


def _assert_unsupported_install_did_not_mutate(
    *,
    venv_target: Path,
    home_dir: Path,
    bashrc_before: bytes,
    profile_before: bytes,
    bashrc_hash_before: str,
    profile_hash_before: str,
    project_dir: Path,
    project_before: list[tuple[str, str, str]],
    apt_log: Path,
) -> None:
    assert not venv_target.exists()
    assert not (home_dir / ".local" / "bin" / "ai-sdlc").exists()
    assert (home_dir / ".bashrc").read_bytes() == bashrc_before
    assert (home_dir / ".profile").read_bytes() == profile_before
    assert (
        hashlib.sha256((home_dir / ".bashrc").read_bytes()).hexdigest()
        == bashrc_hash_before
    )
    assert (
        hashlib.sha256((home_dir / ".profile").read_bytes()).hexdigest()
        == profile_hash_before
    )
    assert _snapshot_project_tree(project_dir) == project_before
    assert not apt_log.exists()


def test_online_unsupported_install_snapshot_detects_any_project_tree_change(
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    bashrc_before, profile_before, bashrc_hash_before, profile_hash_before = (
        _prepare_unsupported_install_home(home_dir)
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "keep.txt").write_text("business data\n", encoding="utf-8")
    nested_dir = project_dir / "nested"
    nested_dir.mkdir()
    (nested_dir / "config.txt").write_text("preserve\n", encoding="utf-8")
    (project_dir / "keep-link").symlink_to("keep.txt")
    project_before = _snapshot_project_tree(project_dir)
    (project_dir / "unexpected.txt").write_text("mutation\n", encoding="utf-8")

    with pytest.raises(AssertionError):
        _assert_unsupported_install_did_not_mutate(
            venv_target=tmp_path / "custom-venv",
            home_dir=home_dir,
            bashrc_before=bashrc_before,
            profile_before=profile_before,
            bashrc_hash_before=bashrc_hash_before,
            profile_hash_before=profile_hash_before,
            project_dir=project_dir,
            project_before=project_before,
            apt_log=tmp_path / "apt.log",
        )


def test_install_online_bootstraps_only_debian12_x86_64_glibc_without_python(
    tmp_path: Path,
) -> None:
    script_path, _, apt_log, env, _, project_dir = (
        _prepare_missing_python_linux_install(
            tmp_path,
            os_release="# accepted comment\n\n" + _DEBIAN12_OS_RELEASE,
            arch="x86_64",
            libc="glibc 2.36",
            install_python_after_apt=True,
        )
    )
    project_dir.mkdir()

    result = subprocess.run(
        [_bash_command(), str(script_path)],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (project_dir / ".venv" / "bin" / "ai-sdlc").is_file()
    assert apt_log.read_text(encoding="utf-8").splitlines() == [
        "update",
        "install -y python3.11 python3.11-venv python3-pip",
    ]


def test_install_online_rejects_ubuntu_glibc_without_python_before_any_mutation(
    tmp_path: Path,
) -> None:
    script_path, _, apt_log, env, _, project_dir = (
        _prepare_missing_python_linux_install(
            tmp_path,
            os_release=_UBUNTU2204_OS_RELEASE,
            arch="x86_64",
            libc="glibc 2.35",
        )
    )
    home_dir = tmp_path / "home"
    bashrc_before, profile_before, bashrc_hash_before, profile_hash_before = (
        _prepare_unsupported_install_home(home_dir)
    )
    project_dir.mkdir()
    (project_dir / "keep.txt").write_bytes(b"business data\n")
    project_before = _snapshot_project_tree(project_dir)
    env["HOME"] = str(home_dir)
    env["SHELL"] = "/bin/bash"
    venv_target = tmp_path / "custom-venv"

    result = subprocess.run(
        [_bash_command(), str(script_path), str(venv_target), "--add-to-path"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "distro=ubuntu version=22.04 arch=x86_64 libc=glibc" in result.stdout
    assert "Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc" in result.stdout
    assert "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" in result.stdout
    assert "route 6/12" in result.stdout
    _assert_unsupported_install_did_not_mutate(
        venv_target=venv_target,
        home_dir=home_dir,
        bashrc_before=bashrc_before,
        profile_before=profile_before,
        bashrc_hash_before=bashrc_hash_before,
        profile_hash_before=profile_hash_before,
        project_dir=project_dir,
        project_before=project_before,
        apt_log=apt_log,
    )


def test_install_online_uses_existing_python_on_ubuntu_without_consulting_linux_guard(
    tmp_path: Path,
) -> None:
    script_path = _copy_online_installer_with_os_release_fixture(
        tmp_path, "ID=ubuntu\nVERSION_ID=22.04\n"
    )
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    identity_log = tmp_path / "identity.log"
    _make_linux_identity_wrappers(
        wrapper_dir, arch="x86_64", libc="glibc 2.35", identity_log=identity_log
    )
    fake_python = _make_fake_python(wrapper_dir)
    _make_path_alias(fake_python, wrapper_dir / "python3.11")
    _make_fake_git(wrapper_dir)
    env = dict(os.environ)
    _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env["AI_SDLC_PACKAGE_SPEC"] = "ai-sdlc==1.0.1"

    result = subprocess.run(
        [_bash_command(), str(script_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".venv" / "bin" / "ai-sdlc").is_file()
    assert not identity_log.exists()


@pytest.mark.parametrize(
    ("os_release", "arch", "libc", "expected_identity", "expects_amd64_fallback"),
    [
        (None, "x86_64", "glibc 2.35", "arch=x86_64 libc=glibc", True),
        (
            "ID=ubuntu\nID=debian\nVERSION_ID=22.04\n",
            "x86_64",
            "glibc 2.35",
            "arch=x86_64 libc=glibc",
            True,
        ),
        (
            "ID=debian\nVERSION_ID=12\nBROKEN_LINE\n",
            "x86_64",
            "glibc 2.35",
            "arch=x86_64 libc=glibc",
            True,
        ),
        (
            "ID=debian\nVERSION_ID=12\nINVALID KEY=value\n",
            "x86_64",
            "glibc 2.35",
            "arch=x86_64 libc=glibc",
            True,
        ),
        (
            'ID=debian\nVERSION_ID=12\nPRETTY_NAME="unterminated\n',
            "x86_64",
            "glibc 2.35",
            "arch=x86_64 libc=glibc",
            True,
        ),
        (
            "ID=debian\nVERSION_ID=12\nHOME_URL=$(touch unsafe)\n",
            "x86_64",
            "glibc 2.35",
            "arch=x86_64 libc=glibc",
            True,
        ),
        (None, "", "glibc 2.35", "arch=unknown libc=glibc", False),
        (None, "x86_64", "", "arch=x86_64 libc=unknown", False),
    ],
)
def test_install_online_preserves_independent_linux_host_facts_when_os_release_is_unknown(
    tmp_path: Path,
    os_release: str | None,
    arch: str,
    libc: str,
    expected_identity: str,
    expects_amd64_fallback: bool,
) -> None:
    script_path, _, apt_log, env, _, project_dir = (
        _prepare_missing_python_linux_install(
            tmp_path,
            os_release=os_release,
            arch=arch,
            libc=libc,
        )
    )
    home_dir = tmp_path / "home"
    bashrc_before, profile_before, bashrc_hash_before, profile_hash_before = (
        _prepare_unsupported_install_home(home_dir)
    )
    project_dir.mkdir()
    (project_dir / "keep.txt").write_bytes(b"business data\n")
    project_before = _snapshot_project_tree(project_dir)
    env["HOME"] = str(home_dir)
    env["SHELL"] = "/bin/bash"
    venv_target = tmp_path / "custom-venv"

    result = subprocess.run(
        [_bash_command(), str(script_path), str(venv_target), "--add-to-path"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert f"distro=unknown version=unknown {expected_identity}" in result.stdout
    if expects_amd64_fallback:
        assert "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" in result.stdout
        assert "route 6/12" in result.stdout
    else:
        assert "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" not in result.stdout
        assert "route 6/12" not in result.stdout
    _assert_unsupported_install_did_not_mutate(
        venv_target=venv_target,
        home_dir=home_dir,
        bashrc_before=bashrc_before,
        profile_before=profile_before,
        bashrc_hash_before=bashrc_hash_before,
        profile_hash_before=profile_hash_before,
        project_dir=project_dir,
        project_before=project_before,
        apt_log=apt_log,
    )


def test_install_online_rejects_linux_aarch64_glibc_without_python_fallback(
    tmp_path: Path,
) -> None:
    script_path, _, apt_log, env, _, project_dir = (
        _prepare_missing_python_linux_install(
            tmp_path,
            os_release="ID=debian\nVERSION_ID=12\n",
            arch="aarch64",
            libc="glibc 2.36",
        )
    )
    home_dir = tmp_path / "home"
    bashrc_before, profile_before, bashrc_hash_before, profile_hash_before = (
        _prepare_unsupported_install_home(home_dir)
    )
    project_dir.mkdir()
    (project_dir / "keep.txt").write_bytes(b"business data\n")
    project_before = _snapshot_project_tree(project_dir)
    env["HOME"] = str(home_dir)
    env["SHELL"] = "/bin/bash"
    venv_target = tmp_path / "custom-venv"

    result = subprocess.run(
        [_bash_command(), str(script_path), str(venv_target), "--add-to-path"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "distro=debian version=12 arch=aarch64 libc=glibc" in result.stdout
    assert "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" not in result.stdout
    assert "route 6/12" not in result.stdout
    _assert_unsupported_install_did_not_mutate(
        venv_target=venv_target,
        home_dir=home_dir,
        bashrc_before=bashrc_before,
        profile_before=profile_before,
        bashrc_hash_before=bashrc_hash_before,
        profile_hash_before=profile_hash_before,
        project_dir=project_dir,
        project_before=project_before,
        apt_log=apt_log,
    )


def test_install_online_rejects_linux_x86_64_musl_without_python_fallback(
    tmp_path: Path,
) -> None:
    script_path, _, apt_log, env, _, project_dir = (
        _prepare_missing_python_linux_install(
            tmp_path,
            os_release="ID=debian\nVERSION_ID=12\n",
            arch="x86_64",
            libc="musl 1.2.4",
        )
    )
    home_dir = tmp_path / "home"
    bashrc_before, profile_before, bashrc_hash_before, profile_hash_before = (
        _prepare_unsupported_install_home(home_dir)
    )
    project_dir.mkdir()
    (project_dir / "keep.txt").write_bytes(b"business data\n")
    project_before = _snapshot_project_tree(project_dir)
    env["HOME"] = str(home_dir)
    env["SHELL"] = "/bin/bash"
    venv_target = tmp_path / "custom-venv"

    result = subprocess.run(
        [_bash_command(), str(script_path), str(venv_target), "--add-to-path"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "distro=debian version=12 arch=x86_64 libc=musl" in result.stdout
    assert "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" not in result.stdout
    assert "route 6/12" not in result.stdout
    _assert_unsupported_install_did_not_mutate(
        venv_target=venv_target,
        home_dir=home_dir,
        bashrc_before=bashrc_before,
        profile_before=profile_before,
        bashrc_hash_before=bashrc_hash_before,
        profile_hash_before=profile_hash_before,
        project_dir=project_dir,
        project_before=project_before,
        apt_log=apt_log,
    )


def test_install_online_uses_existing_python_on_musl_without_consulting_linux_guard(
    tmp_path: Path,
) -> None:
    script_path = _copy_online_installer_with_os_release_fixture(
        tmp_path, "ID=debian\nVERSION_ID=12\n"
    )
    wrapper_dir = tmp_path / "wrappers"
    wrapper_dir.mkdir()
    identity_log = tmp_path / "identity.log"
    _make_linux_identity_wrappers(
        wrapper_dir, arch="x86_64", libc="musl 1.2.4", identity_log=identity_log
    )
    fake_python = _make_fake_python(wrapper_dir)
    _make_path_alias(fake_python, wrapper_dir / "python3.11")
    _make_fake_git(wrapper_dir)
    env = dict(os.environ)
    _set_bash_wrapper_env(env, wrapper_dir, tmp_path)
    env["AI_SDLC_PACKAGE_SPEC"] = "ai-sdlc==1.0.1"

    result = subprocess.run(
        [_bash_command(), str(script_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".venv" / "bin" / "ai-sdlc").is_file()
    assert not identity_log.exists()


def test_windows_online_installer_catches_auto_install_failure_before_bilingual_guidance() -> (
    None
):
    online_ps1 = (_PACKAGING_DIR / "install_online.ps1").read_text(encoding="utf-8")

    assert "try {" in online_ps1
    assert "Install-PythonOnline" in online_ps1
    assert "Write-BilingualStatus" in online_ps1
    assert "No supported Windows package manager found" not in online_ps1


def test_windows_online_installer_accepts_any_py_launcher_python_gte_311() -> None:
    online_ps1 = (_PACKAGING_DIR / "install_online.ps1").read_text(encoding="utf-8")

    assert '@{ Command = "py"; Args = @("-3") }' in online_ps1
    assert '@{ Command = "py"; Args = @("-3.11") }' not in online_ps1


def test_windows_online_installer_checks_native_exit_codes_before_success_guidance() -> (
    None
):
    online_ps1 = (_PACKAGING_DIR / "install_online.ps1").read_text(encoding="utf-8")

    assert "function Assert-LastExitCode" in online_ps1
    assert (
        '& $python.Command @($python.Args + @("-m", "venv", $VenvPath))' in online_ps1
    )
    assert 'Assert-LastExitCode "python -m venv"' in online_ps1
    assert 'Assert-LastExitCode "pip install --upgrade pip"' in online_ps1
    assert 'Assert-LastExitCode "pip install $PackageSpec"' in online_ps1


def test_windows_online_installer_checks_git_source_before_python_or_venv() -> None:
    online_ps1 = (_PACKAGING_DIR / "install_online.ps1").read_text(encoding="utf-8")

    assert "function Assert-GitSourcePrerequisite" in online_ps1
    assert 'if ($PackageSpec -notmatch "^git\\+")' in online_ps1
    assert "Git is required for the configured git+ package source" in online_ps1
    assert "winget install --id Git.Git -e" in online_ps1
    assert online_ps1.index("Assert-GitSourcePrerequisite") < online_ps1.index(
        "$python = Get-PythonCommand"
    )


def test_windows_install_scripts_include_auto_python_detection_and_bilingual_guidance() -> (
    None
):
    offline_bat = (_OFFLINE_DIR / "install_offline.bat").read_text(encoding="utf-8")
    offline_ps1 = (_OFFLINE_DIR / "install_offline.ps1").read_text(encoding="utf-8")
    online_ps1 = (_PACKAGING_DIR / "install_online.ps1").read_text(encoding="utf-8")

    assert "离线安装失败 / Offline install failed." in offline_bat
    assert "离线安装完成 / Offline install complete." in offline_bat

    assert "python-runtime\\python.exe" in offline_ps1
    assert "Using bundled Python runtime" in offline_ps1
    assert "UpgradeExisting" in offline_ps1
    assert "Using existing AI-SDLC runtime" in offline_ps1
    assert 'Join-Path $baseDir "python.exe"' in offline_ps1
    assert (
        "$existingCommandSource = (Resolve-Path -LiteralPath $aiSdlcCommand.Source).Path"
        in offline_ps1
    )
    assert "$existingCliCandidates += $existingCommandSource" in offline_ps1
    assert "supported_python_versions" in offline_ps1
    assert "Get-ManifestPythonVersions" in offline_ps1
    assert "ai-sdlc --version" in offline_ps1
    assert "expectedVersion" in offline_ps1
    assert "failed to upgrade the current ai-sdlc installation" in offline_ps1
    assert "$LASTEXITCODE -ne 0" in offline_ps1
    assert "Result" in offline_ps1
    assert "Next" in offline_ps1
    assert "if ($StatusEn -and ($StatusEn -ne $Status))" in offline_ps1
    assert "if ($PurposeEn -and ($PurposeEn -ne $Purpose))" in offline_ps1
    assert not any(ord(char) > 127 for char in offline_ps1)
    assert "amd64" in offline_ps1
    assert "x64" in offline_ps1
    assert "PYTHONUTF8" in offline_ps1
    assert "PYTHONIOENCODING" in offline_ps1
    assert "UTF8Encoding" in offline_ps1
    assert "`run --dry-run`" not in offline_ps1
    assert "New terminals can run ai-sdlc directly." in offline_ps1
    assert (
        "-AddToPath was provided, so the installer updated User PATH" not in offline_ps1
    )
    assert "The latest AI-SDLC command entry is now preferred" not in offline_ps1
    assert (
        "current parent terminal may still resolve an older ai-sdlc command"
        not in offline_ps1
    )
    assert "Get-Command ai-sdlc | Select-Object Source" not in offline_ps1
    assert "Set-PreferredAiSdlcPath" in offline_ps1
    assert "Repair-AiSdlcCommandPath" in offline_ps1
    assert "Add-DirectoryToUserPath" not in offline_ps1
    assert "Sync-AiSdlcLaunchersOnPath" in offline_ps1
    assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' not in offline_ps1
    assert (
        '[Environment]::SetEnvironmentVariable("Path", $updatedMachinePath, "Machine")'
        not in offline_ps1
    )
    assert "Install-AiSdlcCommandShim" in offline_ps1
    assert 'Join-Path $env:LOCALAPPDATA "AI-SDLC\\bin"' in offline_ps1
    assert 'Join-Path $shimDir "ai-sdlc.exe"' in offline_ps1
    assert 'Join-Path $shimDir "ai-sdlc.cmd"' in offline_ps1
    assert 'Join-Path $shimDir "ai-sdlc.ps1"' in offline_ps1
    assert (
        'Write-TextUtf8NoBom -Path (Join-Path $shimDir "ai-sdlc.ps1")'
        not in offline_ps1
    )
    assert "Remove-Item -LiteralPath $existingPsShim -Force" in offline_ps1
    assert 'Join-Path $shimDir "ai-sdlc-runtime.txt"' in offline_ps1
    assert "Write-TextUtf8NoBom" in offline_ps1
    assert "ConvertFrom-GitBashPath" in offline_ps1
    assert "Get-GitBashHomeDirectory" in offline_ps1
    assert "Update-GitBashProfilePath" in offline_ps1
    assert '$profileNames = @(".bashrc")' in offline_ps1
    assert '@(".bash_profile", ".bash_login", ".profile")' in offline_ps1
    assert "candidateProfileName" in offline_ps1
    assert "Join-Path $gitBashHome $profileName" in offline_ps1
    assert (
        "foreach ($profileName in ($profileNames | Select-Object -Unique))"
        in offline_ps1
    )
    assert "hash -r 2>/dev/null || true" in offline_ps1
    assert "stableShimRuntimePath" in offline_ps1
    assert "Test-DirectoryHasAiSdlc" not in offline_ps1
    assert "Direct shim compatibility" in offline_ps1
    assert "$nextCommand = $stableInitCommand" in offline_ps1
    assert "$nextCommand = $moduleInitCommand" in offline_ps1
    assert "Codex + PowerShell project init" in offline_ps1
    assert "cd YOUR_PROJECT_PATH; ai-sdlc init ." in offline_ps1
    assert "--agent-target codex --shell powershell" in offline_ps1

    assert "winget install --id Python.Python.3.11" in online_ps1
    assert "choco install python311 -y" in online_ps1
    assert "Result" in online_ps1
    assert "Next" in online_ps1
    assert "if ($StatusEn -and ($StatusEn -ne $Status))" in online_ps1
    assert "if ($PurposeEn -and ($PurposeEn -ne $Purpose))" in online_ps1
    assert not any(ord(char) > 127 for char in online_ps1)
    assert "''-m'', ''ai_sdlc'', ''init'', ''.''" in online_ps1
    assert "ai-sdlc adapter status" not in online_ps1
    assert "PYTHONUTF8" in online_ps1
    assert "PYTHONIOENCODING" in online_ps1
    assert "UTF8Encoding" in online_ps1
    assert "New terminals can run ai-sdlc directly." in online_ps1
    assert (
        "-AddToPath was provided, so the installer updated User PATH" not in online_ps1
    )
    assert "The latest AI-SDLC command entry is now preferred" not in online_ps1
    assert (
        "current parent terminal may still resolve an older ai-sdlc command"
        not in online_ps1
    )
    assert "Get-Command ai-sdlc | Select-Object Source" not in online_ps1
    assert "Bare ai-sdlc may still resolve an older install" not in online_ps1
    assert "Set-PreferredAiSdlcPath" in online_ps1
    assert "Repair-AiSdlcCommandPath" in online_ps1
    assert "Add-DirectoryToUserPath" not in online_ps1
    assert "Sync-AiSdlcLaunchersOnPath" in online_ps1
    assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' in online_ps1
    assert (
        '[Environment]::SetEnvironmentVariable("Path", $updatedMachinePath, "Machine")'
        not in online_ps1
    )
    assert (
        '[Environment]::SetEnvironmentVariable("Path", $updatedMachinePath, "Machine")'
        not in online_ps1
    )
    assert "Install-AiSdlcCommandShim" in online_ps1
    assert 'Join-Path $env:LOCALAPPDATA "AI-SDLC\\bin"' in online_ps1
    assert 'Join-Path $shimDir "ai-sdlc.exe"' in online_ps1
    assert 'Join-Path $shimDir "ai-sdlc.cmd"' in online_ps1
    assert 'Join-Path $shimDir "ai-sdlc.ps1"' in online_ps1
    assert (
        'Write-TextUtf8NoBom -Path (Join-Path $shimDir "ai-sdlc.ps1")' not in online_ps1
    )
    assert "Remove-Item -LiteralPath $existingPsShim -Force" in online_ps1
    assert 'Join-Path $shimDir "ai-sdlc-runtime.txt"' in online_ps1
    assert "Write-TextUtf8NoBom" in online_ps1
    assert "ConvertFrom-GitBashPath" in online_ps1
    assert "Get-GitBashHomeDirectory" in online_ps1
    assert "Update-GitBashProfilePath" in online_ps1
    assert '$profileNames = @(".bashrc")' in online_ps1
    assert '@(".bash_profile", ".bash_login", ".profile")' in online_ps1
    assert "candidateProfileName" in online_ps1
    assert "Join-Path $gitBashHome $profileName" in online_ps1
    assert (
        "foreach ($profileName in ($profileNames | Select-Object -Unique))"
        in online_ps1
    )
    assert "hash -r 2>/dev/null || true" in online_ps1
    assert "Test-DirectoryHasAiSdlc" not in online_ps1
    assert "Direct shim compatibility" in online_ps1
    assert "$nextCommand = $stableInitCommand" in online_ps1
    assert "$nextCommand = $moduleInitCommand" in online_ps1
    assert "Codex + PowerShell project init" in online_ps1
    assert "cd YOUR_PROJECT_PATH; ai-sdlc init ." in online_ps1
    assert "--agent-target codex --shell powershell" in online_ps1


def test_windows_install_guidance_is_safe_for_windows_powershell_parser(
    tmp_path: Path,
) -> None:
    offline_ps1 = (_OFFLINE_DIR / "install_offline.ps1").read_text(encoding="utf-8")
    online_ps1 = (_PACKAGING_DIR / "install_online.ps1").read_text(encoding="utf-8")

    for script in (offline_ps1, online_ps1):
        assert "Start-Process -Wait -NoNewWindow" in script
        assert "Resolve-Path -LiteralPath $venvPython" in script
        assert "YOUR_PROJECT_PATH" in script
        assert "[char]34" in script
        assert '`"$resolvedVenvPython`"' not in script
        assert "cd <your-project>" not in script
        assert '-Command "&' not in script
        assert "Activate.ps1'; cd <your-project>" not in script

    assert "$doubleQuote = [char]34" in offline_ps1
    assert "Write-Host \"  $callOperator '$resolvedCliExe' --help\"" not in offline_ps1
    assert (
        "Write-Host \"  $callOperator '$resolvedVenvPython' -m ai_sdlc --help\""
        not in offline_ps1
    )
    expected_help_guidance = (
        "Write-Host ('  & {0}{1}{0} -m ai_sdlc --help' "
        "-f $doubleQuote, $resolvedVenvPython)"
    )
    assert expected_help_guidance in offline_ps1
    assert expected_help_guidance in online_ps1

    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is required for the quoted-path parser regression")

    runtime_dir = tmp_path / "runtime with spaces"
    runtime_dir.mkdir()
    if os.name == "nt":
        fake_python = runtime_dir / "python.cmd"
        fake_python.write_text(
            '@echo off\r\nif "%1 %2 %3"=="-m ai_sdlc --help" exit /b 0\r\nexit /b 23\r\n',
            encoding="utf-8",
        )
    else:
        fake_python = runtime_dir / "python"
        fake_python.write_text(
            '#!/bin/sh\n[ "$1 $2 $3" = "-m ai_sdlc --help" ]\n',
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

    escaped_python = str(fake_python).replace("'", "''")
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$resolvedVenvPython = '{escaped_python}'; "
            '& "$resolvedVenvPython" -m ai_sdlc --help',
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_user_guide_documents_published_assets_and_two_new_user_paths() -> None:
    guide = (_REPO_ROOT / "USER_GUIDE.zh-CN.md").read_text(encoding="utf-8")
    assert "## 第一章：全新用户 + 全新空项目" in guide
    assert "## 第二章：全新用户 + 已有项目" in guide
    assert "https://github.com/panguosong/AI-SDLC" in guide
    assert "ai-sdlc-offline-3.2.1-windows-amd64.zip" in guide
    assert "ai-sdlc-offline-3.2.1-macos-arm64.tar.gz" in guide
    assert "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" in guide
    assert "releases/download/v1.0.4/" not in guide
    assert "Get-FileHash -Algorithm SHA256" in guide
    assert "shasum -a 256 -c" in guide
    assert "sha256sum -c" in guide
    assert "ai-sdlc init ." in guide
    assert "ai-sdlc adopt ." in guide
    assert "Invoke-WebRequest -Uri" in guide
    assert "从源码运行" not in guide
    assert "老版本升级" not in guide


def test_windows_release_guidance_uses_repeat_safe_extract_cache() -> None:
    guidance_paths = [
        _REPO_ROOT / "README.md",
        _REPO_ROOT / "USER_GUIDE.zh-CN.md",
        _OFFLINE_DIR / "README.md",
    ]

    guide = (_REPO_ROOT / "USER_GUIDE.zh-CN.md").read_text(encoding="utf-8")
    assert '$InstallRoot = Join-Path $HOME "AI-SDLC"' in guide
    assert "-DestinationPath $InstallRoot -Force" in guide
    assert guide.count("Push-Location $BundleRoot") >= 2
    assert (
        guide.count(
            'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\install_offline.ps1" -AddToPath'
        )
        >= 2
    )
    assert guide.count("Pop-Location") >= 2
    assert "$GitCommand = Get-Command git -ErrorAction SilentlyContinue" in guide
    assert "if ($GitCommand)" in guide

    for path in guidance_paths:
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"Expand-Archive[^\n]*-DestinationPath\s+\.(?:\s|$)", text
        ), path
        assert not re.search(r"(?m)^(?:cd|Set-Location)\s+\.\\ai-sdlc-offline", text), (
            path
        )

    build_script = (_OFFLINE_DIR / "build_offline_bundle.sh").read_text(
        encoding="utf-8"
    )
    assert ".ai-sdlc-install" in build_script
    assert "DestinationPath \\$ExtractRoot -Force" in build_script
    assert "unzip ${OUT_BASENAME}.zip" not in build_script
