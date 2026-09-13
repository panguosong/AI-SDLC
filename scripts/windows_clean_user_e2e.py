"""在 Windows ConPTY 中重放 AI-SDLC 普通用户完整旅程。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from windows_clean_user_e2e_support import (
    CUSTOM_SOLUTION_TOKENS,
    DEFAULT_SOLUTION_TOKENS,
    _business_hashes,
    _commit_current_state,
    _initialize_existing_repo,
    _write_existing_project,
    _write_hashes,
    _write_refined_frontend_requirement,
    _write_summary,
)
from winpty import PtyProcess
from winpty.enums import Backend

_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _plain_text(value: str) -> str:
    return _ANSI_ESCAPE.sub("", value).replace("\r", "")


def _assert_contains(text: str, *expected: str) -> None:
    searchable = " ".join(text.split())
    missing = [item for item in expected if " ".join(item.split()) not in searchable]
    if missing:
        raise AssertionError(f"输出缺少预期内容: {missing}")


def _record_clean_review(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
    *,
    loop_type: str,
    loop_id: str,
    review_payload: dict[str, object],
    slug: str,
) -> None:
    roles = review_payload.get("expert_roles")
    reasons = review_payload.get("expert_reasons")
    round_number = review_payload.get("round_number")
    digest = review_payload.get("input_digest")
    if (
        not isinstance(roles, list)
        or not roles
        or not isinstance(reasons, dict)
        or not isinstance(round_number, int)
        or not isinstance(digest, str)
    ):
        raise AssertionError("评审输入缺少所选专家元数据")

    result_dir = (
        project_root / ".git" / "ai-sdlc-windows-user-e2e-review-fixtures" / slug
    )
    result_dir.mkdir(parents=True, exist_ok=True)
    record_args = [
        "loop",
        "review-record",
        "--type",
        loop_type,
        "--loop-id",
        loop_id,
        "--expect-digest",
        digest,
    ]
    for index, role_value in enumerate(roles):
        if not isinstance(role_value, str):
            raise AssertionError("评审输入包含无效专家角色")
        reason = reasons.get(role_value)
        if not isinstance(reason, str) or not reason.strip():
            raise AssertionError(f"评审输入缺少 {role_value} 的选择原因")
        result_path = result_dir / f"round-{round_number}-expert-{index}.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "roles": [role_value],
                    "role_reasons": {role_value: reason},
                    "findings": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        record_args.extend(
            ["--result", result_path.relative_to(project_root).as_posix()]
        )
    record_args.append("--json")
    record_output = _run_cli(
        cli_path,
        record_args,
        cwd=project_root,
        evidence_path=evidence_root / f"{slug}-review-record.json",
    )
    if json.loads(record_output).get("status") != "passed":
        raise AssertionError("普通用户路径未完成所选专家评审")


class _ConPtyTranscript:
    """持续读取 ConPTY，确保只有看到真实提示后才发送用户输入。"""

    def __init__(self, process: PtyProcess) -> None:
        self.process = process
        self.text = ""
        self._chunks: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._read_forever, daemon=True)
        self._reader.start()

    def _read_forever(self) -> None:
        while True:
            try:
                chunk = self.process.read(4096)
            except EOFError:
                return
            if chunk:
                self._chunks.put(chunk)

    def _drain_once(self, timeout: float = 0.1) -> None:
        try:
            self.text += self._chunks.get(timeout=timeout)
        except queue.Empty:
            return
        while True:
            try:
                self.text += self._chunks.get_nowait()
            except queue.Empty:
                return

    def wait_for(self, expected: str, *, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain_once()
            if expected in _plain_text(self.text):
                return
            if not self.process.isalive() and self._chunks.empty():
                break
        raise AssertionError(
            f"ConPTY 未在 {timeout:.0f} 秒内显示提示: {expected}\n"
            f"--- transcript ---\n{_plain_text(self.text)}"
        )

    def collect_to_exit(self, *, timeout: float = 300.0) -> tuple[int, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain_once()
            if not self.process.isalive() and self._chunks.empty():
                self._reader.join(timeout=2)
                self._drain_once(timeout=0)
                return self.process.exitstatus, _plain_text(self.text)
        self.process.terminate(force=True)
        raise AssertionError(
            f"ConPTY 命令超过 {timeout:.0f} 秒未退出。\n"
            f"--- transcript ---\n{_plain_text(self.text)}"
        )


def _run_cli(
    cli_path: str,
    args: list[str],
    *,
    cwd: Path,
    evidence_path: Path,
) -> str:
    completed = subprocess.run(
        [cli_path, *args],
        cwd=cwd,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    output = _plain_text(completed.stdout + completed.stderr)
    evidence_path.write_text(output, encoding="utf-8")
    if completed.returncode != 0:
        raise AssertionError(
            f"公开 CLI 命令失败 ({completed.returncode}): {[cli_path, *args]}\n{output}"
        )
    return output


def _verify_codex_cli_available(evidence_root: Path) -> None:
    codex_path = shutil.which("codex")
    if not codex_path:
        raise AssertionError("干净 Windows E2E 未找到已安装的真实 Codex CLI")
    completed = subprocess.run(
        [codex_path, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    output = _plain_text(completed.stdout + completed.stderr).strip()
    (evidence_root / "codex-cli-version.txt").write_text(
        f"resolved_path={codex_path}\nversion={output}\n",
        encoding="utf-8",
    )
    if completed.returncode != 0 or "codex-cli" not in output:
        raise AssertionError(
            f"真实 Codex CLI 版本检查失败 ({completed.returncode}): {output}"
        )
    expected_version = os.environ.get("CODEX_CLI_E2E_EXPECTED_VERSION", "").strip()
    if expected_version and expected_version not in output:
        raise AssertionError(
            f"Codex CLI 版本为 {output!r}，预期包含 {expected_version!r}"
        )


def _archive_codex_adapter_files(project_root: Path, evidence_root: Path) -> None:
    sources = {
        "AGENTS.md": project_root / "AGENTS.md",
        ".ai-sdlc/project/config/project-config.yaml": (
            project_root / ".ai-sdlc" / "project" / "config" / "project-config.yaml"
        ),
    }
    archive_root = evidence_root / "codex-adapter-files"
    files: list[dict[str, str]] = []
    for relative_path, source in sources.items():
        if not source.is_file():
            raise AssertionError(f"Codex 适配证据文件不存在: {relative_path}")
        destination = archive_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "adapter_target": "codex",
        "preferred_shell": "powershell",
        "canonical_path": "AGENTS.md",
        "files": files,
    }
    (evidence_root / "codex-adapter-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_interactive_init(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
) -> str:
    clean_env = os.environ.copy()
    for name in (
        "OPENAI_CODEX",
        "CODEX_CLI_READY",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDECODE",
        "CURSOR_TRACE_ID",
        "CURSOR_AGENT",
        "VSCODE_IPC_HOOK_CLI",
        "TERM_PROGRAM",
    ):
        clean_env.pop(name, None)
    clean_env.update(
        {
            "CI": "1",
            "CONPTY_CI": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )

    process = PtyProcess.spawn(
        [cli_path, "init", "."],
        cwd=str(project_root),
        env=clean_env,
        dimensions=(45, 180),
        backend=Backend.ConPTY,
    )
    transcript = _ConPtyTranscript(process)
    transcript.wait_for("请选择当前实际用于聊天开发的 AI 代理入口")
    process.write("2\r\n")
    transcript.wait_for("请选择当前项目默认使用的命令 Shell")
    process.write("1\r\n")
    exit_code, output = transcript.collect_to_exit()
    (evidence_root / "interactive-init.txt").write_text(output, encoding="utf-8")
    if exit_code != 0:
        raise AssertionError(f"交互式 init 失败 ({exit_code})\n{output}")
    return output


def _verify_interactive_init(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
) -> None:
    init_output = _run_interactive_init(cli_path, project_root, evidence_root)
    _assert_contains(
        init_output,
        "请选择当前实际用于聊天开发的 AI 代理入口",
        "请选择当前项目默认使用的命令 Shell",
        "AI 代理入口: Codex",
        "Project shell: PowerShell",
        "当前结果 / Result",
        "下一步 / Next",
    )
    if "non-interactive fallback" in init_output or "explicit override" in init_output:
        raise AssertionError("init 未走真实交互选择路径")
    config_path = (
        project_root / ".ai-sdlc" / "project" / "config" / "project-config.yaml"
    )
    agents_path = project_root / "AGENTS.md"
    if not config_path.is_file() or not agents_path.is_file():
        raise AssertionError("init 未生成项目配置或 Codex canonical AGENTS.md")
    config_text = config_path.read_text(encoding="utf-8")
    _assert_contains(config_text, "agent_target: codex", "preferred_shell: powershell")
    agents_text = agents_path.read_text(encoding="utf-8")
    _assert_contains(
        agents_text,
        "若需求涉及前端需求、UI、页面、组件、浏览器交互或前端工程",
        "基于项目已有技术栈、约束和交付目标给出一个推荐方案",
        "至少一个可选 / 自定义方案",
        "明确区分“规范正文”“可选建议”“已经落地”",
        "等待用户明确确认",
        "通用规则不得硬编码框架、组件库、provider 或 style pack",
        "只有项目事实或用户明确选择才能确定具体方案",
    )
    forbidden_agent_tokens = (
        "进入实现前必须先给出技术栈 / 组件库建议",
        "public-primevue",
        "enterprise-vue2",
        "modern-saas",
        "data-console",
    )
    stale_tokens = [token for token in forbidden_agent_tokens if token in agents_text]
    if stale_tokens:
        raise AssertionError(
            f"Codex canonical AGENTS.md 包含旧固定栈指导: {stale_tokens}"
        )
    _archive_codex_adapter_files(project_root, evidence_root)


def _run_requirement_and_workitem_flow(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
) -> None:
    requirement = _write_refined_frontend_requirement(project_root)
    relative_requirement = requirement.relative_to(project_root).as_posix()
    start_output = _run_cli(
        cli_path,
        [
            "loop",
            "requirement",
            "start",
            "--input-file",
            relative_requirement,
            "--acceptance",
            "The approval flow is responsive and browser-tested.",
            "--work-item-id",
            "001-customer-approval-dashboard",
            "--json",
        ],
        cwd=project_root,
        evidence_path=evidence_root / "requirement-start.json",
    )
    _assert_contains(
        start_output,
        '"result": "Requirement loop started."',
        '"loop_status": "needs_review"',
    )
    status_output = _run_cli(
        cli_path,
        ["loop", "requirement", "status", "--json"],
        cwd=project_root,
        evidence_path=evidence_root / "requirement-status.json",
    )
    status_payload = json.loads(status_output)
    current_loop = status_payload.get("current_loop")
    if (
        not isinstance(current_loop, dict)
        or current_loop.get("status") != "needs_review"
        or status_payload.get("blocker") != "review-result-missing"
    ):
        raise AssertionError("Requirement 状态未投影缺失专家结果的稳定阻断字段")
    requirement_payload = json.loads(start_output)
    requirement_loop_id = str(requirement_payload["loop_id"])
    review_output = _run_cli(
        cli_path,
        [
            "loop",
            "review",
            "--type",
            "requirement",
            "--loop-id",
            requirement_loop_id,
            "--json",
        ],
        cwd=project_root,
        evidence_path=evidence_root / "requirement-review.json",
    )
    requirement_review_payload = json.loads(review_output)
    requirement_review_digest = str(requirement_review_payload["input_digest"])
    _record_clean_review(
        cli_path,
        project_root,
        evidence_root,
        loop_type="requirement",
        loop_id=requirement_loop_id,
        review_payload=requirement_review_payload,
        slug="requirement",
    )
    freeze_output = _run_cli(
        cli_path,
        [
            "loop",
            "requirement",
            "freeze",
            "--loop-id",
            requirement_loop_id,
            "--expect-review-digest",
            requirement_review_digest,
            "--yes",
            "--json",
        ],
        cwd=project_root,
        evidence_path=evidence_root / "requirement-freeze.json",
    )
    _assert_contains(freeze_output, '"frozen": true')
    _commit_current_state(project_root, "freeze frontend requirement")
    _initialize_workitem(cli_path, project_root, evidence_root, requirement)


def _initialize_workitem(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
    requirement: Path,
) -> None:
    _run_cli(
        cli_path,
        [
            "workitem",
            "init",
            "--title",
            "Customer Approval Dashboard",
            "--wi-id",
            "001-customer-approval-dashboard",
            "--input",
            requirement.read_text(encoding="utf-8"),
            "--related-doc",
            requirement.relative_to(project_root).as_posix(),
        ],
        cwd=project_root,
        evidence_path=evidence_root / "workitem-init.txt",
    )
    spec = project_root / "specs" / "001-customer-approval-dashboard" / "spec.md"
    if not spec.is_file():
        raise AssertionError("公开 workitem init 未生成规范目录")


def _run_default_solution(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
) -> None:
    simple_output = _run_cli(
        cli_path,
        [
            "loop",
            "frontend-evidence",
            "solution-confirm",
            "--wi",
            "specs/001-customer-approval-dashboard",
            "--dry-run",
            "--json",
        ],
        cwd=project_root,
        evidence_path=evidence_root / "solution-simple.txt",
    )
    _assert_contains(simple_output, *DEFAULT_SOLUTION_TOKENS)


def _run_custom_solution(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
) -> None:
    custom_output = _run_cli(
        cli_path,
        [
            "loop",
            "frontend-evidence",
            "solution-confirm",
            "--wi",
            "specs/001-customer-approval-dashboard",
            "--dry-run",
            "--frontend-stack",
            "vue3",
            "--provider-id",
            "public-primevue",
            "--style-pack-id",
            "data-console",
            "--json",
        ],
        cwd=project_root,
        evidence_path=evidence_root / "solution-custom.txt",
    )
    _assert_contains(custom_output, *CUSTOM_SOLUTION_TOKENS)


def _verify_no_delivery_apply(project_root: Path) -> None:
    solution_artifact = (
        project_root
        / ".ai-sdlc"
        / "memory"
        / "frontend-delivery"
        / "solution"
        / "latest.yaml"
    )
    managed_apply_root = (
        project_root / ".ai-sdlc" / "memory" / "frontend-delivery" / "apply"
    )
    if solution_artifact.exists() or managed_apply_root.exists():
        raise AssertionError(
            "dry-run 用户路径不应物化方案或执行 managed delivery apply"
        )


def run_journey(cli_path: str, project_root: Path, evidence_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    _verify_codex_cli_available(evidence_root)
    business_files = _write_existing_project(project_root)
    _initialize_existing_repo(project_root)
    hashes_before = _business_hashes(project_root, business_files)
    _write_hashes(evidence_root / "business-hashes-before.json", hashes_before)
    _verify_interactive_init(cli_path, project_root, evidence_root)
    adopt_output = _run_cli(
        cli_path,
        ["adopt", "."],
        cwd=project_root,
        evidence_path=evidence_root / "adopt-existing-project.txt",
    )
    _assert_contains(adopt_output, "接入已有项目：已生成桥接结果")
    if _business_hashes(project_root, business_files) != hashes_before:
        raise AssertionError("交互式 init/adopt 修改了已有业务文件")
    _commit_current_state(project_root, "initialize AI-SDLC")
    _run_requirement_and_workitem_flow(cli_path, project_root, evidence_root)
    _run_default_solution(cli_path, project_root, evidence_root)
    _run_custom_solution(cli_path, project_root, evidence_root)
    _verify_no_delivery_apply(project_root)
    hashes_after_all = _business_hashes(project_root, business_files)
    _write_hashes(evidence_root / "business-hashes-after.json", hashes_after_all)
    if hashes_after_all != hashes_before:
        raise AssertionError("普通用户 E2E 修改了已有业务文件")
    _write_summary(evidence_root)


def run_interactive_init_only(
    cli_path: str,
    project_root: Path,
    evidence_root: Path,
    *,
    allow_existing_project: bool = False,
) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    if not allow_existing_project and any(project_root.iterdir()):
        raise AssertionError("交互式空项目 E2E 必须从空目录开始")
    _verify_interactive_init(cli_path, project_root, evidence_root)
    if not allow_existing_project and (
        (project_root / "src").exists() or (project_root / "package.json").exists()
    ):
        raise AssertionError("交互式空项目 init 生成了意外的业务示例文件")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--allow-existing-project", action="store_true")
    args = parser.parse_args()
    if args.init_only:
        run_interactive_init_only(
            args.cli,
            args.project_root.resolve(),
            args.evidence_root.resolve(),
            allow_existing_project=args.allow_existing_project,
        )
        print("WINDOWS_INTERACTIVE_INIT_E2E_PASSED")
    else:
        run_journey(args.cli, args.project_root.resolve(), args.evidence_root.resolve())
        print("WINDOWS_CLEAN_USER_E2E_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
