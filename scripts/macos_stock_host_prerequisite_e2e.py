#!/usr/bin/env python3
"""在隔离目录重放两条 macOS 在线路线的 stock-host 前置命令。"""

from __future__ import annotations

import json
import os
import re
import runpy
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VALIDATOR = runpy.run_path(ROOT / "scripts" / "validate_user_guide_standard.py")
_route_sections = _VALIDATOR["_route_sections"]
_step_sections = _VALIDATOR["_step_sections"]

GUIDE = ROOT / "USER_GUIDE.zh-CN.md"
ROUTE_IDS = ("new|online|macos-arm64", "existing|online|macos-arm64")
HOMEBREW_INSTALLER = (
    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
)
_BASH_BLOCK = re.compile(r"```bash\n(?P<body>.*?)```", re.DOTALL)


def _prerequisite_script(route_id: str) -> str:
    """提取指定路线准备步骤中的唯一 bash 命令块。"""

    _, routes = _route_sections(GUIDE.read_text(encoding="utf-8"))
    _, steps = _step_sections(routes[route_id])
    blocks = _BASH_BLOCK.findall(steps["prerequisites"])
    if len(blocks) != 1:
        raise RuntimeError(f"{route_id} must contain exactly one bash block")
    return blocks[0]


def _write_command_shims(bin_dir: Path) -> None:
    """隔离真实网络与宿主工具，只模拟 Homebrew 官方安装边界。"""

    curl = bin_dir / "curl"
    curl.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_CURL_LOG"
cat <<'INSTALLER'
set -eu
mkdir -p "$(dirname "$FAKE_BREW_BIN")"
cat > "$FAKE_BREW_BIN" <<'BREW'
#!/bin/sh
set -eu
case "${1:-}" in
  shellenv)
    brew_dir="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
    printf 'export PATH="%s:$PATH"\n' "$brew_dir"
    ;;
  --version)
    printf '%s\n' 'Homebrew 4.0.0-test'
    ;;
  *)
    exit 2
    ;;
esac
BREW
chmod +x "$FAKE_BREW_BIN"
INSTALLER
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    git = bin_dir / "git"
    git.write_text(
        """#!/bin/sh
set -eu
if test "${1:-}" = "--version"; then
  printf '%s\n' 'git version 2.51.0-test'
  exit 0
fi
if test "${1:-}" = "rev-parse"; then
  exit 128
fi
exit 0
""",
        encoding="utf-8",
    )
    git.chmod(0o755)


def _replay_route(route_id: str, root: Path) -> dict[str, object]:
    """强制 Homebrew 缺失分支，并验证安装、shellenv 与版本探测。"""

    route_root = root / route_id.replace("|", "-")
    home = route_root / "home"
    work = route_root / "project"
    shim_bin = route_root / "shims"
    fake_brew = route_root / "homebrew" / "bin" / "brew"
    curl_log = route_root / "curl.log"
    for path in (home, work, shim_bin):
        path.mkdir(parents=True, exist_ok=True)
    _write_command_shims(shim_bin)

    script = _prerequisite_script(route_id)
    if script.count("/opt/homebrew/bin/brew") != 2:
        raise RuntimeError(f"{route_id} must use the canonical Homebrew binary twice")
    script = script.replace("/opt/homebrew/bin/brew", shlex.quote(str(fake_brew)))

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{shim_bin}:/usr/bin:/bin",
            "FAKE_BREW_BIN": str(fake_brew),
            "FAKE_CURL_LOG": str(curl_log),
        }
    )
    completed = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{route_id} prerequisite replay failed: {completed.stderr.strip()}"
        )
    curl_invocations = curl_log.read_text(encoding="utf-8").splitlines()
    if len(curl_invocations) != 1 or HOMEBREW_INSTALLER not in curl_invocations[0]:
        raise RuntimeError(f"{route_id} did not invoke the official Homebrew installer")
    if not fake_brew.is_file() or "Homebrew 4.0.0-test" not in completed.stdout:
        raise RuntimeError(f"{route_id} did not activate and verify Homebrew")
    return {
        "route_id": route_id,
        "official_installer_invocations": len(curl_invocations),
        "homebrew_activated": True,
    }


def main() -> int:
    """重放两条路线并写入可上传的结构化证据。"""

    evidence_root = (
        Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        / "posix-online-user-guide-e2e-evidence"
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ai-sdlc-macos-stock-host-") as temp:
        results = [_replay_route(route_id, Path(temp)) for route_id in ROUTE_IDS]
    evidence_path = evidence_root / "macos-stock-host-prerequisites.json"
    evidence_path.write_text(
        json.dumps({"routes": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("MACOS_STOCK_HOST_PREREQUISITES_OK")
    print(evidence_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
