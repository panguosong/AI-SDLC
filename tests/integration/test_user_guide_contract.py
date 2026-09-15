"""Executable content contract for the new-user Chinese guide."""

from pathlib import Path

from scripts.validate_user_guide_standard import (
    EXPECTED_ROUTE_IDS,
    _route_sections,
    _step_sections,
    validate_guide_text,
)

from ai_sdlc.integrations.agent_target import AGENT_TARGET_OPTIONS, agent_target_label

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "USER_GUIDE.zh-CN.md"
README = ROOT / "README.md"


def guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_current_v3_0_1_guide_meets_the_final_twelve_route_contract() -> None:
    assert validate_guide_text(guide_text(), version=(3, 0, 1)) == []


def test_readme_links_every_final_new_user_route() -> None:
    readme = README.read_text(encoding="utf-8")
    guide = guide_text()

    for route_id in EXPECTED_ROUTE_IDS:
        anchor = f"route-{route_id.replace('|', '-')}"
        assert f"USER_GUIDE.zh-CN.md#{anchor}" in readme
        assert f'<a id="{anchor}"></a>' in guide


def test_readme_linux_selector_states_certified_python_bootstrap_boundary() -> None:
    readme = README.read_text(encoding="utf-8")

    for marker in (
        "已存在 Python 3.11+ 的 Linux 主机保持发行版无关的在线兼容路径",
        "缺少 Python 时，在线自动 bootstrap 仅认证 Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc",
        "其他缺少 Python 的 amd64/x86_64 + glibc Linux 主机使用路线 6/12 的 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz",
    ):
        assert marker in readme
    assert "所有 Linux AMD64 都会自动安装 Python" not in readme


def test_offline_routes_redefine_target_host_variables_before_verification() -> None:
    _, routes = _route_sections(guide_text())

    for route_id in EXPECTED_ROUTE_IDS:
        state, channel, platform = route_id.split("|")
        if channel != "offline":
            continue
        _, steps = _step_sections(routes[route_id])
        verify = steps["verify"]
        if platform == "windows-amd64":
            required = (
                "$ProjectRoot =",
                "$InstallRoot =",
                "$DownloadRoot =",
                "$PackageName =",
            )
        else:
            required = (
                "PROJECT_ROOT=",
                "INSTALL_ROOT=",
                "DOWNLOAD_ROOT=",
                "PACKAGE_NAME=",
            )
        for marker in required:
            assert marker in verify, f"{route_id} target verify missing {marker}"


def test_offline_routes_define_download_root_inside_connected_host_acquisition() -> (
    None
):
    _, routes = _route_sections(guide_text())

    for route_id in EXPECTED_ROUTE_IDS:
        _, channel, platform = route_id.split("|")
        if channel != "offline":
            continue
        _, steps = _step_sections(routes[route_id])
        acquire = steps["acquire"]
        if platform == "windows-amd64":
            required = ("$DownloadRoot =", "New-Item", "$DownloadRoot")
        else:
            required = ("DOWNLOAD_ROOT=", 'mkdir -p "$DOWNLOAD_ROOT"')
        for marker in required:
            assert marker in acquire, f"{route_id} acquisition missing {marker}"


def test_existing_project_success_checks_include_untracked_files() -> None:
    _, routes = _route_sections(guide_text())

    for route_id in EXPECTED_ROUTE_IDS:
        if not route_id.startswith("existing|"):
            continue
        _, steps = _step_sections(routes[route_id])
        assert "git status --short --untracked-files=all" in steps["success"], route_id


def test_posix_existing_routes_recognize_linked_git_worktrees() -> None:
    _, routes = _route_sections(guide_text())

    for route_id in EXPECTED_ROUTE_IDS:
        state, _, platform = route_id.split("|")
        if state != "existing" or platform == "windows-amd64":
            continue
        _, steps = _step_sections(routes[route_id])
        assert "git rev-parse --is-inside-work-tree" in steps["success"], route_id
        assert "test -d .git" not in routes[route_id], route_id


def test_windows_existing_routes_guard_status_with_git_worktree_identity() -> None:
    _, routes = _route_sections(guide_text())

    for channel in ("online", "offline"):
        route_id = f"existing|{channel}|windows-amd64"
        _, steps = _step_sections(routes[route_id])
        for step in ("prerequisites", "success"):
            assert "git rev-parse --is-inside-work-tree" in steps[step], (
                route_id,
                step,
            )
            assert "git status --short --untracked-files=all" in steps[step], (
                route_id,
                step,
            )


def test_online_routes_check_git_in_their_own_prerequisite_step() -> None:
    _, routes = _route_sections(guide_text())

    for route_id in EXPECTED_ROUTE_IDS:
        state, channel, platform = route_id.split("|")
        if channel != "online":
            continue
        _, steps = _step_sections(routes[route_id])
        prerequisites = steps["prerequisites"]
        marker = "Get-Command git" if platform == "windows-amd64" else "command -v git"
        assert marker in prerequisites, route_id
        assert "git --version" in prerequisites, route_id


def test_macos_online_routes_bootstrap_homebrew_before_python_install() -> None:
    _, routes = _route_sections(guide_text())
    installer = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    shellenv = 'eval "$(/opt/homebrew/bin/brew shellenv)"'

    for state in ("new", "existing"):
        route_id = f"{state}|online|macos-arm64"
        _, steps = _step_sections(routes[route_id])
        assert "command -v brew" in steps["prerequisites"], route_id
        assert installer in steps["prerequisites"], route_id
        assert shellenv in steps["prerequisites"], route_id
        assert installer in steps["recover"], route_id
        assert shellenv in steps["recover"], route_id


def test_linux_online_routes_provide_executable_download_bootstrap_and_recovery() -> (
    None
):
    _, routes = _route_sections(guide_text())
    required = (
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

    for state in ("new", "existing"):
        route_id = f"{state}|online|linux-amd64"
        _, steps = _step_sections(routes[route_id])
        for step in ("prerequisites", "recover"):
            for marker in required:
                assert marker in steps[step], (route_id, step, marker)


def test_linux_offline_routes_bootstrap_connected_host_download_tools() -> None:
    _, routes = _route_sections(guide_text())
    required = (
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

    for state in ("new", "existing"):
        route_id = f"{state}|offline|linux-amd64"
        _, steps = _step_sections(routes[route_id])
        for step in ("acquire", "recover"):
            for marker in required:
                assert marker in steps[step], (route_id, step, marker)


def test_linux_offline_routes_keep_exact_asset_without_debian_only_reclassification() -> (
    None
):
    _, routes = _route_sections(guide_text())
    asset = "ai-sdlc-offline-3.2.0-linux-amd64.tar.gz"

    for route_id in (
        "new|offline|linux-amd64",
        "existing|offline|linux-amd64",
    ):
        _, steps = _step_sections(routes[route_id])
        assert asset in steps["acquire"], route_id
        assert asset in steps["verify"], route_id
        assert "Debian GNU/Linux 12 (bookworm)" not in steps["prerequisites"], route_id


def test_linux_offline_routes_gate_amd64_glibc_in_prerequisites_and_recovery() -> None:
    _, routes = _route_sections(guide_text())
    required = (
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
        "无法确定此主机使用的 libc",
        '"$ARCH" != "x86_64"',
        '"$ARCH" != "amd64"',
        '"$LIBC" != "glibc"',
        "停止：v3.2.0 没有与此主机兼容的 Linux 发行资产",
        "不得使用 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz",
        "exit 1",
        "\nfi\n",
    )

    for route_id in (
        "new|offline|linux-amd64",
        "existing|offline|linux-amd64",
    ):
        _, steps = _step_sections(routes[route_id])
        for step_name in ("prerequisites", "recover"):
            for marker in required:
                assert marker in steps[step_name], (route_id, step_name, marker)


def test_guide_is_scoped_to_two_new_user_scenarios() -> None:
    text = guide_text()
    assert "全新用户 + 全新空项目" in text
    assert "全新用户 + 已有项目" in text
    for forbidden in ("老版本升级", "从源码运行", "@main", "uv sync", "Lean Code"):
        assert forbidden not in text


def test_guide_lists_every_runtime_adapter_in_both_scenarios() -> None:
    text = guide_text()
    labels = [agent_target_label(option) for option in AGENT_TARGET_OPTIONS]
    assert labels == ["Claude Code", "Codex", "Cursor", "VS Code", "其他-通用"]
    for label in labels:
        assert text.count(label) >= 2
    assert "实际用于聊天开发的 AI 代理入口" in text
    assert "Codex + PowerShell 为默认组合" not in text


def test_guide_pins_published_assets_and_stable_output_contract() -> None:
    text = guide_text()
    for asset in (
        "ai-sdlc-offline-3.2.0-windows-amd64.zip",
        "ai-sdlc-offline-3.2.0-macos-arm64.tar.gz",
        "ai-sdlc-offline-3.2.0-linux-amd64.tar.gz",
    ):
        assert asset in text
        assert f"releases/download/v3.2.0/{asset}" in text
        assert f"{asset}.sha256" in text
    assert "releases/download/v1.0.4/" not in text
    for anchor in (
        "Offline installation completed",
        "3.2.0",
        "Initialized AI-SDLC project",
        "当前结果 / Result",
        "下一步 / Next",
        "接入已有项目：已生成桥接结果",
        "原任务文件不会被修改",
        "推荐继续点",
    ):
        assert anchor in text


def test_guide_contains_copyable_recovery_paths() -> None:
    text = guide_text()
    for command in (
        "ai-sdlc init .",
        "ai-sdlc adopt .",
        "ai-sdlc adapter select",
        "ai-sdlc adapter shell-select",
    ):
        assert command in text
    for symptom in (
        "SHA256 verification failed",
        "No module named ai_sdlc",
        "open gates",
    ):
        assert symptom in text


def test_windows_update_contract_prefers_supported_entrypoints() -> None:
    text = guide_text()

    for marker in (
        "外部 stable shim 与 `python -m ai_sdlc`",
        "Windows 运行时目录内的 direct `Scripts\\ai-sdlc.exe`",
        "零安装并让当前业务命令继续一次",
        "$ModulePython -m ai_sdlc",
        "新终端中的裸 `ai-sdlc`",
    ):
        assert marker in text
    assert "$DirectCli" not in text


def test_each_windows_route_locally_explains_direct_launcher_recovery() -> None:
    _, routes = _route_sections(guide_text())

    for state in ("new", "existing"):
        for channel in ("online", "offline"):
            route_id = f"{state}|{channel}|windows-amd64"
            _, steps = _step_sections(routes[route_id])
            recovery = steps["recover"]
            for marker in (
                "direct `Scripts\\ai-sdlc.exe`",
                "不能安全原地替换",
                "显式 direct self-update",
                "返回非零",
                "python -m ai_sdlc",
            ):
                assert marker in recovery, (route_id, marker)
