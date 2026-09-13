from pathlib import Path

import pytest
from scripts.validate_user_guide_standard import (
    EXPECTED_ROUTE_IDS,
    MATRIX_MARKER,
    parse_project_version,
    validate_guide_text,
    validate_repository,
)

ROOT = Path(__file__).resolve().parents[2]


def _complete_route(route_id: str) -> str:
    state, channel, platform = route_id.split("|")
    initialization = (
        "ai-sdlc init .\npython -m ai_sdlc init ."
        if state == "new"
        else "ai-sdlc init .\npython -m ai_sdlc init .\nai-sdlc adopt ."
    )
    installer = {
        ("online", "windows-amd64"): "install_online.ps1 -AddToPath",
        ("online", "macos-arm64"): "install_online.sh --add-to-path",
        ("online", "linux-amd64"): "install_online.sh --add-to-path",
        ("offline", "windows-amd64"): "install_offline.ps1 -AddToPath",
        ("offline", "macos-arm64"): "install_offline.sh --add-to-path",
        ("offline", "linux-amd64"): "install_offline.sh --add-to-path",
    }[(channel, platform)]
    verification = "ai-sdlc --version"
    acquisition = f"获取 {installer}\n"
    if channel == "offline":
        verification = {
            "windows-amd64": "package.sha256 Get-FileHash",
            "macos-arm64": "package.sha256 shasum -a 256",
            "linux-amd64": "package.sha256 sha256sum",
        }[platform]
        acquisition += (
            "$DownloadRoot = Join-Path $HOME Downloads\n"
            "New-Item -ItemType Directory -Force -Path $DownloadRoot\n"
            if platform == "windows-amd64"
            else 'DOWNLOAD_ROOT="$HOME/Downloads"\nmkdir -p "$DOWNLOAD_ROOT"\n'
        )
    prerequisites = f"适用平台：{platform}\n"
    success = "当前结果 / Result\n下一步 / Next\n"
    recovery = "失败时停止并按本路线恢复。\n"
    if channel == "online" and platform == "linux-amd64":
        python_bootstrap_boundary = (
            "已存在 Python 3.11+ 时，保持发行版无关的在线兼容路径。"
            "缺少 Python 3.11+ 时，自动 bootstrap 仅认证 Debian GNU/Linux 12 (bookworm) 的 "
            "amd64/x86_64 + glibc 主机。其他无 Python 的 amd64/x86_64 + glibc 主机应使用 "
            "路线 6/12 的 ai-sdlc-offline-3.1.0-linux-amd64.tar.gz。"
            "非 AMD64 或非 glibc 的 Linux 主机，v3.1.0 没有兼容的 Linux 发行资产；"
            "不得使用路线 6/12 的 AMD64 离线包。\n"
        )
        git_bootstrap = (
            "ca_bundle_available; command -v curl; "
            "/etc/ssl/ca-bundle.pem; "
            "command -v apt-get; apt-get install -y ca-certificates curl git; "
            "command -v dnf; dnf install -y ca-certificates curl git; "
            "command -v yum; yum install -y ca-certificates curl git\n"
        )
        prerequisites += python_bootstrap_boundary + git_bootstrap
        recovery += python_bootstrap_boundary + git_bootstrap
    if channel == "offline" and platform == "linux-amd64":
        offline_compatibility_check = (
            "detect_linux_libc()\n"
            "command -v getconf\n"
            "LC_ALL=C getconf GNU_LIBC_VERSION\n"
            "getconf_glibc_re='^glibc [0-9]+\\.[0-9]+$'\n"
            "/lib64/ld-linux-x86-64.so.2\n"
            "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2\n"
            'LC_ALL=C "$loader" --version\n'
            "loader_glibc_re='^ld\\.so\n"
            "(GNU libc|[^)]+ GLIBC [0-9][^)]*)\n"
            "[0-9]+\\.[0-9]+\\.?$'\n"
            "command -v ldd\n"
            "LC_ALL=C ldd --version\n"
            "ldd_musl_re='^musl libc\n"
            'if [ "$musl_seen" -eq 1 ]\n'
            'if [ "$glibc_seen" -eq 1 ]\n'
            'ARCH="$(uname -m)"\n'
            'LIBC="$(detect_linux_libc)"\n'
            'if { [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "amd64" ]; } || '
            '[ "$LIBC" = "musl" ]; then\n'
            '  echo "停止：v3.1.0 没有与此主机兼容的 Linux 发行资产；'
            '不得使用 ai-sdlc-offline-3.1.0-linux-amd64.tar.gz。" >&2\n'
            "  exit 1\n"
            "fi\n"
            'if [ "$LIBC" != "glibc" ]; then\n'
            '  echo "停止：无法确定此主机使用的 libc；为避免误装，未下载、解压或安装。" >&2\n'
            "  exit 1\n"
            "fi\n"
        )
        download_bootstrap = (
            "ca_bundle_available; command -v curl; "
            "/etc/ssl/ca-bundle.pem; "
            "command -v apt-get; apt-get install -y ca-certificates curl; "
            "command -v dnf; dnf install -y ca-certificates curl; "
            "command -v yum; yum install -y ca-certificates curl\n"
        )
        prerequisites += offline_compatibility_check
        acquisition += download_bootstrap
        recovery += offline_compatibility_check + download_bootstrap
    if platform == "windows-amd64":
        recovery += (
            "运行时目录内的 direct Scripts\\ai-sdlc.exe 不能安全原地替换；"
            "显式 direct self-update 返回非零，请使用 python -m ai_sdlc。\n"
        )
    if state == "existing" and platform == "windows-amd64":
        git_guard = (
            "git rev-parse --is-inside-work-tree\n"
            "git status --short --untracked-files=all\n"
        )
        prerequisites += git_guard
        success += git_guard
    return (
        f"<!-- AI-SDLC-USER-GUIDE-ROUTE: {route_id} -->\n"
        "<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->\n"
        f"{prerequisites}"
        "<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->\n"
        f"{acquisition}"
        "<!-- AI-SDLC-USER-GUIDE-STEP: verify -->\n"
        f"{verification}\n"
        "<!-- AI-SDLC-USER-GUIDE-STEP: install -->\n"
        f"执行 {installer}\n"
        "<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->\n"
        f"{initialization}\n"
        "<!-- AI-SDLC-USER-GUIDE-STEP: success -->\n"
        f"{success}"
        "<!-- AI-SDLC-USER-GUIDE-STEP: recover -->\n"
        f"{recovery}"
    )


def _complete_guide() -> str:
    return (
        MATRIX_MARKER
        + "\n"
        + "\n".join(_complete_route(route_id) for route_id in EXPECTED_ROUTE_IDS)
    )


def test_repository_activates_the_standard_in_v3_0_1() -> None:
    assert parse_project_version((ROOT / "pyproject.toml").read_text()) == (3, 1, 0)
    assert validate_repository(ROOT) == []


def test_first_post_v3_release_requires_all_twelve_routes() -> None:
    findings = validate_guide_text("", version=(3, 0, 1))

    route_findings = [
        finding for finding in findings if finding.marker == "guide-route-marker-count"
    ]
    assert len(route_findings) == 12
    assert any(finding.marker == "guide-matrix-marker-count" for finding in findings)


def test_complete_self_contained_matrix_passes_after_activation() -> None:
    assert validate_guide_text(_complete_guide(), version=(3, 0, 1)) == []


def test_route_cannot_delegate_required_recovery_to_shared_text() -> None:
    guide = (
        _complete_guide()
        .replace(
            "执行 install_online.ps1 -AddToPath\n",
            "执行 install_online.ps1 -AddToPath\n安装失败时停止。\n",
            1,
        )
        .replace(
            "失败时停止并按本路线恢复。",
            "参见公共章节。",
            1,
        )
    )

    findings = validate_guide_text(guide, version=(3, 1, 0))

    assert any(finding.marker == "guide-route-recovery-empty" for finding in findings)


def test_existing_route_requires_initialization_before_adoption() -> None:
    route_id = "existing|online|windows-amd64"
    route = _complete_route(route_id)
    guide = _complete_guide().replace(
        route,
        route.replace("ai-sdlc init .\n", "").replace("python -m ai_sdlc init .\n", ""),
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-step-content-missing"
        and "initialize: init ." in finding.excerpt
        for finding in findings
    )


def test_windows_online_route_rejects_posix_installer() -> None:
    guide = _complete_guide().replace(
        "install_online.ps1 -AddToPath",
        "install_online.sh --add-to-path",
        2,
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-step-content-missing"
        and "windows-amd64:acquire: install_online.ps1" in finding.excerpt
        for finding in findings
    )


def test_windows_offline_route_rejects_posix_installer() -> None:
    guide = _complete_guide().replace(
        "install_offline.ps1 -AddToPath",
        "install_offline.sh --add-to-path",
        2,
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-step-content-missing"
        and "windows-amd64:acquire: install_offline.ps1" in finding.excerpt
        for finding in findings
    )


def test_offline_route_requires_download_root_inside_acquisition_step() -> None:
    route_id = "existing|offline|macos-arm64"
    route = _complete_route(route_id)
    guide = _complete_guide().replace(
        route,
        route.replace('mkdir -p "$DOWNLOAD_ROOT"\n', ""),
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-offline-acquire-download-root-missing"
        and finding.excerpt == route_id
        for finding in findings
    )


def test_existing_route_requires_init_before_adopt() -> None:
    route_id = "existing|online|windows-amd64"
    route = _complete_route(route_id)
    guide = _complete_guide().replace(
        route,
        route.replace(
            "ai-sdlc init .\npython -m ai_sdlc init .\nai-sdlc adopt .",
            "ai-sdlc adopt .\nai-sdlc init .\npython -m ai_sdlc init .",
        ),
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(finding.marker == "guide-route-initialize-order" for finding in findings)


def test_required_content_must_stay_in_its_declared_step() -> None:
    guide = _complete_guide().replace(
        "适用平台：windows-amd64\n"
        "<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->\n"
        "获取 install_online.ps1 -AddToPath",
        "适用平台：windows-amd64，获取 install_online.ps1 -AddToPath\n"
        "<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->\n"
        "参见前置条件",
        1,
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-step-content-missing"
        and "windows-amd64:acquire: install_online.ps1" in finding.excerpt
        for finding in findings
    )


def test_every_windows_route_requires_its_own_direct_launcher_recovery() -> None:
    route_id = "new|online|windows-amd64"
    route = _complete_route(route_id)
    guide = _complete_guide().replace(
        route,
        route.replace(
            "运行时目录内的 direct Scripts\\ai-sdlc.exe 不能安全原地替换；"
            "显式 direct self-update 返回非零，请使用 python -m ai_sdlc。\n",
            "",
        ),
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-windows-direct-recovery-missing"
        and finding.excerpt == route_id
        for finding in findings
    )


def test_existing_windows_route_requires_git_worktree_guard_for_status() -> None:
    route_id = "existing|offline|windows-amd64"
    route = _complete_route(route_id)
    guide = _complete_guide().replace(
        route,
        route.replace("git rev-parse --is-inside-work-tree\n", ""),
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-windows-git-worktree-guard-missing"
        and finding.excerpt.startswith(f"{route_id}:")
        for finding in findings
    )


def test_linux_online_route_requires_executable_download_recovery() -> None:
    route_id = "new|online|linux-amd64"
    route = _complete_route(route_id)
    guide = _complete_guide().replace(
        route,
        route.replace("command -v dnf; dnf install -y ca-certificates curl git; ", ""),
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-linux-download-bootstrap-missing"
        and finding.excerpt.startswith(f"{route_id}:")
        for finding in findings
    )


@pytest.mark.parametrize(
    ("route_id", "step_name", "boundary_marker"),
    [
        (route_id, step_name, boundary_marker)
        for route_id in ("new|online|linux-amd64", "existing|online|linux-amd64")
        for step_name in ("prerequisites", "recover")
        for boundary_marker in (
            "Debian GNU/Linux 12 (bookworm)",
            "amd64/x86_64",
            "glibc",
            "Python 3.11+",
            "ai-sdlc-offline-3.1.0-linux-amd64.tar.gz",
            "路线 6/12",
            "非 AMD64 或非 glibc",
            "v3.1.0 没有兼容的 Linux 发行资产",
            "不得使用路线 6/12 的 AMD64 离线包",
        )
    ],
)
def test_linux_online_route_requires_each_python_bootstrap_boundary_marker(
    route_id: str, step_name: str, boundary_marker: str
) -> None:
    route = _complete_route(route_id)
    step_marker = f"<!-- AI-SDLC-USER-GUIDE-STEP: {step_name} -->\n"
    step_start = route.index(step_marker) + len(step_marker)
    next_step_start = route.find("<!-- AI-SDLC-USER-GUIDE-STEP:", step_start)
    step_end = len(route) if next_step_start == -1 else next_step_start
    corrupted_route = (
        route[:step_start]
        + route[step_start:step_end].replace(boundary_marker, "")
        + route[step_end:]
    )
    guide = _complete_guide().replace(
        route,
        corrupted_route,
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-linux-python-bootstrap-boundary-missing"
        and finding.excerpt == f"{route_id}:{step_name}"
        for finding in findings
    )


def test_linux_offline_route_requires_connected_host_download_recovery() -> None:
    route_id = "existing|offline|linux-amd64"
    route = _complete_route(route_id)
    guide = _complete_guide().replace(
        route,
        route.replace("command -v dnf; dnf install -y ca-certificates curl; ", ""),
    )

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-linux-download-bootstrap-missing"
        and finding.excerpt.startswith(f"{route_id}:")
        for finding in findings
    )


@pytest.mark.parametrize(
    ("route_id", "step_name", "compatibility_marker"),
    [
        (route_id, step_name, compatibility_marker)
        for route_id in ("new|offline|linux-amd64", "existing|offline|linux-amd64")
        for step_name in ("prerequisites", "recover")
        for compatibility_marker in (
            'ARCH="$(uname -m)"',
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
            '"$ARCH" != "x86_64"',
            '"$ARCH" != "amd64"',
            '"$LIBC" = "musl"',
            '"$LIBC" != "glibc"',
            "无法确定此主机使用的 libc",
            "停止：v3.1.0 没有与此主机兼容的 Linux 发行资产",
            "不得使用 ai-sdlc-offline-3.1.0-linux-amd64.tar.gz",
        )
    ],
)
def test_each_linux_offline_route_step_requires_executable_glibc_compatibility_gate(
    route_id: str, step_name: str, compatibility_marker: str
) -> None:
    route = _complete_route(route_id)
    step_marker = f"<!-- AI-SDLC-USER-GUIDE-STEP: {step_name} -->\n"
    step_start = route.index(step_marker) + len(step_marker)
    next_step_start = route.find("<!-- AI-SDLC-USER-GUIDE-STEP:", step_start)
    step_end = len(route) if next_step_start == -1 else next_step_start
    step_text = route[step_start:step_end]
    assert compatibility_marker in step_text
    corrupted_route = (
        route[:step_start]
        + step_text.replace(compatibility_marker, "", 1)
        + route[step_end:]
    )
    guide = _complete_guide().replace(route, corrupted_route)

    findings = validate_guide_text(guide, version=(3, 0, 1))

    assert any(
        finding.marker == "guide-route-linux-offline-compatibility-gate-missing"
        and finding.excerpt == f"{route_id}:{step_name}"
        for finding in findings
    )
