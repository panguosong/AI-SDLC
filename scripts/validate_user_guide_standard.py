#!/usr/bin/env python3
"""校验 AI-SDLC 3.0.0 之后强制执行的新用户手册矩阵。"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BASELINE_VERSION = (3, 0, 0)
MATRIX_MARKER = "<!-- AI-SDLC-USER-GUIDE-MATRIX: 2x2x3=12 -->"
PROJECT_STATES = ("new", "existing")
INSTALL_CHANNELS = ("online", "offline")
PLATFORMS = ("windows-amd64", "macos-arm64", "linux-amd64")
REQUIRED_STEPS = (
    "prerequisites",
    "acquire",
    "verify",
    "install",
    "initialize",
    "success",
    "recover",
)
EXPECTED_ROUTE_IDS = tuple(
    f"{state}|{channel}|{platform}"
    for state in PROJECT_STATES
    for channel in INSTALL_CHANNELS
    for platform in PLATFORMS
)
LINUX_PYTHON_BOOTSTRAP_BOUNDARY_MARKERS = (
    "Debian GNU/Linux 12 (bookworm)",
    "amd64/x86_64",
    "glibc",
    "Python 3.11+",
    "ai-sdlc-offline-3.2.2-linux-amd64.tar.gz",
    "路线 6/12",
    "非 AMD64 或非 glibc",
    "v3.2.2 没有兼容的 Linux 发行资产",
    "不得使用路线 6/12 的 AMD64 离线包",
)
LINUX_OFFLINE_COMPATIBILITY_GATE_MARKERS = (
    "detect_linux_libc()",
    "command -v getconf",
    "LC_ALL=C getconf GNU_LIBC_VERSION",
    "getconf_glibc_re='^glibc [0-9]+\\.[0-9]+$'",
    "/lib64/ld-linux-x86-64.so.2",
    "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
    'LC_ALL=C "$loader" --version',
    "loader_glibc_re='^ld\\.so",
    "(GNU libc|[^)]+ GLIBC [0-9][^)]*)",
    "[0-9]+\\.[0-9]+\\.?$'",
    "command -v ldd",
    "LC_ALL=C ldd --version",
    "ldd_musl_re='^musl libc",
    'if [ "$musl_seen" -eq 1 ]',
    'if [ "$glibc_seen" -eq 1 ]',
    'ARCH="$(uname -m)"',
    'LIBC="$(detect_linux_libc)"',
    'if { [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "amd64" ]; } ||',
    '[ "$LIBC" = "musl" ]; then',
    'echo "停止：v3.2.2 没有与此主机兼容的 Linux 发行资产；'
    '不得使用 ai-sdlc-offline-3.2.2-linux-amd64.tar.gz。" >&2',
    '[ "$LIBC" != "glibc" ]; then',
    "无法确定此主机使用的 libc",
    "为避免误装，未下载、解压或安装",
    "  exit 1\nfi",
)

_VERSION_PATTERN = re.compile(
    r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)(?:[^\"]*)"\s*$', re.MULTILINE
)
_ROUTE_PATTERN = re.compile(r"<!--\s*AI-SDLC-USER-GUIDE-ROUTE:\s*([^\s]+)\s*-->")
_STEP_PATTERN = re.compile(r"<!--\s*AI-SDLC-USER-GUIDE-STEP:\s*([a-z-]+)\s*-->")


@dataclass(frozen=True)
class Finding:
    """一项用户手册发布合同违规。"""

    marker: str
    excerpt: str


def parse_project_version(pyproject_text: str) -> tuple[int, int, int] | None:
    """返回用于激活合同的三段式项目版本。"""

    match = _VERSION_PATTERN.search(pyproject_text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def validate_standard_text(text: str) -> list[Finding]:
    """在合同正式生效前也保证所记录的规范本身完整。"""

    findings: list[Finding] = []
    required_markers = (
        "2 × 2 × 3 = 12",
        "3.0.0` 之后的首个版本",
        MATRIX_MARKER,
        *EXPECTED_ROUTE_IDS,
        *(f"AI-SDLC-USER-GUIDE-STEP: {step}" for step in REQUIRED_STEPS),
        "不得发布",
    )
    for marker in required_markers:
        if marker not in text:
            findings.append(Finding("standard-marker-missing", marker))
    return findings


def _route_sections(text: str) -> tuple[list[str], dict[str, str]]:
    matches = list(_ROUTE_PATTERN.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.setdefault(match.group(1), text[match.end() : end])
    return [match.group(1) for match in matches], sections


def _step_sections(text: str) -> tuple[list[str], dict[str, str]]:
    matches = list(_STEP_PATTERN.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.setdefault(match.group(1), text[match.end() : end])
    return [match.group(1) for match in matches], sections


def validate_guide_text(text: str, *, version: tuple[int, int, int]) -> list[Finding]:
    """合同生效后校验 12 条可独立执行的路线。"""

    if version <= BASELINE_VERSION:
        return []

    findings: list[Finding] = []
    if text.count(MATRIX_MARKER) != 1:
        findings.append(
            Finding("guide-matrix-marker-count", str(text.count(MATRIX_MARKER)))
        )

    route_ids, sections = _route_sections(text)
    counts = Counter(route_ids)
    for route_id in EXPECTED_ROUTE_IDS:
        if counts[route_id] != 1:
            findings.append(
                Finding("guide-route-marker-count", f"{route_id}={counts[route_id]}")
            )
    for route_id in sorted(set(route_ids) - set(EXPECTED_ROUTE_IDS)):
        findings.append(Finding("guide-route-unknown", route_id))

    for route_id in EXPECTED_ROUTE_IDS:
        section = sections.get(route_id)
        if section is None:
            continue
        step_ids, step_sections = _step_sections(section)
        steps = tuple(step_ids)
        if steps != REQUIRED_STEPS:
            findings.append(
                Finding(
                    "guide-route-step-order",
                    f"{route_id}: {','.join(steps) or 'none'}",
                )
            )

        state, channel, platform = route_id.split("|")
        installer_name, path_flag = {
            ("online", "windows-amd64"): ("install_online.ps1", "-AddToPath"),
            ("online", "macos-arm64"): ("install_online.sh", "--add-to-path"),
            ("online", "linux-amd64"): ("install_online.sh", "--add-to-path"),
            ("offline", "windows-amd64"): ("install_offline.ps1", "-AddToPath"),
            ("offline", "macos-arm64"): ("install_offline.sh", "--add-to-path"),
            ("offline", "linux-amd64"): ("install_offline.sh", "--add-to-path"),
        }[(channel, platform)]
        step_requirements: dict[str, tuple[str, ...]] = {
            "prerequisites": (platform,),
            "acquire": (installer_name,),
            "install": (installer_name, path_flag),
            "initialize": ("ai-sdlc", "-m ai_sdlc", "init ."),
            "success": ("当前结果 / Result", "下一步 / Next"),
        }
        if channel == "online":
            step_requirements["verify"] = ("--version",)
        else:
            step_requirements["verify"] = (
                ".sha256",
                {
                    "windows-amd64": "Get-FileHash",
                    "macos-arm64": "shasum -a 256",
                    "linux-amd64": "sha256sum",
                }[platform],
            )
        if state == "existing":
            step_requirements["initialize"] += ("adopt .",)

        for step, markers in step_requirements.items():
            step_text = step_sections.get(step, "")
            for marker in markers:
                if marker not in step_text:
                    findings.append(
                        Finding(
                            "guide-route-step-content-missing",
                            f"{route_id}:{step}: {marker}",
                        )
                    )

        if channel == "offline":
            acquire_text = step_sections.get("acquire", "")
            download_root_markers = (
                ("$DownloadRoot =", "New-Item", "$DownloadRoot")
                if platform == "windows-amd64"
                else ("DOWNLOAD_ROOT=", 'mkdir -p "$DOWNLOAD_ROOT"')
            )
            if any(marker not in acquire_text for marker in download_root_markers):
                findings.append(
                    Finding(
                        "guide-route-offline-acquire-download-root-missing",
                        route_id,
                    )
                )

        if state == "existing":
            initialize_text = step_sections.get("initialize", "")
            if initialize_text.rfind("init .") > initialize_text.find("adopt ."):
                findings.append(Finding("guide-route-initialize-order", route_id))
        recovery_text = step_sections.get("recover", "")
        if not re.search(r"失败|错误|不可用|停止", recovery_text):
            findings.append(Finding("guide-route-recovery-empty", route_id))
        if platform == "windows-amd64":
            direct_recovery_markers = (
                "Scripts\\ai-sdlc.exe",
                "不能安全原地替换",
                "显式 direct self-update",
                "返回非零",
                "python -m ai_sdlc",
            )
            if any(marker not in recovery_text for marker in direct_recovery_markers):
                findings.append(
                    Finding("guide-route-windows-direct-recovery-missing", route_id)
                )
        if channel == "online" and platform == "linux-amd64":
            for step_name in ("prerequisites", "recover"):
                step_text = step_sections.get(step_name, "")
                if any(
                    marker not in step_text
                    for marker in LINUX_PYTHON_BOOTSTRAP_BOUNDARY_MARKERS
                ):
                    findings.append(
                        Finding(
                            "guide-route-linux-python-bootstrap-boundary-missing",
                            f"{route_id}:{step_name}",
                        )
                    )
            prerequisite_bootstrap_markers = (
                "command -v apt-get",
                "apt-get install -y ca-certificates curl git",
                "command -v dnf",
                "dnf install -y ca-certificates curl git",
                "command -v yum",
                "yum install -y ca-certificates curl git",
                "command -v curl",
                "ca_bundle_available",
                "/etc/ssl/ca-bundle.pem",
            )
            for step_name in ("prerequisites", "recover"):
                step_text = step_sections.get(step_name, "")
                if any(
                    marker not in step_text for marker in prerequisite_bootstrap_markers
                ):
                    findings.append(
                        Finding(
                            "guide-route-linux-download-bootstrap-missing",
                            f"{route_id}:{step_name}",
                        )
                    )
        if channel == "offline" and platform == "linux-amd64":
            for step_name in ("prerequisites", "recover"):
                step_text = step_sections.get(step_name, "")
                if any(
                    marker not in step_text
                    for marker in LINUX_OFFLINE_COMPATIBILITY_GATE_MARKERS
                ):
                    findings.append(
                        Finding(
                            "guide-route-linux-offline-compatibility-gate-missing",
                            f"{route_id}:{step_name}",
                        )
                    )
            acquisition_bootstrap_markers = (
                "command -v apt-get",
                "apt-get install -y ca-certificates curl",
                "command -v dnf",
                "dnf install -y ca-certificates curl",
                "command -v yum",
                "yum install -y ca-certificates curl",
                "command -v curl",
                "ca_bundle_available",
                "/etc/ssl/ca-bundle.pem",
            )
            for step_name in ("acquire", "recover"):
                step_text = step_sections.get(step_name, "")
                if any(
                    marker not in step_text for marker in acquisition_bootstrap_markers
                ):
                    findings.append(
                        Finding(
                            "guide-route-linux-download-bootstrap-missing",
                            f"{route_id}:{step_name}",
                        )
                    )
        if state == "existing" and platform == "windows-amd64":
            for step in ("prerequisites", "success"):
                step_text = step_sections.get(step, "")
                if (
                    "git rev-parse --is-inside-work-tree" not in step_text
                    or "git status --short --untracked-files=all" not in step_text
                ):
                    findings.append(
                        Finding(
                            "guide-route-windows-git-worktree-guard-missing",
                            f"{route_id}:{step}",
                        )
                    )

    return findings


def validate_repository(root: Path) -> list[Finding]:
    """校验已记录规范，并在 3.0.0 之后激活手册门禁。"""

    findings: list[Finding] = []
    standard_path = root / "docs" / "user-guide-release-standard.zh-CN.md"
    guide_path = root / "USER_GUIDE.zh-CN.md"
    pyproject_path = root / "pyproject.toml"

    try:
        standard_text = standard_path.read_text(encoding="utf-8")
    except OSError:
        findings.append(Finding("standard-document-missing", str(standard_path)))
    else:
        findings.extend(validate_standard_text(standard_text))

    try:
        pyproject_text = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        findings.append(Finding("project-version-unreadable", str(pyproject_path)))
        return findings
    version = parse_project_version(pyproject_text)
    if version is None:
        findings.append(Finding("project-version-unparseable", str(pyproject_path)))
        return findings

    try:
        guide_text = guide_path.read_text(encoding="utf-8")
    except OSError:
        findings.append(Finding("user-guide-missing", str(guide_path)))
    else:
        findings.extend(validate_guide_text(guide_text, version=version))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """执行用户手册发布合同检查。"""

    arguments = list(argv or sys.argv[1:])
    root = Path(arguments[0] if arguments else ".").resolve()
    findings = validate_repository(root)
    for finding in findings:
        print(f"{finding.marker}: {finding.excerpt}")
    if findings:
        return 1
    print("USER_GUIDE_RELEASE_STANDARD_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
