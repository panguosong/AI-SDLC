#!/usr/bin/env python3
"""隔离宿主 Python，执行 macOS 在线安装器的自动 Python 安装分支。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "packaging" / "install_online.sh"


def _write_executable(path: Path, content: str) -> None:
    """写入仅供隔离安装边界使用的可执行 shim。"""

    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_python_candidates(shim_bin: Path) -> None:
    """候选解释器在包管理器安装前失败，安装后支持 venv 最小协议。"""

    candidate = """#!/bin/sh
set -eu
printf 'python %s\n' "$*" >> "$FAKE_BOOTSTRAP_LOG"
if test ! -f "$FAKE_PYTHON_READY"; then
  exit 1
fi
if test "${1:-}" = "-c"; then
  exit 0
fi
if test "${1:-}" = "-m" && test "${2:-}" = "venv"; then
  target="$3"
  mkdir -p "$target/bin"
  cp "$0" "$target/bin/python"
  chmod +x "$target/bin/python"
  exit 0
fi
if test "${1:-}" = "-m" && test "${2:-}" = "pip"; then
  exit 0
fi
exit 2
"""
    for name in ("python3.11", "python3", "python"):
        _write_executable(shim_bin / name, candidate)


def _write_platform_shims(shim_bin: Path, platform_name: str) -> None:
    """用确定性 Homebrew 边界模拟 macOS Python 安装。"""

    _write_executable(
        shim_bin / "git",
        """#!/bin/sh
set -eu
if test "${1:-}" = "--version"; then
  printf '%s\n' 'git version 2.51.0-test'
  exit 0
fi
exit 2
""",
    )
    _write_executable(
        shim_bin / "id",
        """#!/bin/sh
set -eu
if test "${1:-}" = "-u"; then
  printf '%s\n' '0'
  exit 0
fi
exec /usr/bin/id "$@"
""",
    )
    if platform_name == "Darwin":
        _write_executable(
            shim_bin / "brew",
            """#!/bin/sh
set -eu
if test "${1:-}" = "install" && test "${2:-}" = "python@3.11"; then
  printf '%s\n' 'package-manager-install brew python@3.11' >> "$FAKE_BOOTSTRAP_LOG"
  : > "$FAKE_PYTHON_READY"
  exit 0
fi
if test "${1:-}" = "--version"; then
  printf '%s\n' 'Homebrew 4.0.0-test'
  exit 0
fi
exit 2
""",
        )
        return
    raise RuntimeError(
        f"unsupported platform for macOS bootstrap replay: {platform_name}"
    )


def _run_bootstrap(root: Path) -> dict[str, object]:
    """执行完整安装器，并证明 Python 在包管理器动作前不可用。"""

    platform_name = subprocess.run(
        ["/usr/bin/uname", "-s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    shim_bin = root / "shims"
    home = root / "home"
    install_root = root / "runtime"
    ready = root / "python-ready"
    log = root / "bootstrap.log"
    shim_bin.mkdir(parents=True)
    home.mkdir(parents=True)
    _write_python_candidates(shim_bin)
    _write_platform_shims(shim_bin, platform_name)

    env = os.environ.copy()
    env.pop("PYTHON", None)
    env.update(
        {
            "AI_SDLC_PACKAGE_SPEC": (
                "git+https://github.com/panguosong/AI-SDLC.git@v3.2.0"
            ),
            "FAKE_BOOTSTRAP_LOG": str(log),
            "FAKE_PYTHON_READY": str(ready),
            "HOME": str(home),
            "PATH": f"{shim_bin}:/usr/bin:/bin",
        }
    )
    completed = subprocess.run(
        ["/bin/bash", str(INSTALLER), str(install_root)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "isolated Python bootstrap failed: "
            f"stdout={completed.stdout.strip()} stderr={completed.stderr.strip()}"
        )
    events = log.read_text(encoding="utf-8").splitlines()
    install_events = [
        event for event in events if event.startswith("package-manager-install ")
    ]
    install_index = events.index(install_events[0]) if install_events else -1
    preinstall_python_calls = [
        event for event in events[:install_index] if event.startswith("python ")
    ]
    postinstall_python_calls = [
        event for event in events[install_index + 1 :] if event.startswith("python ")
    ]
    if len(preinstall_python_calls) < 3 or not postinstall_python_calls:
        raise RuntimeError(
            "installer did not prove the missing-to-installed Python transition"
        )
    if len(install_events) != 1 or not ready.is_file():
        raise RuntimeError(
            "installer did not execute exactly one Python package install"
        )
    for marker in (
        "No Python 3.11+ detected. Attempting online installation",
        "Using Python runtime: python3.11",
        "Online installation completed",
    ):
        if marker not in completed.stdout:
            raise RuntimeError(f"installer output missing bootstrap marker: {marker}")
    return {
        "platform": platform_name,
        "python_missing_before_install": True,
        "python_available_after_install": True,
        "package_manager_install_calls": len(install_events),
        "installer_completed": True,
    }


def main() -> int:
    """执行平台本机分支并写入 CI 可上传的结构化证据。"""

    evidence_root = (
        Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        / "posix-online-user-guide-e2e-evidence"
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ai-sdlc-python-bootstrap-") as temp:
        evidence = _run_bootstrap(Path(temp))
    evidence_path = evidence_root / "python-bootstrap.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("POSIX_PYTHON_BOOTSTRAP_OK")
    print(evidence_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
