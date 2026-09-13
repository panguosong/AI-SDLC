"""Integration tests for project-neutral ``ai-sdlc workitem init``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from ai_sdlc.cli.main import app
from ai_sdlc.core.config import load_project_state
from ai_sdlc.core.plan_check import parse_markdown_frontmatter
from ai_sdlc.routers.bootstrap import init_project

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()

_INIT_ARGS = [
    "workitem",
    "init",
    "--title=Direct Formal Entry",
    "--wi-id=008-direct-formal-entry",
]


@pytest.fixture(autouse=True)
def _no_ide_adapter_hook() -> None:
    with patch("ai_sdlc.cli.main.run_ide_adapter_if_initialized"):
        yield


@pytest.fixture
def adapter_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_ide_adapter_hook: None,
) -> tuple[Path, list[str]]:
    root = tmp_path / "repo"
    root.mkdir()
    init_project(root)
    monkeypatch.chdir(root)
    calls: list[str] = []

    def _write(*, console: object) -> None:
        _ = console
        calls.append("adapter")
        (root / "adapter-proof.txt").write_text("consumed\n", encoding="utf-8")

    monkeypatch.setattr("ai_sdlc.cli.main.run_ide_adapter_if_initialized", _write)
    return root, calls


def _venv_executable(venv_dir: Path, name: str) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv_dir / scripts_dir / f"{name}{suffix}"


def _installed_dep_site_packages() -> Path:
    return Path(typer.__file__).resolve().parents[1]


def _dependency_overlay_site_packages(tmp_path: Path) -> Path:
    source = _installed_dep_site_packages()
    overlay = tmp_path / "dep-site"
    overlay.mkdir()

    for item in source.iterdir():
        if item.name == "ai_sdlc":
            continue
        if item.name.startswith("ai_sdlc-") and item.name.endswith(".dist-info"):
            continue
        if item.name.startswith("ai_sdlc") and item.suffix == ".pth":
            continue

        target = overlay / item.name
        try:
            target.symlink_to(item, target_is_directory=item.is_dir())
        except OSError:
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

    return overlay


def _init_git_repo(root: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _checkout_branch(root: Path, branch_name: str) -> None:
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


class TestCliWorkitemInit:
    def test_workitem_init_guides_formal_bootstrap_when_state_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        (root / ".ai-sdlc" / "project" / "config").mkdir(parents=True)
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Direct Formal Entry",
            ],
        )

        assert result.exit_code == 1
        assert "formal bootstrap" in result.output.lower()
        assert "project-state.yaml" in result.output
        assert "ai-sdlc init ." in result.output
        assert not (root / "specs").exists()

    def test_workitem_init_generates_project_workitem_docs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        _init_git_repo(root)
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Payment retry policy",
                "--wi-id",
                "008-payment-retry-policy",
                "--input",
                "支付失败后按退避策略重试",
                "--related-plan",
                "docs/payment-retry-plan.md",
                "--related-doc",
                "docs/payment-api.md",
            ],
        )
        assert result.exit_code == 0
        assert "specs/008-payment-retry-policy" in result.output
        assert "canonical formal docs" in result.output.lower()

        wi_dir = root / "specs" / "008-payment-retry-policy"
        assert (wi_dir / "spec.md").is_file()
        assert (wi_dir / "plan.md").is_file()
        assert (wi_dir / "tasks.md").is_file()
        assert (wi_dir / "task-execution-log.md").is_file()
        assert not (root / "docs" / "superpowers" / "plans").exists()

        fm, _ = parse_markdown_frontmatter(wi_dir / "tasks.md")
        assert fm["related_plan"] == "docs/payment-retry-plan.md"
        assert fm["related_doc"] == ["docs/payment-api.md"]

        spec_text = (wi_dir / "spec.md").read_text(encoding="utf-8")
        assert "Payment retry policy" in spec_text
        assert "支付失败后按退避策略重试" in spec_text
        assert "作为项目成员" in spec_text

        exec_log_text = (wi_dir / "task-execution-log.md").read_text(encoding="utf-8")
        assert "统一验证命令" in exec_log_text
        assert "代码审查结论" in exec_log_text
        assert "任务/计划同步状态" in exec_log_text
        assert "已完成 git 提交：待执行" in exec_log_text
        assert all(
            token in exec_log_text
            for token in ("merge-pending", "archived(reason)", "retained(reason)")
        )

    def test_workitem_init_leaves_existing_program_manifest_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        manifest_path = root / "program-manifest.yaml"
        manifest_path.write_text(
            'schema_version: "2"\nprogram:\n  goal: "Existing"\nspecs: []\n',
            encoding="utf-8",
        )
        before_bytes = manifest_path.read_bytes()
        before_mtime = manifest_path.stat().st_mtime_ns
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Payment retry policy",
                "--wi-id",
                "008-payment-retry-policy",
            ],
        )

        assert result.exit_code == 0
        assert manifest_path.read_bytes() == before_bytes
        assert manifest_path.stat().st_mtime_ns == before_mtime
        assert "program truth handoff" not in result.output.lower()

    def test_workitem_init_deduplicates_created_paths_display(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        spec_dir = root / "specs" / "008-direct-formal-entry"
        created_path = spec_dir / "spec.md"
        scaffold_result = SimpleNamespace(
            spec_dir=spec_dir,
            work_item_id="008-direct-formal-entry",
            created_paths=(created_path, created_path),
        )
        with (
            patch("ai_sdlc.cli.workitem_cmd.find_project_root", return_value=root),
            patch(
                "ai_sdlc.cli.workitem_cmd.WorkitemScaffolder.preview_work_item_id",
                return_value="008-direct-formal-entry",
            ),
            patch(
                "ai_sdlc.cli.workitem_cmd.WorkitemScaffolder.scaffold",
                return_value=scaffold_result,
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "workitem",
                    "init",
                    "--title",
                    "Direct Formal Entry",
                    "--wi-id",
                    "008-direct-formal-entry",
                ],
            )

        assert result.exit_code == 0
        assert result.output.count("specs/008-direct-formal-entry/spec.md") == 1

    def test_workitem_init_succeeds_on_arbitrary_existing_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        _init_git_repo(root)
        _checkout_branch(root, "developer/payment-retry")
        before_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Payment retry policy",
                "--wi-id",
                "008-payment-retry-policy",
            ],
        )

        assert result.exit_code == 0
        assert "developer/payment-retry" in result.output
        assert (root / "specs" / "008-payment-retry-policy").is_dir()
        assert (
            subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == before_branch
        )

    def test_workitem_init_succeeds_with_unrelated_tracked_modification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        _init_git_repo(root)
        dirty_path = root / "README.md"
        dirty_path.write_text("unrelated pending change\n", encoding="utf-8")
        before_dirty = dirty_path.read_bytes()
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Payment retry policy",
                "--wi-id",
                "008-payment-retry-policy",
            ],
        )

        assert result.exit_code == 0
        assert "unrelated changes" in result.output.lower()
        assert dirty_path.read_bytes() == before_dirty
        assert (root / "specs" / "008-payment-retry-policy").is_dir()

    @pytest.mark.parametrize("dirty_tree", [False, True])
    def test_workitem_init_adapter_runs_after_informational_git_status(
        self, dirty_tree: bool, adapter_receipt: tuple[Path, list[str]]
    ) -> None:
        root, calls = adapter_receipt
        _init_git_repo(root)
        dirty_path = root / "dirty.txt"
        if dirty_tree:
            dirty_path.write_text("pending\n", encoding="utf-8")

        result = runner.invoke(app, _INIT_ARGS)

        assert result.exit_code == 0
        assert calls == ["adapter"]
        assert (root / "specs" / "008-direct-formal-entry" / "spec.md").is_file()
        if dirty_tree:
            assert dirty_path.read_text(encoding="utf-8") == "pending\n"
            assert "unrelated changes" in result.output.lower()

    def test_workitem_init_missing_title_does_not_run_adapter(
        self, adapter_receipt: tuple[Path, list[str]]
    ) -> None:
        root, calls = adapter_receipt

        result = runner.invoke(app, ["workitem", "init"])

        assert result.exit_code == 2
        assert calls == []
        assert not (root / "adapter-proof.txt").exists()

    def test_workitem_non_init_runs_adapter_before_handler_once(
        self, adapter_receipt: tuple[Path, list[str]]
    ) -> None:
        root, calls = adapter_receipt
        plan = root / ".cursor" / "plans" / "p.md"
        plan.parent.mkdir(parents=True)
        plan.write_text(
            "---\ntodos:\n  - id: x\n    content: Work\n    status: pending\n---\n",
            encoding="utf-8",
        )
        _init_git_repo(root)

        result = runner.invoke(app, ["workitem", "plan-check", "--plan", str(plan)])

        assert result.exit_code == 1
        assert calls == ["adapter"]
        assert "adapter-proof.txt" in result.output

    def test_workitem_init_auto_generated_id_updates_project_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Branch Lifecycle Truth Guard",
            ],
        )
        assert result.exit_code == 0
        assert "specs/001-branch-lifecycle-truth-guard" in result.output

        state = load_project_state(root)
        assert state.next_work_item_seq == 2

    def test_workitem_init_rejects_duplicate_initialization(
        self, adapter_receipt: tuple[Path, list[str]]
    ) -> None:
        root, calls = adapter_receipt

        first = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Direct Formal Entry",
                "--wi-id",
                "008-direct-formal-entry",
            ],
        )
        assert first.exit_code == 0
        assert calls == ["adapter"]

        (root / "adapter-proof.txt").unlink()
        calls.clear()

        second = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Direct Formal Entry",
                "--wi-id",
                "008-direct-formal-entry",
            ],
        )
        assert second.exit_code == 1
        assert "already exist" in second.output.lower()
        assert calls == []
        assert not (root / "adapter-proof.txt").exists()

    def test_workitem_init_does_not_bootstrap_program_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        _init_git_repo(root)
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Payment retry policy",
                "--wi-id",
                "149-payment-retry-policy",
            ],
        )

        assert result.exit_code == 0
        assert "program truth handoff" not in result.output.lower()
        assert not (root / "program-manifest.yaml").exists()

    def test_workitem_init_skips_existing_sequences_when_project_state_lags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        existing = root / "specs" / "046-existing-formal-baseline"
        existing.mkdir(parents=True)
        (existing / "spec.md").write_text("# existing\n", encoding="utf-8")
        state_path = root / ".ai-sdlc" / "project" / "config" / "project-state.yaml"
        state_path.write_text(
            "status: initialized\n"
            "project_name: demo\n"
            "next_work_item_seq: 20\n"
            "version: '1.0'\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(root)

        result = runner.invoke(
            app,
            [
                "workitem",
                "init",
                "--title",
                "Frontend Program Final Proof Archive Orchestration Baseline",
            ],
        )

        assert result.exit_code == 0
        assert (
            "specs/047-frontend-program-final-proof-archive-orchestration-baseline"
            in result.output
        )
        assert load_project_state(root).next_work_item_seq == 48

    def test_workitem_init_requires_title(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        init_project(root)
        monkeypatch.chdir(root)

        result = runner.invoke(app, ["workitem", "init"])
        assert result.exit_code == 2

    def test_workitem_init_succeeds_from_installed_wheel_runtime(
        self,
        tmp_path: Path,
    ) -> None:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is required for installed-wheel smoke coverage")

        dist_dir = tmp_path / "dist"
        venv_dir = tmp_path / "venv"
        repo = tmp_path / "repo"

        build = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(dist_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert build.returncode == 0, build.stderr

        wheels = sorted(dist_dir.glob("ai_sdlc-*-py3-none-any.whl"))
        assert wheels, "expected uv build to emit an ai_sdlc wheel"
        wheel = wheels[-1]

        create_venv = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert create_venv.returncode == 0, create_venv.stderr

        venv_python = _venv_executable(venv_dir, "python")
        venv_cli = _venv_executable(venv_dir, "ai-sdlc")

        install = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheel)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert install.returncode == 0, install.stderr
        assert venv_cli.is_file()

        env = dict(os.environ)
        dep_site = _dependency_overlay_site_packages(tmp_path)
        env["PYTHONPATH"] = str(dep_site)

        package_root = subprocess.run(
            [str(venv_python), "-c", "import ai_sdlc; print(ai_sdlc.__file__)"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert package_root.returncode == 0, package_root.stderr
        assert str(venv_dir) in package_root.stdout.strip()

        repo.mkdir()

        init_result = subprocess.run(
            [str(venv_cli), "init", "."],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert init_result.returncode == 0, init_result.stderr

        workitem_result = subprocess.run(
            [str(venv_cli), "workitem", "init", "--title", "Installed Wheel Smoke"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert workitem_result.returncode == 0, workitem_result.stderr
        assert "specs/001-installed-wheel-smoke" in workitem_result.stdout
        wi_dir = repo / "specs" / "001-installed-wheel-smoke"
        assert (wi_dir / "spec.md").is_file()
        assert (wi_dir / "plan.md").is_file()
        assert (wi_dir / "tasks.md").is_file()
        assert (wi_dir / "task-execution-log.md").is_file()
