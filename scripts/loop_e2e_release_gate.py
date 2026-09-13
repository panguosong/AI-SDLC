"""Run clean-project E2E checks for the five Loop Engine flows.

The script is intentionally CLI-first: it creates a fresh target repository and
invokes ai-sdlc as a subprocess for every user-facing action. It writes a
release-report bundle with raw command output, JSON summaries, and SVG terminal
captures that can be attached to release evidence.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

_LOCAL_PR_CANDIDATE_PATHS = (
    "specs/demo-loop-e2e",
    "src/app.py",
    "tests/test_app.py",
)
_WINDOWS_FRONTEND_DELIVERY_ROOTS = ("managed/frontend",)


@dataclass
class StepResult:
    index: int
    slug: str
    command: list[str]
    cwd: str
    returncode: int
    expected_returncodes: list[int]
    status: str
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    screenshot_path: str
    note: str = ""
    parsed_json: dict[str, Any] | None = None


@dataclass
class E2EResult:
    run_id: str
    platform: str
    project_root: str
    evidence_root: str
    cli: list[str]
    started_at: str
    finished_at: str = ""
    status: str = "running"
    steps: list[StepResult] = field(default_factory=list)
    key_artifacts: dict[str, str] = field(default_factory=dict)
    assertions: list[str] = field(default_factory=list)


class E2EHarness:
    def __init__(
        self,
        *,
        repo_root: Path,
        evidence_root: Path,
        project_root: Path,
        cli: list[str],
    ) -> None:
        self.repo_root = repo_root
        self.evidence_root = evidence_root
        self.project_root = project_root
        self.cli = cli
        self.step_index = 0
        self.steps_dir = evidence_root / "steps"
        self.screenshots_dir = evidence_root / "screenshots"
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.result = E2EResult(
            run_id=evidence_root.name,
            platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
            project_root=str(project_root),
            evidence_root=str(evidence_root),
            cli=cli,
            started_at=_now(),
        )

    def run(
        self,
        slug: str,
        args: list[str],
        *,
        expected: tuple[int, ...] = (0,),
        note: str = "",
        parse_json: bool = False,
        env_overrides: dict[str, str] | None = None,
    ) -> StepResult:
        return self.run_raw(
            slug,
            [*self.cli, *args],
            cwd=self.project_root,
            expected=expected,
            note=note,
            parse_json=parse_json,
            env_overrides=env_overrides,
        )

    def run_raw(
        self,
        slug: str,
        command: list[str],
        *,
        cwd: Path | None = None,
        expected: tuple[int, ...] = (0,),
        note: str = "",
        parse_json: bool = False,
        env_overrides: dict[str, str] | None = None,
    ) -> StepResult:
        self.step_index += 1
        command_cwd = cwd or self.project_root
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        if env_overrides:
            env.update(env_overrides)
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=command_cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            env=env,
        )
        duration = time.monotonic() - started
        prefix = f"{self.step_index:02d}_{slug}"
        stdout_path = self.steps_dir / f"{prefix}.stdout.txt"
        stderr_path = self.steps_dir / f"{prefix}.stderr.txt"
        screenshot_path = self.screenshots_dir / f"{prefix}.svg"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        parsed_json: dict[str, Any] | None = None
        if parse_json and completed.stdout.strip():
            parsed = json.loads(completed.stdout)
            if not isinstance(parsed, dict):
                raise AssertionError(f"{slug} JSON output was not an object")
            parsed_json = parsed
            (self.steps_dir / f"{prefix}.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        status = "PASS" if completed.returncode in expected else "FAIL"
        self._write_terminal_svg(
            screenshot_path,
            command=command,
            returncode=completed.returncode,
            expected=expected,
            stdout=completed.stdout,
            stderr=completed.stderr,
            status=status,
            note=note,
        )
        step = StepResult(
            index=self.step_index,
            slug=slug,
            command=command,
            cwd=str(command_cwd),
            returncode=completed.returncode,
            expected_returncodes=list(expected),
            status=status,
            duration_seconds=round(duration, 3),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            screenshot_path=str(screenshot_path),
            note=note,
            parsed_json=parsed_json,
        )
        self.result.steps.append(step)
        if status != "PASS":
            raise AssertionError(
                f"{slug} exited {completed.returncode}; expected {expected}. "
                f"stdout={stdout_path} stderr={stderr_path}"
            )
        return step

    def assert_true(self, label: str, value: bool) -> None:
        if not value:
            raise AssertionError(label)
        self.result.assertions.append(label)

    def _write_terminal_svg(
        self,
        path: Path,
        *,
        command: list[str],
        returncode: int,
        expected: tuple[int, ...],
        stdout: str,
        stderr: str,
        status: str,
        note: str,
    ) -> None:
        console = Console(record=True, width=132, file=io.StringIO())
        command_text = shlex.join(command)
        console.print(f"[bold cyan]$ {command_text}[/bold cyan]")
        console.print(
            f"[bold]status:[/bold] {status}  "
            f"[bold]exit:[/bold] {returncode}  "
            f"[bold]expected:[/bold] {list(expected)}"
        )
        if note:
            console.print(f"[bold]note:[/bold] {note}")
        if stdout:
            console.print("[bold green]stdout[/bold green]")
            console.print(_clip(stdout))
        if stderr:
            console.print("[bold red]stderr[/bold red]")
            console.print(_clip(stderr))
        console.save_svg(str(path), title=path.stem)

    def write_summary(self) -> None:
        self.result.finished_at = _now()
        self.result.status = (
            "PASS"
            if all(step.status == "PASS" for step in self.result.steps)
            else "FAIL"
        )
        summary = asdict(self.result)
        (self.evidence_root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.evidence_root / "report.md").write_text(
            self._render_report(summary),
            encoding="utf-8",
        )

    def _render_report(self, summary: dict[str, Any]) -> str:
        lines = [
            "# Loop Engine Clean-Environment E2E Release Gate Report",
            "",
            f"- Status: `{summary['status']}`",
            f"- Run ID: `{summary['run_id']}`",
            f"- Platform: `{summary['platform']}`",
            f"- Started: `{summary['started_at']}`",
            f"- Finished: `{summary['finished_at']}`",
            f"- Clean project: `{summary['project_root']}`",
            f"- Evidence root: `{summary['evidence_root']}`",
            f"- CLI: `{' '.join(summary['cli'])}`",
            "",
            "## Scope",
            "",
            "- Requirement loop: missing acceptance blocks freeze; valid acceptance freezes.",
            "- Design-contract loop: incomplete contract blocks close; repaired contract closes.",
            "- Implementation loop: missing task evidence blocks close; recorded evidence closes.",
            "- Frontend-evidence loop: missing browser artifact blocks start; valid artifact closes.",
            "- Windows frontend provider path: no Codex/no local Playwright, doctor-recommended Playwright install, Chromium smoke.",
            "- Windows Playwright evidence path: installed Playwright runs frontend-evidence capture, handles first-run baseline when needed, materializes browser evidence, and closes frontend-evidence.",
            "- No-install frontend path: provider tooling unavailable, explicit skip records audit and waits for expert review.",
            "- Local PR review loop: mock adversarial finding forces fix/rerun; clean rerun closes and attests.",
            "",
            "## Step Results",
            "",
            "| # | Step | Status | Exit | Expected | Screenshot |",
            "|---:|---|---|---:|---|---|",
        ]
        for step in self.result.steps:
            shot = Path(step.screenshot_path).name
            lines.append(
                f"| {step.index} | `{step.slug}` | `{step.status}` | "
                f"{step.returncode} | `{step.expected_returncodes}` | "
                f"[{shot}](screenshots/{shot}) |"
            )
        lines.extend(["", "## Key Artifacts", ""])
        for key, value in self.result.key_artifacts.items():
            lines.append(f"- `{key}`: `{value}`")
        lines.extend(["", "## Assertions", ""])
        for assertion in self.result.assertions:
            lines.append(f"- PASS: {assertion}")
        lines.append("")
        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument(
        "--cli",
        default="",
        help="CLI command to invoke. Defaults to '<current python> -m ai_sdlc'.",
    )
    parser.add_argument(
        "--include-windows-playwright-provider-e2e",
        action="store_true",
        help=(
            "On Windows, verify frontend-evidence doctor Playwright install "
            "commands and the no-install skip path."
        ),
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    run_id = "loop-e2e-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = (
        args.evidence_root
        or repo_root / ".ai-sdlc" / "artifacts" / "loop-e2e-release-gate" / run_id
    ).resolve()
    if args.project_root:
        project_root = args.project_root.resolve()
        project_root.mkdir(parents=True, exist_ok=True)
        temp_dir: tempfile.TemporaryDirectory[str] | None = None
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="ai-sdlc-loop-e2e-")
        project_root = Path(temp_dir.name) / "clean-loop-project"
        project_root.mkdir(parents=True, exist_ok=True)
    cli = shlex.split(args.cli) if args.cli else [sys.executable, "-m", "ai_sdlc"]
    evidence_root.mkdir(parents=True, exist_ok=True)

    harness = E2EHarness(
        repo_root=repo_root,
        evidence_root=evidence_root,
        project_root=project_root,
        cli=cli,
    )
    try:
        run_scenario(
            harness,
            include_windows_playwright_provider_e2e=(
                args.include_windows_playwright_provider_e2e
            ),
        )
        harness.write_summary()
        print(harness.evidence_root)
        return 0
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _review_input_and_recheck(
    h: E2EHarness,
    loop_type: str,
    loop_id: str,
    slug: str,
    *,
    record_clean: bool = False,
) -> str:
    first = h.run(
        f"{slug}_input",
        [
            "loop",
            "review",
            "--type",
            loop_type,
            "--loop-id",
            loop_id,
            "--json",
        ],
        parse_json=True,
    )
    if first.parsed_json is None:
        raise AssertionError(f"{slug} did not produce JSON")
    digest = str(first.parsed_json["input_digest"])
    recheck = h.run(
        f"{slug}_recheck",
        [
            "loop",
            "review",
            "--type",
            loop_type,
            "--loop-id",
            loop_id,
            "--expect-digest",
            digest,
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        f"{loop_type} review input remains stable before close",
        recheck.parsed_json is not None
        and recheck.parsed_json.get("input_digest") == digest,
    )
    if record_clean:
        _record_clean_review(h, loop_type, loop_id, slug, first.parsed_json)
    return digest


def _record_clean_review(
    h: E2EHarness,
    loop_type: str,
    loop_id: str,
    slug: str,
    review_input: dict[str, Any],
) -> None:
    roles = review_input.get("expert_roles")
    reasons = review_input.get("expert_reasons")
    round_number = review_input.get("round_number")
    digest = review_input.get("input_digest")
    if (
        not isinstance(roles, list)
        or not roles
        or not isinstance(reasons, dict)
        or not isinstance(round_number, int)
        or not isinstance(digest, str)
    ):
        raise AssertionError(f"{slug} review input omitted selected expert metadata")

    result_dir = h.project_root / ".git" / "ai-sdlc-e2e-review-fixtures" / slug
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
            raise AssertionError(f"{slug} selected a non-string expert role")
        reason = reasons.get(role_value)
        if not isinstance(reason, str) or not reason.strip():
            raise AssertionError(f"{slug} omitted the reason for {role_value}")
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
            ["--result", result_path.relative_to(h.project_root).as_posix()]
        )
    record_args.append("--json")
    recorded = h.run(
        f"{slug}_record",
        record_args,
        parse_json=True,
        note="Test fixture records one clean completed execution per selected expert.",
    )
    h.assert_true(
        f"{loop_type} selected experts are recorded before close",
        recorded.parsed_json is not None
        and recorded.parsed_json.get("status") == "passed",
    )


def run_scenario(
    h: E2EHarness,
    *,
    include_windows_playwright_provider_e2e: bool = False,
) -> None:
    _git(h.project_root, "init", "--initial-branch=main")
    _git(h.project_root, "config", "user.email", "loop-e2e@example.com")
    _git(h.project_root, "config", "user.name", "Loop E2E")
    _write_file(h.project_root / "README.md", "# Loop E2E Clean Project\n")
    _git(h.project_root, "add", "README.md")
    _git(h.project_root, "commit", "-m", "initial clean project")

    h.run(
        "init_clean_project",
        ["init", ".", "--agent-target", "codex", "--shell", "powershell"],
        note="Initialize AI-SDLC in a fresh target repository.",
    )
    if include_windows_playwright_provider_e2e:
        _run_windows_playwright_provider_install_check(h)
    _git(h.project_root, "add", ".")
    _git(h.project_root, "commit", "-m", "initialize ai-sdlc")
    base_commit = _git(h.project_root, "rev-parse", "HEAD")
    h.result.key_artifacts["base_commit"] = base_commit

    missing_req = h.run(
        "requirement_start_missing_acceptance",
        [
            "loop",
            "requirement",
            "start",
            "--loop-id",
            "req-missing-acceptance-e2e",
            "--idea",
            "做一个前端订单审批页面",
            "--json",
        ],
        parse_json=True,
        note="Expected needs_user: no acceptance criteria were provided.",
    )
    h.assert_true(
        "Requirement loop without acceptance is needs_user",
        missing_req.parsed_json is not None
        and missing_req.parsed_json.get("status") == "needs_user",
    )
    missing_requirement_digest = _review_input_and_recheck(
        h,
        "requirement",
        "req-missing-acceptance-e2e",
        "requirement_missing_acceptance_review",
    )
    h.run(
        "requirement_freeze_missing_acceptance_blocks",
        [
            "loop",
            "requirement",
            "freeze",
            "--loop-id",
            "req-missing-acceptance-e2e",
            "--expect-review-digest",
            missing_requirement_digest,
            "--yes",
            "--json",
        ],
        expected=(1,),
        parse_json=True,
        note="Expected blocker: freeze must fail without acceptance criteria.",
    )

    req = h.run(
        "requirement_start_ready",
        [
            "loop",
            "requirement",
            "start",
            "--loop-id",
            "req-e2e",
            "--idea",
            "运营用户需要一个前端订单审批页面，支持审批流配置和浏览器验收。",
            "--acceptance",
            "审批节点可以配置",
            "--acceptance",
            "前端页面通过浏览器门禁证据",
            "--work-item-id",
            "demo-loop-e2e",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Requirement loop with acceptance is ready",
        req.parsed_json is not None and req.parsed_json.get("status") == "ready",
    )
    h.run("requirement_status_human", ["loop", "status", "--type", "requirement"])
    requirement_digest = _review_input_and_recheck(
        h,
        "requirement",
        "req-e2e",
        "requirement_review",
        record_clean=True,
    )
    req_freeze = h.run(
        "requirement_freeze",
        [
            "loop",
            "requirement",
            "freeze",
            "--loop-id",
            "req-e2e",
            "--expect-review-digest",
            requirement_digest,
            "--yes",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Requirement freeze closes the requirement loop",
        req_freeze.parsed_json is not None
        and req_freeze.parsed_json.get("loop_status") == "closed",
    )

    work_item = h.project_root / "specs" / "demo-loop-e2e"
    _write_work_item(work_item, valid_tasks=False)
    dc_blocked = h.run(
        "design_contract_check_blocked",
        [
            "loop",
            "design-contract",
            "check",
            "--wi",
            "specs/demo-loop-e2e",
            "--requirement-loop-id",
            "req-e2e",
            "--loop-id",
            "dc-e2e",
            "--json",
        ],
        parse_json=True,
        note="Initial tasks intentionally omit contract refs and verification.",
    )
    h.assert_true(
        "Design-contract check reports blockers for incomplete tasks",
        dc_blocked.parsed_json is not None
        and dc_blocked.parsed_json.get("loop_status") == "needs_fix",
    )
    blocked_design_digest = _review_input_and_recheck(
        h, "design-contract", "dc-e2e", "design_contract_blocked_review"
    )
    h.run(
        "design_contract_close_blocked",
        [
            "loop",
            "design-contract",
            "close",
            "--loop-id",
            "dc-e2e",
            "--expect-review-digest",
            blocked_design_digest,
            "--yes",
            "--json",
        ],
        expected=(1,),
        parse_json=True,
        note="Expected blocker: design contract cannot close while needs_fix.",
    )

    _write_work_item(work_item, valid_tasks=True)
    dc_ready = h.run(
        "design_contract_check_ready",
        [
            "loop",
            "design-contract",
            "check",
            "--wi",
            "specs/demo-loop-e2e",
            "--requirement-loop-id",
            "req-e2e",
            "--loop-id",
            "dc-e2e",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Design-contract check is ready for expert review after repair",
        dc_ready.parsed_json is not None
        and dc_ready.parsed_json.get("loop_status") == "needs_review",
    )
    h.run(
        "design_contract_status_human", ["loop", "status", "--type", "design-contract"]
    )
    design_digest = _review_input_and_recheck(
        h,
        "design-contract",
        "dc-e2e",
        "design_contract_review",
        record_clean=True,
    )
    dc_close = h.run(
        "design_contract_close",
        [
            "loop",
            "design-contract",
            "close",
            "--loop-id",
            "dc-e2e",
            "--expect-review-digest",
            design_digest,
            "--yes",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Design-contract close succeeds",
        dc_close.parsed_json is not None and dc_close.parsed_json.get("closed") is True,
    )

    impl_start = h.run(
        "implementation_start",
        [
            "loop",
            "implementation",
            "start",
            "--wi",
            "specs/demo-loop-e2e",
            "--design-contract-loop-id",
            "dc-e2e",
            "--loop-id",
            "impl-e2e",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Implementation loop starts from closed design-contract",
        impl_start.parsed_json is not None
        and impl_start.parsed_json.get("loop_status") == "running",
    )
    incomplete_implementation_digest = _review_input_and_recheck(
        h, "implementation", "impl-e2e", "implementation_incomplete_review"
    )
    h.run(
        "implementation_close_without_evidence_blocks",
        [
            "loop",
            "implementation",
            "close",
            "--loop-id",
            "impl-e2e",
            "--expect-review-digest",
            incomplete_implementation_digest,
            "--yes",
            "--json",
        ],
        expected=(1,),
        parse_json=True,
        note="Expected blocker: required task has not been recorded as done.",
    )

    _write_file(
        h.project_root / "src" / "app.py",
        "def approval_enabled() -> bool:\n    return True\n",
    )
    _write_file(
        h.project_root / "tests" / "test_app.py",
        "from src.app import approval_enabled\n\n\ndef test_approval_enabled():\n    assert approval_enabled() is True\n",
    )
    impl_record = h.run(
        "implementation_record_done",
        [
            "loop",
            "implementation",
            "record",
            "--loop-id",
            "impl-e2e",
            "--task-id",
            "T11",
            "--status",
            "done",
            "--verification",
            "python -m compileall src",
            "--evidence",
            "src/app.py",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Implementation record waits for executable verification",
        impl_record.parsed_json is not None
        and impl_record.parsed_json.get("done_count") == 0,
    )
    impl_verify = h.run(
        "implementation_verify_done",
        [
            "loop",
            "implementation",
            "verify",
            "--loop-id",
            "impl-e2e",
            "--task-id",
            "T11",
            "--cwd",
            ".",
            "--json",
            "--",
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "compile(Path('src/app.py').read_text(encoding='utf-8'), "
                "'src/app.py', 'exec')"
            ),
        ],
        parse_json=True,
    )
    h.assert_true(
        "Executable implementation verification marks the task done",
        impl_verify.parsed_json is not None
        and impl_verify.parsed_json.get("done_count") == 1
        and impl_verify.parsed_json.get("loop_status") == "needs_review",
    )
    h.run("implementation_status_human", ["loop", "status", "--type", "implementation"])
    implementation_digest = _review_input_and_recheck(
        h,
        "implementation",
        "impl-e2e",
        "implementation_review",
        record_clean=True,
    )
    impl_close = h.run(
        "implementation_close",
        [
            "loop",
            "implementation",
            "close",
            "--loop-id",
            "impl-e2e",
            "--expect-review-digest",
            implementation_digest,
            "--yes",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Implementation close points to frontend-evidence",
        impl_close.parsed_json is not None
        and impl_close.parsed_json.get("closed") is True
        and "frontend-evidence" in str(impl_close.parsed_json.get("next_action", "")),
    )

    fe_missing = h.run(
        "frontend_evidence_start_missing_artifact_blocks",
        [
            "loop",
            "frontend-evidence",
            "start",
            "--wi",
            "specs/demo-loop-e2e",
            "--implementation-loop-id",
            "impl-e2e",
            "--loop-id",
            "fe-e2e-missing",
            "--json",
        ],
        expected=(1,),
        parse_json=True,
        note="Expected blocker: browser gate artifact has not been materialized yet.",
    )
    h.assert_true(
        "Missing frontend artifact points to provider doctor",
        fe_missing.parsed_json is not None
        and fe_missing.parsed_json.get("next_guidance", {}).get("command")
        == "ai-sdlc loop frontend-evidence doctor",
    )
    no_install_env = (
        _no_install_env(h.evidence_root)
        if include_windows_playwright_provider_e2e
        else None
    )
    if include_windows_playwright_provider_e2e and no_install_env is not None:
        no_install_doctor = h.run(
            "frontend_evidence_doctor_playwright_no_install_tools",
            [
                "loop",
                "frontend-evidence",
                "doctor",
                "--provider",
                "playwright",
                "--frontend-dir",
                "managed/no-install-frontend",
                "--json",
            ],
            parse_json=True,
            env_overrides=no_install_env,
            note="Simulates a machine where node/npm/codex/browser tools are unavailable.",
        )
        h.assert_true(
            "Doctor reports needs_user when no install tools are available",
            no_install_doctor.parsed_json is not None
            and no_install_doctor.parsed_json.get("status") == "needs_user"
            and _provider_field(
                no_install_doctor.parsed_json,
                provider_id="playwright",
                field="package_manager_available",
            )
            is False,
        )
    fe_skip = h.run(
        "frontend_evidence_skip_browser_unavailable",
        [
            "loop",
            "frontend-evidence",
            "skip",
            "--wi",
            "specs/demo-loop-e2e",
            "--implementation-loop-id",
            "impl-e2e",
            "--loop-id",
            "fe-e2e-skip",
            "--reason",
            "Clean E2E user cannot install a browser plugin or control a browser.",
            "--yes",
            "--json",
        ],
        parse_json=True,
        env_overrides=no_install_env,
        note=(
            "No install-tool PATH is active; skip must still record audit and await review."
            if no_install_env
            else ""
        ),
    )
    h.assert_true(
        "Frontend-evidence can be skipped with explicit risk acceptance",
        fe_skip.parsed_json is not None
        and fe_skip.parsed_json.get("closed") is False
        and fe_skip.parsed_json.get("skipped") is True
        and fe_skip.parsed_json.get("loop_status") == "needs_review"
        and "loop review --type frontend-evidence"
        in str(fe_skip.parsed_json.get("next_action", "")),
    )
    fe_doctor_codex = h.run(
        "frontend_evidence_doctor_codex_browser",
        [
            "loop",
            "frontend-evidence",
            "doctor",
            "--provider",
            "codex-browser",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Codex browser provider path does not hard-push Playwright",
        fe_doctor_codex.parsed_json is not None
        and fe_doctor_codex.parsed_json.get("recommended_provider") == "codex-browser"
        and "Playwright" not in str(fe_doctor_codex.parsed_json.get("next_action", "")),
    )
    if (
        include_windows_playwright_provider_e2e
        and platform.system().lower() == "windows"
    ):
        _run_windows_playwright_generated_frontend_evidence_loop(h)
    else:
        _write_browser_gate_artifact(
            h.project_root, work_item_path="specs/demo-loop-e2e"
        )
        _run_frontend_evidence_ready_path(
            h,
            loop_id="fe-e2e",
            start_slug="frontend_evidence_start_ready",
            close_slug="frontend_evidence_close",
        )
    _write_file(
        h.project_root / ".ai-sdlc" / "project" / "config" / "loop-policy.yaml",
        "allowed_omitted_file_policy: allow-with-waiver\n",
    )
    h.result.assertions.append(
        "PR review policy explicitly waives omitted binary browser artifacts"
    )
    _git(h.project_root, "add", ".ai-sdlc")
    _git(h.project_root, "commit", "-m", "record loop execution evidence")
    h.result.key_artifacts["loop_evidence_commit"] = _git(
        h.project_root, "rev-parse", "HEAD"
    )
    _stage_local_pr_candidate(
        h.project_root,
        include_windows_frontend_delivery=(
            include_windows_playwright_provider_e2e
            and platform.system().lower() == "windows"
        ),
    )
    h.result.assertions.append(
        "Every generated delivery file is staged before Local PR Review"
    )
    h.result.key_artifacts["initial_reviewed_tree"] = _git(h.project_root, "write-tree")

    review_start = h.run(
        "pr_review_start_changes_required",
        [
            "pr-review",
            "start",
            "--base",
            base_commit,
            "--provider",
            "mock-reviewer",
            "--mock-fixture",
            "changes_required",
            "--review-id",
            "review-e2e",
            "--json",
        ],
        expected=(10,),
        parse_json=True,
        note="Expected needs_fix: adversarial review fixture returns REQUIRED finding.",
    )
    h.assert_true(
        "Local PR review enters needs_fix on REQUIRED finding",
        review_start.parsed_json is not None
        and review_start.parsed_json.get("verdict") == "changes_required"
        and review_start.parsed_json.get("next_action") == "Run ai-sdlc pr-review fix.",
    )
    h.run("local_pr_review_status_needs_fix_human", ["loop", "status"])
    review_fix = h.run("pr_review_fix", ["pr-review", "fix", "--json"], parse_json=True)
    h.assert_true(
        "Local PR review fix plan is generated",
        review_fix.parsed_json is not None
        and review_fix.parsed_json.get("status") == "ready",
    )

    _write_file(
        h.project_root / "src" / "app.py",
        "def approval_enabled() -> bool:\n    return True\n\n\ndef review_feedback_addressed() -> bool:\n    return True\n",
    )
    if review_fix.parsed_json is None:
        raise AssertionError("pr_review_fix did not produce JSON")
    _mark_resolution_fixed(Path(str(review_fix.parsed_json["resolution_path"])))
    _git(h.project_root, "add", "src/app.py")

    review_rerun = h.run(
        "pr_review_rerun_clean",
        ["pr-review", "rerun", "--mock-fixture", "clean", "--json"],
        parse_json=True,
    )
    h.assert_true(
        "Local PR review rerun becomes clean",
        review_rerun.parsed_json is not None
        and review_rerun.parsed_json.get("verdict") == "clean",
    )
    h.run("local_pr_review_status_clean_human", ["loop", "status"])
    review_verify = h.run(
        "pr_review_verify",
        [
            "pr-review",
            "verify",
            "--json",
            "--",
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "compile(Path('src/app.py').read_text(encoding='utf-8'), "
                "'src/app.py', 'exec')"
            ),
        ],
        parse_json=True,
    )
    h.assert_true(
        "Local PR review records executable verification for the reviewed tree",
        review_verify.parsed_json is not None
        and review_verify.parsed_json.get("status") == "ready",
    )
    if review_rerun.parsed_json is None:
        raise AssertionError("pr_review_rerun_clean did not produce JSON")
    local_review_loop_id = str(review_rerun.parsed_json["loop_id"])
    local_review_digest = _review_input_and_recheck(
        h,
        "local-pr-review",
        local_review_loop_id,
        "local_pr_review",
        record_clean=True,
    )
    review_commit = h.run(
        "pr_review_commit",
        [
            "pr-review",
            "commit",
            "--message",
            "deliver reviewed loop e2e tree",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Local PR review commits exactly the reviewed staged tree",
        review_commit.parsed_json is not None
        and review_commit.parsed_json.get("status") == "ready"
        and bool(review_commit.parsed_json.get("commit"))
        and review_commit.parsed_json.get("tree_oid")
        == _git(h.project_root, "rev-parse", "HEAD^{tree}"),
    )
    if review_commit.parsed_json is not None:
        h.result.key_artifacts["reviewed_delivery_commit"] = str(
            review_commit.parsed_json["commit"]
        )
    review_close = h.run(
        "pr_review_close",
        [
            "pr-review",
            "close",
            "--review-id",
            "review-e2e",
            "--loop-id",
            local_review_loop_id,
            "--expect-review-digest",
            local_review_digest,
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Local PR review closes cleanly",
        review_close.parsed_json is not None
        and review_close.parsed_json.get("status") == "closed"
        and review_close.parsed_json.get("verdict")
        in {"clean", "fully_clean", "risk_accepted"}
        and review_close.parsed_json.get("unresolved_required", 0) == 0,
    )
    h.run("loop_list_all_local_pr_review_human", ["loop", "list"])

    for key, rel in {
        "requirement_freeze": ".ai-sdlc/loops/requirement/req-e2e/requirement-freeze.json",
        "design_contract_report": ".ai-sdlc/loops/design-contract/dc-e2e/design-contract-report.json",
        "implementation_report": ".ai-sdlc/loops/implementation/impl-e2e/implementation-report.json",
        "frontend_evidence_report": ".ai-sdlc/loops/frontend-evidence/fe-e2e/frontend-evidence-report.json",
    }.items():
        path = h.project_root / rel
        h.assert_true(f"Artifact exists: {rel}", path.is_file())
        h.result.key_artifacts[key] = str(path)
    if review_close.parsed_json is None:
        raise AssertionError("pr_review_close did not produce JSON")
    final_report = Path(str(review_close.parsed_json["final_report_path"]))
    h.assert_true("Local PR final report exists", final_report.is_file())
    h.result.key_artifacts["pr_review_final_report"] = str(final_report)


def _write_work_item(work_item: Path, *, valid_tasks: bool) -> None:
    work_item.mkdir(parents=True, exist_ok=True)
    _write_file(
        work_item / "spec.md",
        "\n".join(
            [
                "# PRD：Demo Loop E2E",
                "",
                "**状态**：已冻结",
                "",
                "## 需求",
                "",
                "- **FR-E2E-001**：系统必须提供前端订单审批页面。",
                "- **FR-E2E-002**：系统必须保存浏览器证据并进入 frontend-evidence loop。",
                "",
                "## 成功标准",
                "",
                "- **SC-E2E-001**：审批节点可以配置。",
                "- **SC-E2E-002**：页面通过浏览器 gate 和本地 PR review。",
            ]
        )
        + "\n",
    )
    _write_file(
        work_item / "plan.md",
        "\n".join(
            [
                "# 实施计划",
                "",
                "## 技术背景",
                "Python demo code plus frontend/browser evidence artifact.",
                "",
                "## 阶段计划",
                "Phase 1 implements the UI-facing approval capability.",
                "",
                "## 验证策略",
                "Run compileall and browser evidence loop.",
                "",
                "## 回退方式",
                "Revert the feature commit.",
            ]
        )
        + "\n",
    )
    if valid_tasks:
        task_refs = "FR-E2E-001, FR-E2E-002, SC-E2E-001, SC-E2E-002"
        verification = "python -m compileall src"
    else:
        task_refs = "contract docs"
        verification = ""
    _write_file(
        work_item / "tasks.md",
        "\n".join(
            [
                "# 任务分解",
                "",
                "### Task 1.1 Build approval page shell",
                "- **任务编号**：T11",
                "- **优先级**：P0",
                f"- **验收标准**：Cover {task_refs}.",
                f"- **验证**：{verification}",
            ]
        )
        + "\n",
    )


def _write_browser_gate_artifact(root: Path, *, work_item_path: str) -> None:
    generated = _now()
    gate_run_id = "gate-run-e2e"
    artifact_root = f".ai-sdlc/artifacts/frontend-browser-gate/{gate_run_id}"
    screenshot_ref = f"{artifact_root}/shared-runtime/navigation-screenshot.png"
    trace_ref = f"{artifact_root}/shared-runtime/playwright-trace.zip"
    interaction_ref = f"{artifact_root}/interaction/interaction-snapshot.json"
    artifact_records = [
        _artifact_record(
            "smoke-screenshot",
            gate_run_id,
            "playwright_smoke",
            "navigation_screenshot",
            screenshot_ref,
        ),
        _artifact_record(
            "smoke-trace",
            gate_run_id,
            "playwright_smoke",
            "playwright_trace",
            trace_ref,
        ),
        _artifact_record(
            "interaction-snapshot",
            gate_run_id,
            "interaction_anti_pattern_checks",
            "interaction_snapshot",
            interaction_ref,
        ),
    ]
    for record in artifact_records:
        target = root / str(record["artifact_ref"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("frontend evidence artifact\n", encoding="utf-8")
    required_probe_set = [
        "playwright_smoke",
        "visual_expectation",
        "basic_a11y",
        "interaction_anti_pattern_checks",
    ]
    payload = {
        "generated_at": generated,
        "apply_artifact_path": ".ai-sdlc/memory/frontend-delivery/apply/latest.yaml",
        "probe_runtime_state": "completed",
        "gate_run_id": gate_run_id,
        "artifact_root": artifact_root,
        "required_probe_set": required_probe_set,
        "execution_context": {
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-e2e",
            "solution_snapshot_id": "solution-snapshot-e2e",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "readiness_subject_id": "subject-e2e",
            "effective_provider": "public-primevue",
            "effective_style_pack": "modern-saas",
            "style_fidelity_status": "verified",
            "delivery_entry_id": "vue3-public-primevue",
            "package_manager": "npm",
            "component_library_packages": ["primevue", "@primeuix/themes"],
            "required_probe_set": required_probe_set,
            "browser_entry_ref": "managed/frontend/index.html",
            "source_linkage_refs": {"apply_result_status": "ok"},
        },
        "runtime_session": {
            "probe_runtime_session_id": "session-e2e",
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-e2e",
            "solution_snapshot_id": "solution-snapshot-e2e",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "readiness_subject_id": "subject-e2e",
            "browser_entry_ref": "managed/frontend/index.html",
            "artifact_root_ref": artifact_root,
            "status": "completed",
            "started_at": generated,
            "updated_at": generated,
            "finished_at": generated,
        },
        "artifact_records": artifact_records,
        "bundle_input": {
            "bundle_id": "bundle-e2e",
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-e2e",
            "solution_snapshot_id": "solution-snapshot-e2e",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "source_artifact_ref": ".ai-sdlc/memory/frontend-delivery/apply/latest.yaml",
            "readiness_subject_id": "subject-e2e",
            "playwright_trace_refs": [trace_ref],
            "screenshot_refs": [screenshot_ref],
            "check_receipts": [
                _receipt("playwright_smoke", ["smoke-screenshot", "smoke-trace"]),
                _receipt("visual_expectation", ["smoke-screenshot"]),
                _receipt("basic_a11y", ["smoke-screenshot"]),
                _receipt("interaction_anti_pattern_checks", ["interaction-snapshot"]),
            ],
            "smoke_verdict": "pass",
            "visual_verdict": "pass",
            "a11y_verdict": "pass",
            "interaction_anti_pattern_verdict": "pass",
            "overall_gate_status": "passed",
            "blocking_reason_codes": [],
            "advisory_reason_codes": [],
            "generated_at": generated,
        },
        "overall_gate_status": "passed",
        "warnings": [],
        "plain_language_blockers": [],
        "recommended_next_steps": [],
    }
    artifact_path = (
        root / ".ai-sdlc" / "memory" / "frontend-delivery" / "browser" / "latest.yaml"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _run_frontend_evidence_ready_path(
    h: E2EHarness,
    *,
    loop_id: str,
    doctor_slug: str = "frontend_evidence_doctor_auto_artifact",
    start_slug: str,
    close_slug: str,
    status_slug: str = "frontend_evidence_status_human",
) -> None:
    fe_doctor_auto = h.run(
        doctor_slug,
        ["loop", "frontend-evidence", "doctor", "--provider", "auto", "--json"],
        parse_json=True,
    )
    h.assert_true(
        "Auto provider prefers existing browser artifact",
        fe_doctor_auto.parsed_json is not None
        and fe_doctor_auto.parsed_json.get("browser_artifact_available") is True
        and fe_doctor_auto.parsed_json.get("recommended_provider")
        == "external-artifact",
    )
    fe_start = h.run(
        start_slug,
        [
            "loop",
            "frontend-evidence",
            "start",
            "--wi",
            "specs/demo-loop-e2e",
            "--implementation-loop-id",
            "impl-e2e",
            "--loop-id",
            loop_id,
            "--json",
        ],
        expected=(0, 1),
        parse_json=True,
    )
    start_payload = fe_start.parsed_json or {}
    start_status = start_payload.get("loop_status")
    advisory_warnings = (
        start_payload.get("overall_gate_status") == "passed_with_advisories"
        and start_payload.get("execute_gate_state") == "ready"
        and start_payload.get("blocker_count") == 0
    )
    h.assert_true(
        "Frontend-evidence loop starts with valid browser artifact",
        fe_start.parsed_json is not None and start_status == "needs_review",
    )
    h.run(status_slug, ["loop", "status", "--type", "frontend-evidence"])
    frontend_digest = _review_input_and_recheck(
        h,
        "frontend-evidence",
        loop_id,
        f"{start_slug}_review",
        record_clean=True,
    )
    close_args = [
        "loop",
        "frontend-evidence",
        "close",
        "--loop-id",
        loop_id,
        "--expect-review-digest",
        frontend_digest,
        "--yes",
    ]
    if advisory_warnings:
        close_args.append("--allow-warnings")
    close_args.append("--json")
    fe_close = h.run(
        close_slug,
        close_args,
        parse_json=True,
    )
    h.assert_true(
        "Frontend-evidence close points to local PR review",
        fe_close.parsed_json is not None
        and fe_close.parsed_json.get("closed") is True
        and "pr-review" in str(fe_close.parsed_json.get("next_action", "")),
    )


def _run_windows_playwright_generated_frontend_evidence_loop(h: E2EHarness) -> None:
    frontend_dir_rel = "managed/frontend"
    frontend_dir = h.project_root / frontend_dir_rel
    _write_file(
        frontend_dir / "package.json",
        _frontend_package_json("frontend-loop-playwright-evidence-e2e"),
    )
    h.run_raw(
        "windows_playwright_evidence_absent_before_install",
        [
            "node",
            "-e",
            (
                "try { require.resolve('@playwright/test'); process.exit(0); } "
                "catch (error) { console.log('local @playwright/test missing'); process.exit(1); }"
            ),
        ],
        cwd=frontend_dir,
        expected=(1,),
        note="The evidence frontend target starts without local Playwright.",
    )
    doctor = h.run(
        "frontend_evidence_doctor_playwright_recommends_evidence_install",
        [
            "loop",
            "frontend-evidence",
            "doctor",
            "--provider",
            "playwright",
            "--frontend-dir",
            frontend_dir_rel,
            "--json",
        ],
        parse_json=True,
        note="Read SDLC-recommended Playwright setup commands for the evidence frontend target.",
    )
    commands = _provider_install_commands(doctor.parsed_json, provider_id="playwright")
    h.assert_true(
        "Playwright evidence doctor recommends npm install and chromium install commands",
        commands
        == [
            "npm install -D @playwright/test pixelmatch pngjs",
            "npx playwright install chromium",
        ],
    )
    for index, command in enumerate(commands, start=1):
        h.run_raw(
            f"windows_playwright_evidence_recommended_command_{index}",
            _windows_shell_command(command),
            cwd=frontend_dir,
            note=f"Executing SDLC-recommended evidence command: {command}",
        )
    doctor_after = h.run(
        "frontend_evidence_doctor_playwright_evidence_ready_after_install",
        [
            "loop",
            "frontend-evidence",
            "doctor",
            "--provider",
            "playwright",
            "--frontend-dir",
            frontend_dir_rel,
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Playwright evidence doctor reports provider available after install",
        doctor_after.parsed_json is not None
        and doctor_after.parsed_json.get("status") == "ready"
        and _provider_field(
            doctor_after.parsed_json,
            provider_id="playwright",
            field="available",
        )
        is True,
    )
    solution = h.run(
        "frontend_solution_confirm_from_explicit_project_facts",
        [
            "loop",
            "frontend-evidence",
            "solution-confirm",
            "--wi",
            "specs/demo-loop-e2e",
            "--frontend-stack",
            "vue3",
            "--provider-id",
            "custom",
            "--style-pack-id",
            "project-defined",
            "--execute",
            "--yes",
            "--json",
        ],
        parse_json=True,
        note="Persist one explicit project-fact solution before managed apply.",
    )
    h.assert_true(
        "Frontend solution is confirmed through the Loop namespace",
        solution.parsed_json is not None
        and solution.parsed_json.get("status") == "ready",
    )
    apply_result = h.run(
        "frontend_managed_apply_from_confirmed_solution",
        [
            "loop",
            "frontend-evidence",
            "apply",
            "--execute",
            "--yes",
            "--json",
        ],
        parse_json=True,
        note="Apply the confirmed solution through the retained managed executor.",
    )
    h.assert_true(
        "Frontend apply awaits browser evidence",
        apply_result.parsed_json is not None
        and apply_result.parsed_json.get("status") == "ready",
    )
    probe = h.run(
        "frontend_browser_gate_probe_execute_after_playwright_install",
        ["loop", "frontend-evidence", "capture", "--execute", "--json"],
        parse_json=True,
        note="Materialize browser evidence through the installed Playwright runtime.",
    )
    h.assert_true(
        "Browser gate probe execute command exits successfully",
        probe.returncode == 0,
    )
    artifact_path = (
        h.project_root
        / ".ai-sdlc"
        / "memory"
        / "frontend-delivery"
        / "browser"
        / "latest.yaml"
    )
    payload = _load_browser_gate_payload(artifact_path)
    passed_statuses = {"passed", "passed_with_advisories"}
    bundle_input = payload.get("bundle_input") or {}
    if bundle_input.get("overall_gate_status") not in passed_statuses:
        h.assert_true(
            "First Playwright browser gate probe can request visual baseline",
            probe.parsed_json is not None
            and probe.parsed_json.get("status") == "needs_recheck"
            and "visual_baseline_missing" in probe.parsed_json.get("blockers", []),
        )
        h.run(
            "frontend_browser_gate_baseline_execute_after_playwright_probe",
            [
                "loop",
                "frontend-evidence",
                "baseline",
                "--execute",
                "--yes",
                "--json",
            ],
            parse_json=True,
            note="Materialize the first-run visual regression baseline requested by capture.",
        )
        rerun_probe = h.run(
            "frontend_browser_gate_probe_rerun_after_visual_baseline",
            ["loop", "frontend-evidence", "capture", "--execute", "--json"],
            parse_json=True,
            note="Re-run capture after baseline materialization.",
        )
        h.assert_true(
            "Browser gate probe rerun exits successfully after baseline",
            rerun_probe.returncode == 0,
        )
        payload = _load_browser_gate_payload(artifact_path)
        bundle_input = payload.get("bundle_input") or {}
    h.assert_true(
        "Playwright browser gate probe materializes frontend-ready evidence",
        isinstance(payload, dict)
        and payload.get("probe_runtime_state") == "completed"
        and bundle_input.get("overall_gate_status") in passed_statuses
        and str(payload.get("artifact_root", "")).startswith(
            ".ai-sdlc/artifacts/frontend-browser-gate/"
        ),
    )
    h.result.key_artifacts["playwright_generated_browser_gate_artifact"] = str(
        artifact_path
    )
    _run_frontend_evidence_ready_path(
        h,
        loop_id="fe-e2e",
        doctor_slug="frontend_evidence_doctor_auto_playwright_generated_artifact",
        start_slug="frontend_evidence_start_playwright_generated_artifact",
        close_slug="frontend_evidence_close_playwright_generated_artifact",
        status_slug="frontend_evidence_status_playwright_generated_human",
    )
    _cleanup_playwright_install_files(frontend_dir)
    h.result.assertions.append(
        "Playwright install files cleaned after generated frontend evidence closes"
    )


def _load_browser_gate_payload(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise AssertionError(f"browser gate artifact is not a mapping: {path}")
    return payload


def _frontend_package_json(name: str) -> str:
    return (
        json.dumps(
            {
                "name": name,
                "private": True,
                "version": "0.0.0",
                "scripts": {},
            },
            indent=2,
        )
        + "\n"
    )


def _cleanup_playwright_install_files(frontend_dir: Path) -> None:
    shutil.rmtree(frontend_dir / "node_modules", ignore_errors=True)
    for rel in ("package-lock.json", "npm-shrinkwrap.json"):
        path = frontend_dir / rel
        if path.exists():
            path.unlink()
    _write_file(
        frontend_dir / "package.json",
        _frontend_package_json("frontend-loop-playwright-evidence-e2e"),
    )


def _run_windows_playwright_provider_install_check(h: E2EHarness) -> None:
    if platform.system().lower() != "windows":
        h.result.assertions.append(
            "Skipped Windows Playwright provider install check on non-Windows host"
        )
        return

    frontend_dir = h.project_root / "managed" / "frontend-playwright-install"
    _write_file(
        frontend_dir / "package.json",
        json.dumps(
            {
                "name": "frontend-loop-playwright-install-e2e",
                "private": True,
                "version": "0.0.0",
                "scripts": {},
            },
            indent=2,
        )
        + "\n",
    )
    h.result.key_artifacts["windows_playwright_frontend_dir"] = str(frontend_dir)
    h.run_raw(
        "windows_codex_cli_absent",
        ["where.exe", "codex"],
        expected=(1,),
        note="Windows runner should not rely on a Codex browser-control CLI.",
    )
    h.run_raw("windows_node_available", ["node", "--version"])
    h.run_raw("windows_npm_available", _windows_shell_command("npm --version"))
    h.run_raw(
        "windows_local_playwright_absent_before_install",
        [
            "node",
            "-e",
            (
                "try { require.resolve('@playwright/test'); process.exit(0); } "
                "catch (error) { console.log('local @playwright/test missing'); process.exit(1); }"
            ),
        ],
        cwd=frontend_dir,
        expected=(1,),
        note="The clean frontend package starts without local Playwright.",
    )
    doctor = h.run(
        "frontend_evidence_doctor_playwright_recommends_windows_install",
        [
            "loop",
            "frontend-evidence",
            "doctor",
            "--provider",
            "playwright",
            "--frontend-dir",
            "managed/frontend-playwright-install",
            "--json",
        ],
        parse_json=True,
        note="Read SDLC-recommended Playwright setup commands from doctor output.",
    )
    commands = _provider_install_commands(doctor.parsed_json, provider_id="playwright")
    h.assert_true(
        "Playwright doctor recommends npm install and chromium install commands",
        commands
        == [
            "npm install -D @playwright/test pixelmatch pngjs",
            "npx playwright install chromium",
        ],
    )
    for index, command in enumerate(commands, start=1):
        h.run_raw(
            f"windows_playwright_recommended_command_{index}",
            _windows_shell_command(command),
            cwd=frontend_dir,
            note=f"Executing SDLC-recommended command: {command}",
        )
    h.run_raw(
        "windows_playwright_chromium_launch_smoke",
        [
            "node",
            "-e",
            (
                "const { chromium } = require('@playwright/test'); "
                "(async () => { "
                "const browser = await chromium.launch(); "
                "const page = await browser.newPage(); "
                "await page.setContent('<h1>AI-SDLC Frontend Loop</h1>'); "
                "const text = await page.textContent('h1'); "
                "await browser.close(); "
                "if (text !== 'AI-SDLC Frontend Loop') throw new Error('unexpected page text'); "
                "console.log('chromium launch ok'); "
                "})().catch((error) => { console.error(error); process.exit(1); });"
            ),
        ],
        cwd=frontend_dir,
        note="Verify the recommended install produced a usable Chromium runtime.",
    )
    doctor_after = h.run(
        "frontend_evidence_doctor_playwright_ready_after_install",
        [
            "loop",
            "frontend-evidence",
            "doctor",
            "--provider",
            "playwright",
            "--frontend-dir",
            "managed/frontend-playwright-install",
            "--json",
        ],
        parse_json=True,
    )
    h.assert_true(
        "Playwright doctor reports provider available after recommended install",
        doctor_after.parsed_json is not None
        and doctor_after.parsed_json.get("status") == "ready"
        and _provider_field(
            doctor_after.parsed_json,
            provider_id="playwright",
            field="available",
        )
        is True,
    )
    shutil.rmtree(frontend_dir, ignore_errors=True)
    h.result.assertions.append(
        "Windows Playwright install sandbox cleaned before PR review diff"
    )


def _provider_install_commands(
    payload: dict[str, Any] | None,
    *,
    provider_id: str,
) -> list[str]:
    provider = _provider_payload(payload, provider_id=provider_id)
    commands = provider.get("install_commands", [])
    if not isinstance(commands, list):
        raise AssertionError(f"{provider_id} install_commands must be a list")
    return [str(command) for command in commands]


def _provider_field(
    payload: dict[str, Any] | None,
    *,
    provider_id: str,
    field: str,
) -> object:
    return _provider_payload(payload, provider_id=provider_id).get(field)


def _provider_payload(
    payload: dict[str, Any] | None,
    *,
    provider_id: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AssertionError("doctor payload is missing")
    providers = payload.get("providers", [])
    if not isinstance(providers, list):
        raise AssertionError("doctor payload providers must be a list")
    for provider in providers:
        if isinstance(provider, dict) and provider.get("provider_id") == provider_id:
            return provider
    raise AssertionError(f"provider {provider_id} not found in doctor payload")


def _windows_shell_command(command: str) -> list[str]:
    return ["cmd.exe", "/d", "/s", "/c", command]


def _no_install_env(evidence_root: Path) -> dict[str, str]:
    empty_path = evidence_root / "no-install-path"
    empty_path.mkdir(parents=True, exist_ok=True)
    git_path = shutil.which("git")
    if git_path is None:
        raise AssertionError("Git must remain available in the no-install scenario")
    return {
        "PATH": os.pathsep.join((str(Path(git_path).parent), str(empty_path))),
        "AI_SDLC_BROWSER_PROVIDER": "",
    }


def _mark_resolution_fixed(path: Path) -> None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"resolution artifact is not an object: {path}")
    records = payload.get("finding_resolutions")
    if not isinstance(records, list) or not records:
        raise AssertionError(f"resolution artifact has no finding_resolutions: {path}")
    for record in records:
        if not isinstance(record, dict):
            raise AssertionError(f"resolution record is not an object: {path}")
        record["status"] = "fixed"
        record["reason"] = "Fixed by E2E harness after applying review feedback."
        record["evidence_refs"] = ["src/app.py"]
        record["operator"] = "loop-e2e-release-gate"
        record["resolved_at"] = _now()
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _artifact_record(
    artifact_id: str,
    gate_run_id: str,
    check_name: str,
    artifact_type: str,
    artifact_ref: str,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "gate_run_id": gate_run_id,
        "check_name": check_name,
        "artifact_type": artifact_type,
        "artifact_ref": artifact_ref,
        "capture_status": "captured",
        "captured_at": _now(),
    }


def _receipt(check_name: str, artifact_ids: list[str]) -> dict[str, Any]:
    return {
        "check_name": check_name,
        "started_at": _now(),
        "finished_at": _now(),
        "runtime_status": "completed",
        "artifact_ids": artifact_ids,
        "classification_candidate": "pass",
        "recheck_required": False,
        "remediation_hints": [],
        "blocking_reason_codes": [],
        "advisory_reason_codes": [],
        "requirement_linkage": [f"browser_quality_gate:{check_name}"],
    }


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _stage_local_pr_candidate(
    project_root: Path,
    *,
    include_windows_frontend_delivery: bool,
) -> None:
    candidate_paths = list(_LOCAL_PR_CANDIDATE_PATHS)
    required_paths: tuple[str, ...] = ()
    if include_windows_frontend_delivery:
        generated_paths: list[str] = []
        missing_roots: list[str] = []
        for relative_root in _WINDOWS_FRONTEND_DELIVERY_ROOTS:
            artifact_root = project_root / relative_root
            root_paths = (
                [
                    path.relative_to(project_root).as_posix()
                    for path in sorted(artifact_root.rglob("*"))
                    if path.is_file() or path.is_symlink()
                ]
                if artifact_root.is_dir()
                else []
            )
            if not root_paths:
                missing_roots.append(relative_root)
                continue
            generated_paths.extend(root_paths)
        if missing_roots:
            raise AssertionError(
                "Windows frontend delivery roots are missing or empty: "
                + ", ".join(missing_roots)
            )
        required_paths = tuple(generated_paths)
        candidate_paths.extend(_WINDOWS_FRONTEND_DELIVERY_ROOTS)

    _git(project_root, "add", *candidate_paths)
    staged_paths = set(
        _git(project_root, "diff", "--cached", "--name-only").splitlines()
    )
    missing_staged_paths = sorted(set(required_paths) - staged_paths)
    if missing_staged_paths:
        raise AssertionError(
            "Windows frontend delivery artifacts were not staged: "
            + ", ".join(missing_staged_paths)
        )

    unstaged_paths = _git(project_root, "diff", "--name-only").splitlines()
    untracked_paths = _git(
        project_root,
        "ls-files",
        "--others",
        "--exclude-standard",
    ).splitlines()
    unreviewed_paths = sorted(set(unstaged_paths) | set(untracked_paths))
    if unreviewed_paths:
        raise AssertionError(
            "Local PR candidate has unreviewed candidate paths: "
            + ", ".join(unreviewed_paths[:10])
        )


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clip(text: str, *, max_lines: int = 90, max_chars: int = 12000) -> str:
    text = textwrap.shorten(text, width=max_chars, placeholder="\n... <truncated> ...")
    lines = text.splitlines()
    if len(lines) > max_lines:
        return "\n".join([*lines[:max_lines], "... <truncated> ..."])
    return text


if __name__ == "__main__":
    raise SystemExit(main())
