"""Regression checks for repository GitHub Actions workflows."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"


def test_github_workflows_are_valid_yaml() -> None:
    workflow_paths = sorted(_WORKFLOWS_DIR.glob("*.yml"))

    assert workflow_paths

    for workflow_path in workflow_paths:
        yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    pr_checks = (_WORKFLOWS_DIR / "pr-checks.yml").read_text(encoding="utf-8")
    required = (
        "fetch-depth: 0",
        "persist-credentials: false",
        "git branch --force main HEAD^1",
        'git switch --create "$GITHUB_HEAD_REF" HEAD^2',
    )
    self_development_command = (
        "uv run ai-sdlc verify constraints --profile self-development"
    )
    assert all(token in pr_checks for token in required) and pr_checks.index(
        "Pytest smoke"
    ) < pr_checks.index(required[2]) < pr_checks.index(required[3]) < pr_checks.index(
        self_development_command
    )

    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "ai-sdlc verify constraints" in readme
    assert self_development_command in readme


def test_cross_platform_core_runs_minimal_review_smoke_on_three_platforms() -> None:
    workflow_path = _WORKFLOWS_DIR / "cross-platform-core.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    matrix = workflow["jobs"]["core-smoke"]["strategy"]["matrix"]
    assert matrix["os"] == ["ubuntu-latest", "macos-latest", "windows-latest"]
    smoke = workflow_path.read_text(encoding="utf-8")
    assert "tests/unit/test_review_kernel.py" in smoke
    assert "tests/unit/test_slimming_advice.py" in smoke
    assert "stage_review" not in smoke


def test_windows_offline_smoke_workflow_covers_bundle_build_install_and_cli_checks() -> (
    None
):
    workflow_path = _WORKFLOWS_DIR / "windows-offline-smoke.yml"

    assert workflow_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert "windows-latest" in workflow
    assert "astral-sh/setup-uv@v7" in workflow
    assert "uv python install 3.11" in workflow
    assert "uv python find --managed-python 3.11" in workflow
    assert "AI_SDLC_OFFLINE_PYTHON_RUNTIME" in workflow
    assert 'AI_SDLC_OFFLINE_PYTHON_VERSIONS="3.11,3.12"' in workflow
    assert 'AI_SDLC_OFFLINE_TARGET_PLATFORM="win_amd64"' in workflow
    assert "build_offline_bundle.sh" in workflow
    assert "install_offline.ps1" in workflow
    assert "old-user-upgrade:" not in workflow
    assert "git+https://" not in workflow
    assert "ai-sdlc init . --agent-target codex --shell powershell" in workflow
    assert "当前结果 / Result" in workflow
    assert "下一步 / Next" in workflow
    assert "OPENAI_CODEX" in workflow
    assert "AI_SDLC_ADAPTER_CANONICAL_SHA256" in workflow
    assert "adapter status" in workflow
    assert "run --dry-run" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "PYTHONUTF8" in workflow
    assert "PYTHONIOENCODING" in workflow
    assert "Console]::OutputEncoding" in workflow
    assert "UTF8Encoding" in workflow
    assert "verify_offline_bundle.py" in workflow
    assert "--require-bundled-runtime" in workflow
    assert "--install-log" in workflow
    assert "WindowsPowerShell\\v1.0\\powershell.exe" in workflow
    assert (
        "-NoProfile -ExecutionPolicy Bypass -File .\\install_offline.ps1 -AddToPath"
        in workflow
    )
    assert '$cliDir = Join-Path $bundleDir.FullName ".venv\\Scripts"' in workflow
    assert "$env:Path = $cliDir + [IO.Path]::PathSeparator + $env:Path" in workflow
    assert "Get-Command ai-sdlc" in workflow
    assert "ai-sdlc --help" in workflow
    assert "Existing Artifact Probe" in workflow
    assert "recover --reconcile" in workflow


def test_posix_offline_smoke_workflow_covers_macos_linux_bundle_install_and_cli_checks() -> (
    None
):
    workflow_path = _WORKFLOWS_DIR / "posix-offline-smoke.yml"

    assert workflow_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert "macos-latest" in workflow
    assert "ubuntu-latest" in workflow
    assert "astral-sh/setup-uv@v7" in workflow
    assert "uv python install 3.11" in workflow
    assert "uv python find --managed-python 3.11" in workflow
    assert "build_offline_bundle.sh" in workflow
    assert "install_offline.sh" in workflow
    assert "install_offline.sh --add-to-path" in workflow
    assert "command -v ai-sdlc" in workflow
    assert "ai-sdlc --help" in workflow
    assert "OPENAI_CODEX" in workflow
    assert "AI_SDLC_ADAPTER_CANONICAL_SHA256" in workflow
    assert "adapter status" in workflow
    assert "run --dry-run" in workflow
    assert "posix-offline-smoke-evidence" in workflow
    assert "install.log" in workflow
    assert "help.txt" in workflow
    assert "adapter-status.txt" in workflow
    assert "run-dry-run.txt" in workflow
    assert "bundle-manifest.json" in workflow
    assert "upload-artifact" in workflow
    assert "PYTHONUTF8" in workflow
    assert "PYTHONIOENCODING" in workflow
    assert "verify_offline_bundle.py" in workflow
    assert "--require-bundled-runtime" in workflow
    assert "--install-log" in workflow


def test_loop_e2e_release_gate_covers_browser_probe_runner_changes() -> None:
    workflow_path = _WORKFLOWS_DIR / "loop-e2e-release-gate.yml"

    assert workflow_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")

    assert "scripts/loop_e2e_release_gate.py" in workflow
    assert "scripts/frontend_browser_gate_probe_runner.mjs" in workflow


def test_loop_e2e_stages_windows_frontend_delivery_artifacts_for_local_review(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(_REPO_ROOT / "scripts" / "loop_e2e_release_gate.py")
    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "loop-e2e@example.com"],
        cwd=project_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Loop E2E"],
        cwd=project_root,
        check=True,
    )
    (project_root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=project_root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initialize fixture"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    candidate_paths = (
        "specs/demo-loop-e2e/requirements.md",
        "src/app.py",
        "tests/test_app.py",
    )
    delivery_roots = tuple(script["_WINDOWS_FRONTEND_DELIVERY_ROOTS"])
    assert delivery_roots == ("managed/frontend",)
    governance_paths = tuple(
        path
        for root in delivery_roots[:-1]
        for path in (f"{root}/fixture.yaml", f"{root}/nested/fixture.yaml")
    )
    managed_paths = (
        "managed/frontend/index.html",
        "managed/frontend/package.json",
    )
    generated_paths = (*governance_paths, *managed_paths)
    for relative_path in (*candidate_paths, *governance_paths):
        path = project_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture: {relative_path}\n", encoding="utf-8")
    managed_root = project_root / "managed" / "frontend"
    (managed_root / "index.html").parent.mkdir(parents=True, exist_ok=True)
    (managed_root / "index.html").write_text(
        "<main>review me</main>\n", encoding="utf-8"
    )
    (managed_root / "package.json").write_text(
        '{"dependencies":{"temp":"1"}}\n', encoding="utf-8"
    )
    ignored_noise = managed_root / "node_modules" / "noise"
    ignored_noise.parent.mkdir(parents=True, exist_ok=True)
    ignored_noise.write_text("ignored\n", encoding="utf-8")
    (managed_root / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (managed_root / "npm-shrinkwrap.json").write_text("{}\n", encoding="utf-8")
    script["_cleanup_playwright_install_files"](managed_root)

    assert not ignored_noise.exists()
    assert not (managed_root / "package-lock.json").exists()
    assert not (managed_root / "npm-shrinkwrap.json").exists()
    assert (managed_root / "package.json").read_text(encoding="utf-8") == script[
        "_frontend_package_json"
    ]("frontend-loop-playwright-evidence-e2e")

    script["_stage_local_pr_candidate"](
        project_root,
        include_windows_frontend_delivery=True,
    )

    staged = set(
        subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    assert set(candidate_paths).issubset(staged)
    staged_delivery = {
        path
        for path in staged
        if any(path.startswith(f"{root}/") for root in delivery_roots)
    }
    assert staged_delivery == set(generated_paths)
    assert "managed/frontend/node_modules/noise" not in staged

    unexpected = project_root / "governance" / "frontend" / "unreviewed.yaml"
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_text("unreviewed: true\n", encoding="utf-8")
    managed_sibling = project_root / "managed" / "unreviewed.txt"
    managed_sibling.write_text("unreviewed\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="unreviewed candidate paths"):
        script["_stage_local_pr_candidate"](
            project_root,
            include_windows_frontend_delivery=True,
        )


def test_loop_e2e_advisory_frontend_evidence_close_allows_warnings(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(_REPO_ROOT / "scripts" / "loop_e2e_release_gate.py")

    class FakeHarness:
        def __init__(self) -> None:
            self.evidence_root = tmp_path / "evidence"
            self.evidence_root.mkdir()
            self.project_root = tmp_path / "project"
            self.project_root.mkdir()
            self.close_args: list[str] = []
            self.review_record_args: list[str] = []
            self.calls: list[str] = []

        def assert_true(self, message: str, condition: bool) -> None:
            assert condition, message

        def run(self, slug: str, args: list[str], **_kwargs: object) -> SimpleNamespace:
            self.calls.append(slug)
            if slug == "frontend_evidence_doctor_auto_artifact":
                payload = {
                    "browser_artifact_available": True,
                    "recommended_provider": "external-artifact",
                }
            elif slug == "frontend_evidence_start":
                payload = {
                    "loop_status": "needs_review",
                    "overall_gate_status": "passed_with_advisories",
                    "execute_gate_state": "ready",
                    "blocker_count": 0,
                    "warning_count": 6,
                    "next_action": (
                        "Run ai-sdlc loop review --type frontend-evidence "
                        "--loop-id frontend-e2e."
                    ),
                }
            elif slug.endswith("_review_input") or slug.endswith("_review_recheck"):
                payload = {
                    "input_digest": "stable-review-input",
                    "round_number": 1,
                    "expert_roles": ["ux-accessibility-and-evidence"],
                    "expert_reasons": {
                        "ux-accessibility-and-evidence": "Primary expert for frontend evidence."
                    },
                }
            elif slug.endswith("_review_record"):
                self.review_record_args = args
                payload = {"status": "passed"}
            elif slug == "frontend_evidence_close":
                self.close_args = args
                payload = {"closed": True, "next_action": "Run local pr-review."}
            else:
                payload = {}
            return SimpleNamespace(parsed_json=payload)

    harness = FakeHarness()
    script["_run_frontend_evidence_ready_path"](
        harness,
        loop_id="frontend-e2e",
        start_slug="frontend_evidence_start",
        close_slug="frontend_evidence_close",
    )

    assert "--allow-warnings" in harness.close_args
    assert harness.review_record_args[:7] == [
        "loop",
        "review-record",
        "--type",
        "frontend-evidence",
        "--loop-id",
        "frontend-e2e",
        "--expect-digest",
    ]
    assert harness.calls.index("frontend_evidence_start_review_record") < (
        harness.calls.index("frontend_evidence_close")
    )
    result_path = harness.project_root / Path(
        harness.review_record_args[harness.review_record_args.index("--result") + 1]
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["roles"] == ["ux-accessibility-and-evidence"]
    assert result["role_reasons"] == {
        "ux-accessibility-and-evidence": "Primary expert for frontend evidence."
    }


def test_release_artifact_smoke_workflow_installs_published_assets() -> None:
    workflow_path = _WORKFLOWS_DIR / "release-artifact-smoke.yml"

    assert workflow_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "release:" in workflow
    assert "default: v3.2.1" in workflow
    assert "gh release download" in workflow
    assert "windows-latest" in workflow
    assert "macos-latest" in workflow
    assert "ubuntu-latest" in workflow
    assert "ref: ${{ github.event.release.tag_name || inputs.tag }}" in workflow
    assert (
        "ai-sdlc-offline-$releaseVersion-windows-$env:RELEASE_ASSET_MACHINE.zip"
        in workflow
    )
    assert (
        "ai-sdlc-offline-${release_version}-${RELEASE_ASSET_OS}-${RELEASE_ASSET_MACHINE}.tar.gz"
        in workflow
    )
    assert "RELEASE_ASSET_OS" in workflow
    assert "RELEASE_ASSET_MACHINE" in workflow
    assert ".sha256" in workflow
    assert "install_offline.ps1" in workflow
    assert "./install_offline.sh" in workflow
    assert "actions/setup-python@v6" in workflow
    assert "verify_offline_bundle.py" in workflow
    assert "--require-bundled-runtime" in workflow
    assert "--require-checksums" in workflow
    assert "--expected-package-version" in workflow
    assert "--archive-checksum" in workflow
    assert "--install-log" in workflow
    assert "verify_offline_bundle.py failed with exit code" in workflow
    assert "adapter status" in workflow
    assert "run --dry-run" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "WindowsPowerShell\\v1.0\\powershell.exe" in workflow
    assert (
        "-NoProfile -ExecutionPolicy Bypass -File .\\install_offline.ps1 -AddToPath"
        in workflow
    )
    assert '$cliDir = Join-Path $bundleDir.FullName ".venv\\Scripts"' in workflow
    assert "$env:Path = $cliDir + [IO.Path]::PathSeparator + $env:Path" in workflow
    assert "Get-Command ai-sdlc" in workflow
    assert "ai-sdlc --help" in workflow
    assert "ai-sdlc --version" in workflow
    assert "install_offline.sh --add-to-path" in workflow
    assert "command -v ai-sdlc" in workflow
    windows_hash = workflow.index(
        "Get-FileHash -Algorithm SHA256 -LiteralPath $archive.FullName"
    )
    windows_extract = workflow.index("Expand-Archive -LiteralPath $archive.FullName")
    windows_verify = workflow.index("--require-checksums", windows_extract)
    windows_install = workflow.index("install_offline.ps1 -AddToPath", windows_extract)
    assert windows_hash < windows_extract < windows_verify < windows_install
    posix_hash = workflow.index('actual_archive_hash="$(')
    posix_extract = workflow.index('tar xzf "${archive}"')
    posix_verify = workflow.index("--require-checksums", posix_extract)
    posix_install = workflow.index("./install_offline.sh --add-to-path", posix_extract)
    assert posix_hash < posix_extract < posix_verify < posix_install


def test_release_build_uses_standard_cross_platform_release_flow() -> None:
    workflow_path = _WORKFLOWS_DIR / "release-build.yml"

    assert workflow_path.is_file()
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "default: v3.2.1" in workflow
    assert "ref: ${{ inputs.tag }}" in workflow
    assert 'git rev-parse "${RELEASE_TAG}^{commit}"' in workflow
    assert all(
        platform in workflow
        for platform in ("windows-latest", "macos-latest", "ubuntu-latest")
    )
    assert "build_offline_bundle.sh" in workflow
    assert "verify_offline_bundle.py" in workflow
    assert "install_offline.ps1" in workflow
    assert "./install_offline.sh" in workflow
    assert "gh release upload" in workflow
    assert "--json isDraft" in workflow
    assert "--json assets" in workflow
    assert "--clobber" not in workflow
    assert "release-satisfaction-proof" not in workflow
    assert "release-certificate" not in workflow
    assert "terminal-generation-burn" not in workflow
    assert "actions/attest" not in workflow
    parsed = yaml.safe_load(workflow)
    assert parsed["permissions"] == {"contents": "read"}
    build_job = parsed["jobs"]["build-smoke"]
    upload_job = parsed["jobs"]["upload-release-assets"]
    assert build_job["permissions"] == {"contents": "read"}
    assert "GH_TOKEN" not in build_job.get("env", {})
    checkout = next(
        step for step in build_job["steps"] if step.get("name") == "Checkout"
    )
    assert checkout["with"]["persist-credentials"] is False
    stage_assets = next(
        step
        for step in build_job["steps"]
        if step.get("name") == "Stage smoke-passed release assets"
    )
    assert stage_assets["with"]["path"].splitlines() == [
        "dist-offline/ai-sdlc-offline-*-${{ matrix.asset_os }}-${{ matrix.asset_machine }}.${{ matrix.archive }}",
        "dist-offline/ai-sdlc-offline-*-${{ matrix.asset_os }}-${{ matrix.asset_machine }}.${{ matrix.archive }}.sha256",
    ]
    assert upload_job["permissions"] == {"contents": "write"}
    assert upload_job["needs"] == "build-smoke"
    assert not any(
        "actions/checkout" in step.get("uses", "") for step in upload_job["steps"]
    )


def test_release_build_rejects_frozen_tags_and_non_main_dispatch(tmp_path) -> None:
    bash = shutil.which("bash")
    git = shutil.which("git")
    if bash is None or git is None:
        pytest.skip("The release source validation requires Bash and Git.")
    workflow = yaml.safe_load(
        (_WORKFLOWS_DIR / "release-build.yml").read_text(encoding="utf-8")
    )
    validation_script = next(
        step["run"]
        for step in workflow["jobs"]["build-smoke"]["steps"]
        if step.get("name") == "Validate exact release tag source"
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "ai-sdlc"\nversion = "3.2.1"\n',
        encoding="utf-8",
    )
    for command in (
        [git, "init", "-b", "main"],
        [git, "config", "user.name", "Release Test"],
        [git, "config", "user.email", "release@example.invalid"],
        [git, "add", "pyproject.toml"],
        [git, "commit", "-m", "release source"],
        [git, "tag", "-a", "v3.2.1", "-m", "v3.2.1"],
    ):
        subprocess.run(command, cwd=repository, check=True, capture_output=True)
    head = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    base_env = {
        **os.environ,
        "RELEASE_TAG": "v3.2.1",
        "ALLOWED_RELEASE_TAG": "v3.2.1",
        "DISPATCH_REF": "refs/heads/main",
        "DISPATCH_SHA": head,
    }

    accepted = subprocess.run(
        [bash, "-c", validation_script],
        cwd=repository,
        env=base_env,
        text=True,
        capture_output=True,
        check=False,
    )
    frozen_tag = subprocess.run(
        [bash, "-c", validation_script],
        cwd=repository,
        env={**base_env, "RELEASE_TAG": "v1.0.4"},
        text=True,
        capture_output=True,
        check=False,
    )
    non_main = subprocess.run(
        [bash, "-c", validation_script],
        cwd=repository,
        env={**base_env, "DISPATCH_REF": "refs/heads/release-candidate"},
        text=True,
        capture_output=True,
        check=False,
    )
    wrong_commit = subprocess.run(
        [bash, "-c", validation_script],
        cwd=repository,
        env={**base_env, "DISPATCH_SHA": "0" * 40},
        text=True,
        capture_output=True,
        check=False,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert frozen_tag.returncode != 0
    assert "Only v3.2.1" in frozen_tag.stderr
    assert non_main.returncode != 0
    assert wrong_commit.returncode != 0


def test_release_upload_step_is_draft_only_and_retryable(tmp_path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("The release upload step requires Bash.")
    workflow = yaml.safe_load(
        (_WORKFLOWS_DIR / "release-build.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["upload-release-assets"]["steps"]
    upload_script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Upload smoke-passed assets to release"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "release" && "$2" == "view" && "$*" == *"--json isDraft"* ]]; then
  if [[ -n "${FAKE_RELEASE_DRAFT_SEQUENCE_FILE:-}" && -s "${FAKE_RELEASE_DRAFT_SEQUENCE_FILE}" ]]; then
    head -n 1 "${FAKE_RELEASE_DRAFT_SEQUENCE_FILE}"
    tail -n +2 "${FAKE_RELEASE_DRAFT_SEQUENCE_FILE}" > "${FAKE_RELEASE_DRAFT_SEQUENCE_FILE}.next"
    mv "${FAKE_RELEASE_DRAFT_SEQUENCE_FILE}.next" "${FAKE_RELEASE_DRAFT_SEQUENCE_FILE}"
  else
    printf '%s\n' "${FAKE_RELEASE_IS_DRAFT}"
  fi
elif [[ "$1" == "release" && "$2" == "view" && "$*" == *"--json assets"* ]]; then
  if [[ -n "${FAKE_RELEASE_STATE_FILE:-}" && -f "${FAKE_RELEASE_STATE_FILE}" ]]; then
    cat "${FAKE_RELEASE_STATE_FILE}"
  else
    printf '%s' "${FAKE_RELEASE_ASSETS}"
  fi
elif [[ "$1" == "release" && "$2" == "download" ]]; then
  pattern=""
  destination=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --pattern)
        pattern="$2"
        shift 2
        ;;
      --dir)
        destination="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  mkdir -p "${destination}"
  cp "${FAKE_REMOTE_ASSETS}/${pattern}" "${destination}/${pattern}"
elif [[ "$1" == "release" && "$2" == "upload" ]]; then
  printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
  shift 3
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--repo" ]]; then
      shift 2
    else
      basename "$1" >> "${FAKE_RELEASE_STATE_FILE}"
      shift
    fi
  done
  sort -u -o "${FAKE_RELEASE_STATE_FILE}" "${FAKE_RELEASE_STATE_FILE}"
elif [[ "$1" == "api" && "$2" == *"/git/ref/tags/"* ]]; then
  printf 'tag %s\n' "${FAKE_TAG_OBJECT_SHA}"
elif [[ "$1" == "api" && "$2" == *"/git/tags/"* ]]; then
  printf '%s\n' "${FAKE_TAG_COMMIT_SHA}"
else
  exit 97
fi
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    asset = tmp_path / "dist-offline" / "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz"
    asset.parent.mkdir()
    asset.write_bytes(b"archive")
    sidecar = Path(f"{asset}.sha256")
    sidecar.write_text("digest  archive\n", encoding="utf-8")
    other_assets = []
    for name in (
        "ai-sdlc-offline-3.2.1-windows-amd64.zip",
        "ai-sdlc-offline-3.2.1-windows-amd64.zip.sha256",
        "ai-sdlc-offline-3.2.1-macos-arm64.tar.gz",
        "ai-sdlc-offline-3.2.1-macos-arm64.tar.gz.sha256",
    ):
        path = asset.parent / name
        path.write_bytes(name.encode("utf-8"))
        other_assets.append(path)
    remote_assets = tmp_path / "remote-assets"
    remote_assets.mkdir()
    log_path = tmp_path / "gh.log"
    asset_state = tmp_path / "release-assets.txt"
    base_env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "RELEASE_TAG": "v3.2.1",
        "ALLOWED_RELEASE_TAG": "v3.2.1",
        "DISPATCH_REF": "refs/heads/main",
        "DISPATCH_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "panguosong/AI-SDLC",
        "AI_SDLC_RELEASE_ASSET_OS": "linux",
        "AI_SDLC_RELEASE_ASSET_MACHINE": "amd64",
        "FAKE_GH_LOG": str(log_path),
        "FAKE_REMOTE_ASSETS": str(remote_assets),
        "FAKE_RELEASE_STATE_FILE": str(asset_state),
        "FAKE_TAG_OBJECT_SHA": "b" * 40,
        "FAKE_TAG_COMMIT_SHA": "a" * 40,
    }

    asset_state.write_text("", encoding="utf-8", newline="\n")
    moved_tag = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={
            **base_env,
            "FAKE_RELEASE_IS_DRAFT": "true",
            "FAKE_RELEASE_ASSETS": "",
            "FAKE_TAG_COMMIT_SHA": "c" * 40,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    log_path.unlink(missing_ok=True)
    asset_state.write_text("", encoding="utf-8", newline="\n")
    published = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={**base_env, "FAKE_RELEASE_IS_DRAFT": "false", "FAKE_RELEASE_ASSETS": ""},
        text=True,
        capture_output=True,
        check=False,
    )
    (remote_assets / asset.name).write_bytes(b"different archive")
    asset_state.write_text(f"{asset.name}\n", encoding="utf-8", newline="\n")
    mismatched = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={
            **base_env,
            "FAKE_RELEASE_IS_DRAFT": "true",
            "FAKE_RELEASE_ASSETS": asset.name,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    (remote_assets / asset.name).write_bytes(asset.read_bytes())
    asset_state.write_text(f"{asset.name}\n", encoding="utf-8", newline="\n")
    partial = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={
            **base_env,
            "FAKE_RELEASE_IS_DRAFT": "true",
            "FAKE_RELEASE_ASSETS": asset.name,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    partial_upload = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    log_path.unlink(missing_ok=True)
    draft_sequence = tmp_path / "draft-sequence.txt"
    draft_sequence.write_text("true\nfalse\n", encoding="utf-8", newline="\n")
    asset_state.write_text(f"{asset.name}\n", encoding="utf-8", newline="\n")
    published_during_retry = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={
            **base_env,
            "FAKE_RELEASE_IS_DRAFT": "true",
            "FAKE_RELEASE_ASSETS": asset.name,
            "FAKE_RELEASE_DRAFT_SEQUENCE_FILE": str(draft_sequence),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    transitioned_upload = log_path.exists()
    log_path.unlink(missing_ok=True)
    (remote_assets / sidecar.name).write_bytes(sidecar.read_bytes())
    for other_asset in other_assets:
        (remote_assets / other_asset.name).write_bytes(other_asset.read_bytes())
    complete_asset_names = "\n".join(
        [asset.name, sidecar.name, *(item.name for item in other_assets)]
    )
    asset_state.write_text(f"{complete_asset_names}\n", encoding="utf-8", newline="\n")
    complete = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={
            **base_env,
            "FAKE_RELEASE_IS_DRAFT": "true",
            "FAKE_RELEASE_ASSETS": complete_asset_names,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    complete_uploaded = log_path.exists()
    asset_state.write_text(
        f"{complete_asset_names}\nunexpected.txt\n",
        encoding="utf-8",
        newline="\n",
    )
    unexpected = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={
            **base_env,
            "FAKE_RELEASE_IS_DRAFT": "true",
            "FAKE_RELEASE_ASSETS": complete_asset_names,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    asset_state.write_text("", encoding="utf-8", newline="\n")
    allowed = subprocess.run(
        [bash, "-c", upload_script],
        cwd=tmp_path,
        env={**base_env, "FAKE_RELEASE_IS_DRAFT": "true", "FAKE_RELEASE_ASSETS": ""},
        text=True,
        capture_output=True,
        check=False,
    )

    assert moved_tag.returncode != 0
    assert published.returncode != 0
    assert mismatched.returncode != 0
    assert partial.returncode == 0, partial.stderr
    partial_tokens = partial_upload.split()
    assert asset.relative_to(tmp_path).as_posix() not in partial_tokens
    assert sidecar.relative_to(tmp_path).as_posix() in partial_tokens
    assert published_during_retry.returncode != 0
    assert not transitioned_upload
    assert complete.returncode == 0, complete.stderr
    assert not complete_uploaded
    assert unexpected.returncode != 0
    assert allowed.returncode == 0, allowed.stderr
    upload_call = log_path.read_text(encoding="utf-8")
    assert asset.name in upload_call
    assert "--clobber" not in upload_call


def test_windows_user_guide_e2e_replays_existing_project_install_path() -> None:
    workflow_path = _WORKFLOWS_DIR / "windows-user-guide-e2e.yml"

    assert workflow_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert "windows-latest" in workflow
    assert "default: v3.2.1" in workflow
    assert "Build Windows offline bundle for pull request replay" in workflow
    assert "build_offline_bundle.sh" in workflow
    assert 'AI_SDLC_OFFLINE_ASSET_SUFFIX="-windows-amd64"' in workflow
    assert "pull_request_local_bundle" in workflow
    assert "USER_GUIDE.zh-CN.md Chapter 2: existing project" in workflow
    assert "my-existing-project" in workflow
    assert "v3.2.1" in workflow
    assert "ai-sdlc-offline-$releaseVersion-windows-amd64" in workflow
    assert "releases/download/$env:RELEASE_TAG" in workflow
    assert "Invoke-WebRequest" in workflow
    assert ".sha256" in workflow
    assert "Get-FileHash -Algorithm SHA256" in workflow
    assert "Expand-Archive" in workflow
    assert "-ExecutionPolicy Bypass -File .\\install_offline.ps1 -AddToPath" in workflow
    assert ".\\.venv\\Scripts\\python.exe -m ai_sdlc --help" in workflow
    assert "Direct shim" in workflow
    assert "Codex \\+ PowerShell project init" in workflow
    assert "released-package-guide-gap.txt" in workflow
    assert workflow.count("--init-only") >= 2
    assert "--allow-existing-project" in workflow
    assert (
        "& $directShim init . --agent-target vscode --shell powershell" not in workflow
    )
    assert "当前结果 / Result" in workflow
    assert "下一步 / Next" in workflow
    assert "adapter ingress|materialized|unverified|host ingress" in workflow
    assert "& $directShim adopt ." in workflow
    assert "接入已有项目" in workflow
    assert "business-file-hashes-before.txt" in workflow
    assert "business-file-hashes-after.txt" in workflow
    assert "Compare-Object" in workflow
    assert "init/adopt modified existing business files" in workflow
    assert "my-new-project" in workflow
    assert (
        "& $directShim init . --agent-target cursor --shell powershell" not in workflow
    )
    assert "interactive-init.txt" in workflow
    assert "AGENTS.md" in workflow
    assert "windows-user-guide-existing-project-evidence" in workflow
    assert "actions/upload-artifact@v7" in workflow


def test_posix_user_guide_e2e_replays_published_guide_commands() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    driver_path = _REPO_ROOT / "scripts" / "posix_clean_user_e2e.py"

    assert workflow_path.is_file()
    assert driver_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")
    driver = driver_path.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert 'default: "v3.2.1"' in workflow
    assert "v3.2.1" in workflow
    for path_filter in (
        '      - "src/**"',
        '      - "pyproject.toml"',
        '      - "packaging_backend.py"',
        '      - "README.md"',
        '      - "templates/**"',
        '      - "packaging/offline/**"',
    ):
        assert path_filter in workflow
    assert "macos-latest" in workflow
    assert "ubuntu-latest" in workflow
    assert "USER_GUIDE.zh-CN.md" in workflow
    assert '- "scripts/posix_clean_user_e2e.py"' in workflow
    assert "Build POSIX offline bundle for pull request replay" in workflow
    assert "github.event_name == 'pull_request'" in workflow
    assert "bash packaging/offline/build_offline_bundle.sh" in workflow
    assert 'package_source="pull_request_local_bundle"' in workflow
    assert 'package_source="published_release"' in workflow
    assert "dist-offline/${PACKAGE_NAME}" in workflow
    assert "curl --fail --location --retry 3" in workflow
    assert "releases/download/$RELEASE_TAG/$PACKAGE_NAME" in workflow
    assert "shasum -a 256 -c" in workflow
    assert "sha256sum -c" in workflow
    assert "./install_offline.sh --add-to-path" in workflow
    assert '"$DIRECT_CLI" --version' in workflow
    assert '"$DIRECT_CLI" adopt .' in workflow
    assert workflow.count("python3 scripts/posix_clean_user_e2e.py") >= 2
    assert '"$DIRECT_CLI" init . --agent-target vscode' not in workflow
    assert "POSIX_INTERACTIVE_SELECTION_COMPLETED" in workflow
    assert "pty.fork()" in driver
    assert 'os.execv(str(cli_path), [str(cli_path), "init", "."])' in driver
    assert 'os.write(master_fd, b"\\x1b[A")' in driver
    assert "agent_renders > observed_agent_renders" in driver
    assert "AGENT_PROMPT" in driver
    assert "SHELL_PROMPT" in driver
    assert '"--agent-target"' not in driver
    assert '"--shell"' not in driver
    assert "import ai_sdlc" not in driver
    assert "business-before.sha256" in workflow
    assert "business-after.sha256" in workflow
    assert "diff -u" in workflow
    assert "posix-user-guide-e2e-evidence" in workflow
    assert "actions/upload-artifact@v7" in workflow


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX PTY 行为由 POSIX 平台验证")
@pytest.mark.parametrize("read_delay", [0.0, 0.8])
def test_posix_guide_driver_waits_for_real_terminal_input(
    tmp_path: Path, read_delay: float,
) -> None:
    cli = tmp_path / "selector-cli"
    child_pid = tmp_path / "selector.pid"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(_REPO_ROOT / 'src')!r})\n"
        "from pathlib import Path\n"
        "from ai_sdlc.integrations import agent_target as target\n"
        f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "original = target._read_selector_key_posix\n"
        "first = True\n"
        "def delayed_read():\n"
        "    global first\n"
        "    if first:\n"
        "        first = False\n"
        f"        time.sleep({read_delay!r})\n"
        "    return original()\n"
        "target._read_selector_key_posix = delayed_read\n"
        "agent = target.interactive_select_agent_target(target.IDEKind.GENERIC)\n"
        "shell = target.interactive_select_preferred_shell(target.PreferredShell.ZSH)\n"
        "assert agent == target.IDEKind.CURSOR and shell == target.PreferredShell.ZSH\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    # 菜单与读键都调用产品代码；只延迟真实读键入口，重现已公开指南的竞态。
    launcher = (
        "import runpy, signal, sys\n"
        "def stop(signum, frame):\n"
        "    raise TimeoutError('real PTY regression deadline')\n"
        "signal.signal(signal.SIGALRM, stop)\n"
        "signal.alarm(6)\n"
        f"sys.argv = [{str(_REPO_ROOT / 'scripts/posix_clean_user_e2e.py')!r}, "
        f"'--cli', {str(cli)!r}, '--project', {str(tmp_path)!r}]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", launcher], capture_output=True, text=True,
        timeout=15,
    )
    # 驱动须回收其真实 PTY 子进程；回放失败也不能遗留等待输入的进程。
    assert child_pid.is_file(), result.stdout + result.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text()), 0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "POSIX_INTERACTIVE_SELECTION_COMPLETED" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX PTY 行为由 POSIX 平台验证")
def test_posix_guide_driver_rejects_terminal_that_never_becomes_ready() -> None:
    import pty
    import termios
    import time

    driver = runpy.run_path(_REPO_ROOT / "scripts/posix_clean_user_e2e.py")
    master, slave = pty.openpty()
    try:
        assert termios.tcgetattr(master)[3] & (termios.ICANON | termios.ECHO)
        with pytest.raises(TimeoutError, match="raw input mode"):
            driver["wait_input_ready"](master, time.monotonic() + 0.05)
        assert termios.tcgetattr(master)[3] & (termios.ICANON | termios.ECHO)
    finally:
        os.close(master)
        os.close(slave)


def test_user_guide_e2e_covers_both_project_states_through_online_installers() -> None:
    windows = (_WORKFLOWS_DIR / "windows-user-guide-e2e.yml").read_text(
        encoding="utf-8"
    )
    posix = (_WORKFLOWS_DIR / "posix-user-guide-e2e.yml").read_text(encoding="utf-8")
    windows_driver = (_REPO_ROOT / "scripts" / "windows_clean_user_e2e.py").read_text(
        encoding="utf-8"
    )

    windows_online = windows.split("clean-online-interactive-user-journey:", 1)[1]
    assert "install_online.ps1" in windows_online
    assert "windows-online-empty-project" in windows_online
    assert "windows-online-existing-project" in windows_online
    assert "--init-only" in windows_online
    assert "init . --agent-target codex --shell powershell" not in windows_online
    assert '["adopt", "."]' in windows_driver

    assert "online-install:" in posix
    posix_online = posix.split("online-install:", 1)[1]
    assert "macos-latest" in posix_online
    assert "ubuntu-latest" in posix_online
    assert "install_online.sh" in posix_online
    assert "remote-pull-request-head" in posix_online
    assert "remote-release-tag" in posix_online
    assert "posix-online-empty-project" in posix_online
    assert "posix-online-existing-project" in posix_online
    assert posix_online.count("python3 scripts/posix_clean_user_e2e.py") >= 2
    assert '"${online_cli}" init . --agent-target codex' not in posix_online
    assert '"${online_cli}" adopt .' in posix_online


def test_windows_online_guide_replays_missing_python_bootstrap_before_install() -> None:
    workflow_path = _WORKFLOWS_DIR / "windows-user-guide-e2e.yml"
    driver_path = _REPO_ROOT / "scripts" / "windows_python_bootstrap_e2e.ps1"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert driver_path.is_file()
    steps = workflow["jobs"]["clean-online-interactive-user-journey"]["steps"]
    replay = next(
        step
        for step in steps
        if step.get("name") == "Replay installer without a preinstalled Python"
    )
    assert replay["shell"] == "pwsh"
    assert replay["run"] == (
        "pwsh -NoProfile -File scripts/windows_python_bootstrap_e2e.ps1 "
        "-PackageManager winget"
    )
    choco_replay = next(
        step
        for step in steps
        if step.get("name") == "Replay installer after a Chocolatey Python install"
    )
    assert choco_replay["shell"] == "pwsh"
    assert choco_replay["run"] == (
        "pwsh -NoProfile -File scripts/windows_python_bootstrap_e2e.ps1 "
        "-PackageManager choco"
    )
    failure_replay = next(
        step
        for step in steps
        if step.get("name") == "Replay expected Windows Python bootstrap failure"
    )
    assert failure_replay["shell"] == "pwsh"
    assert failure_replay["run"] == (
        "pwsh -NoProfile -File scripts/windows_python_bootstrap_e2e.ps1 "
        "-PackageManager winget -ExpectInstallFailure"
    )
    receipt_check = next(
        step
        for step in steps
        if step.get("name") == "Verify isolated Python bootstrap receipts"
    )
    for scenario in (
        "winget-success",
        "choco-success",
        "winget-expected-failure",
    ):
        assert f"python-bootstrap-{scenario}.json" in receipt_check["run"]
        assert f"python-bootstrap-{scenario}-output.txt" in receipt_check["run"]
        assert f"python-bootstrap-{scenario}-events.txt" in receipt_check["run"]
    prerequisite_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Assert fresh runner prerequisites"
    )
    assert steps.index(replay) < prerequisite_index

    workflow_text = workflow_path.read_text(encoding="utf-8")
    assert '- "scripts/windows_python_bootstrap_e2e.ps1"' in workflow_text
    driver = driver_path.read_text(encoding="utf-8")
    assert 'Join-Path $shimRoot "py.cmd"' in driver
    assert 'Join-Path $shimRoot "python.cmd"' not in driver
    assert "FAKE_INSTALLED_PYTHON_ROOT" in driver
    assert "Programs\\Python\\Python311" in driver
    assert "xcopy" in driver
    assert "mklink /J" not in driver
    assert '[ValidateSet("winget", "choco")]' in driver
    assert "[switch]$ExpectInstallFailure" in driver
    assert (
        '$scenario = if ($ExpectInstallFailure) { "winget-expected-failure" } '
        'else { "$PackageManager-success" }'
    ) in driver
    assert 'Join-Path $shimRoot "choco.cmd"' in driver
    isolated_path = '$isolatedPath = "$shimRoot;$env:SystemRoot\\System32"'
    assert isolated_path in driver
    assert (
        '[Environment]::SetEnvironmentVariable("Path", $isolatedPath, "Machine")'
        in driver
    )
    assert "$env:Path = $isolatedPath" in driver
    assert (
        '$isolatedPath = "$shimRoot;$env:SystemRoot\\System32;$env:SystemRoot"'
        not in driver
    )
    assert (
        '$env:Path = "$shimRoot;$env:SystemRoot\\System32;$env:SystemRoot"'
        not in driver
    )
    assert '$fakeLocalAppData = Join-Path $root "local-app-data"' in driver
    assert '$fakeProgramFiles = Join-Path $root "program-files"' in driver
    assert "$env:LOCALAPPDATA = $fakeLocalAppData" in driver
    assert "$env:ProgramFiles = $fakeProgramFiles" in driver
    preflight_markers = (
        "Get-Command py -All",
        "Get-Command python -All",
        '$whereExe = Join-Path $env:SystemRoot "System32\\where.exe"',
        "& $whereExe py",
        "$standardPythonRootsAbsent",
    )
    installer_call = driver.index(
        '(Join-Path $PSScriptRoot "..\\packaging\\install_online.ps1")'
    )
    for marker in preflight_markers:
        assert marker in driver
        assert driver.index(marker) < installer_call
    assert "python-bootstrap-$scenario-output.txt" in driver
    assert "python-bootstrap-$scenario-events.txt" in driver
    assert "python-bootstrap-$scenario.json" in driver
    assert "importlib.metadata.version('dummy-ai-sdlc')" in driver
    assert 'Join-Path $installRoot "Scripts\\ai-sdlc.exe"' in driver
    assert "distribution_version" in driver
    assert "cli_version" in driver
    assert '"3.1.0"' in driver
    for restoration_marker in (
        "machine_path_restored",
        "user_path_restored",
        "process_path_restored",
        "local_app_data_restored",
        "program_files_restored",
    ):
        assert restoration_marker in driver
    assert driver.count("restorationErrors.Add") >= 5
    assert "runtime_absent" in driver
    assert "venv_absent" in driver
    assert "$windowsPowerShell = (Get-Command powershell" in driver
    assert "& $windowsPowerShell -NoProfile" in driver

    installer = (_REPO_ROOT / "packaging" / "install_online.ps1").read_text(
        encoding="utf-8"
    )
    assert "Update-ProcessPathFromPersistentEnvironment" in installer
    assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' in installer
    assert (
        '[Environment]::SetEnvironmentVariable("Path", $updatedMachinePath, "Machine")'
        not in installer
    )
    assert 'Join-Path $env:LOCALAPPDATA "Programs\\Python"' in installer
    assert "foreach ($minorVersion in @(14, 13, 12, 11))" in installer
    assert 'Join-Path $root "Python3$minorVersion"' in installer
    assert 'Join-Path $pythonHome "python.exe"' in installer


def test_windows_python_bootstrap_preserves_execution_and_restoration_failures() -> (
    None
):
    driver = (_REPO_ROOT / "scripts" / "windows_python_bootstrap_e2e.ps1").read_text(
        encoding="utf-8"
    )

    assert "$executionFailure = if ($capturedFailure)" in driver
    assert "$restorationFailures = @($restorationErrors)" in driver
    assert "execution_failure = $executionFailure" in driver
    assert "restoration_failures = $restorationFailures" in driver
    assert '$finalFailureParts += "Execution failure: $executionFailure"' in driver
    assert (
        '$finalFailureParts += "Environment restoration failures: '
        "$($restorationFailures -join '; ')\""
    ) in driver
    receipt_write = driver.index("Write-Utf8NoBom -Path $evidencePath")
    final_failure_construction = driver.index("$finalFailureParts = @()")
    final_throw = driver.index("throw $finalFailure")
    assert receipt_write < final_failure_construction < final_throw
    assert '$capturedFailure = "Environment restoration failed:' not in driver


def test_linux_offline_acquisition_bootstrap_runs_on_fresh_connected_hosts() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    job = workflow["jobs"]["linux-offline-acquisition-bootstrap"]
    assert job["container"]["image"] == "debian:12-slim"
    assert job["strategy"]["matrix"]["project_state"] == ["new", "existing"]
    replay = next(
        step
        for step in job["steps"]
        if step.get("name") == "Replay exact offline acquisition bootstrap"
    )["run"]
    assert "|offline|linux-amd64" in replay
    assert 'test -z "$(command -v curl || true)"' in replay
    assert "AI-SDLC-USER-GUIDE-STEP: acquire" in replay
    assert "sed '/^PACKAGE_NAME=/,$d'" in replay
    assert 'bash "${bootstrap_script}"' in replay
    assert "curl --fail --location" in replay
    assert "actions/upload-artifact@v7" in str(job["steps"])


def test_linux_offline_compatibility_gates_execute_on_glibc_and_musl_hosts() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    job = workflow["jobs"]["linux-offline-compatibility-gates"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"]["include"] == [
        {
            "host_kind": "supported-glibc",
            "container_image": "debian:12-slim",
            "expected_outcome": "pass",
        },
        {
            "host_kind": "rejected-musl",
            "container_image": "alpine:3.21",
            "expected_outcome": "fail",
        },
        {
            "host_kind": "unknown-libc",
            "container_image": "alpine:3.21",
            "expected_outcome": "unknown",
        },
    ]
    replay = next(
        step
        for step in job["steps"]
        if step.get("name") == "Execute exact offline compatibility gates"
    )["run"]
    for project_state in ("new", "existing"):
        assert f'"{project_state}"' in replay
    for step_name in ("prerequisites", "recover"):
        assert f'"{step_name}"' in replay
    assert '"AI-SDLC-USER-GUIDE-STEP: " step_name' in replay
    assert 'route_marker="<!-- AI-SDLC-USER-GUIDE-ROUTE:' in replay
    assert 'bash "${guard_script}"' in replay
    assert "docker run --rm" in replay
    assert '"${CONTAINER_IMAGE}"' in replay
    assert 'test "${guard_exit}" -eq 0' in replay
    assert 'test "${guard_exit}" -ne 0' in replay
    assert 'test -z "$(command -v getconf || true)"' in replay
    assert "rm -f /usr/bin/getconf" in replay
    assert 'LC_ALL=C "${loader}" --version > /evidence/loader.txt' in replay
    assert "grep -Eq '^ld\\.so" in replay
    assert "grep -Eq '^musl libc" in replay
    assert "test ! -s /evidence/loader.txt" in replay
    assert "diagnostic: glibc compatibility could not be determined" in replay
    assert "cat > \"${guard_root}/ldd\" <<'UNKNOWN_LDD'" in replay
    assert 'test "$(command -v ldd)" = "/guards/ldd"' in replay
    assert "无法确定此主机使用的 libc" in replay
    assert "grep -Ei 'glibc|GNU C Library|GNU libc' /evidence/host.txt" not in replay
    assert "不得使用 ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" in replay
    assert "actions/upload-artifact@v7" in str(job["steps"])


def test_linux_online_existing_python_path_replays_on_opensuse() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    job = workflow["jobs"]["linux-online-opensuse-existing-python"]
    assert job["runs-on"] == "ubuntu-latest"
    replay = next(
        step
        for step in job["steps"]
        if step.get("name") == "Replay exact openSUSE online prerequisites"
    )["run"]
    assert "opensuse/leap:15.6" in replay
    assert "/etc/ssl/ca-bundle.pem" in replay
    assert "python3.11" in replay
    assert '"new" "existing"' in replay
    assert '"prerequisites" "acquire" "verify" "recover"' in replay
    assert 'source "/replay/${project_state}-prerequisites.sh"' in replay
    assert 'source "/replay/${project_state}-acquire.sh"' in replay
    assert 'source "/replay/${project_state}-verify.sh"' in replay
    assert (
        "https://raw.githubusercontent.com/panguosong/AI-SDLC/v3.2.1/packaging/install_online.sh"
        in replay
    )
    assert 'test -s "${INSTALLER_PATH}"' in replay
    assert (
        'cp "${INSTALLER_PATH}" "/evidence/${project_state}-install_online.sh"'
        in replay
    )
    assert "AI_SDLC_PACKAGE_SPEC=/workspace" in replay
    assert "bash /evidence/new-install_online.sh /tmp/ai-sdlc-venv" in replay
    assert "/tmp/ai-sdlc-venv/bin/python -m ai_sdlc --version" in replay
    assert "grep -F '3.2.1' /evidence/version.txt" in replay
    assert '--volume "${GITHUB_WORKSPACE}:/workspace:ro"' in replay
    assert "actions/upload-artifact@v7" in str(job["steps"])


def test_macos_online_guide_replays_stock_host_prerequisites_before_install() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    driver_path = _REPO_ROOT / "scripts" / "macos_stock_host_prerequisite_e2e.py"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert driver_path.is_file()
    online_steps = workflow["jobs"]["online-install"]["steps"]
    replay = next(
        step
        for step in online_steps
        if step.get("name") == "Replay stock macOS route prerequisites"
    )
    assert replay["if"] == "matrix.asset_os == 'macos'"
    assert replay["run"] == "python3 scripts/macos_stock_host_prerequisite_e2e.py"
    install_index = next(
        index
        for index, step in enumerate(online_steps)
        if step.get("name")
        == "Install from the pinned online source and replay both project states"
    )
    assert online_steps.index(replay) < install_index

    workflow_text = workflow_path.read_text(encoding="utf-8")
    assert '- "scripts/macos_stock_host_prerequisite_e2e.py"' in workflow_text


def test_macos_stock_host_prerequisite_driver_runs_from_checkout(
    tmp_path: Path,
) -> None:
    driver_path = _REPO_ROOT / "scripts" / "macos_stock_host_prerequisite_e2e.py"
    if os.name == "nt":
        assert driver_path.is_file()
        return

    env = os.environ.copy()
    env["RUNNER_TEMP"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(driver_path)],
        cwd=_REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "MACOS_STOCK_HOST_PREREQUISITES_OK" in completed.stdout
    evidence = json.loads(
        (
            tmp_path
            / "posix-online-user-guide-e2e-evidence"
            / "macos-stock-host-prerequisites.json"
        ).read_text(encoding="utf-8")
    )
    assert evidence == {
        "routes": [
            {
                "route_id": "new|online|macos-arm64",
                "official_installer_invocations": 1,
                "homebrew_activated": True,
            },
            {
                "route_id": "existing|online|macos-arm64",
                "official_installer_invocations": 1,
                "homebrew_activated": True,
            },
        ]
    }


def test_posix_online_guide_replays_missing_python_bootstrap_before_install() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    driver_path = _REPO_ROOT / "scripts" / "posix_python_bootstrap_e2e.py"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert driver_path.is_file()
    online_steps = workflow["jobs"]["online-install"]["steps"]
    replay = next(
        step
        for step in online_steps
        if step.get("name") == "Replay installer without a preinstalled Python"
    )
    assert replay["if"] == "matrix.asset_os == 'macos'"
    assert replay["run"] == "python3 scripts/posix_python_bootstrap_e2e.py"
    install_index = next(
        index
        for index, step in enumerate(online_steps)
        if step.get("name")
        == "Install from the pinned online source and replay both project states"
    )
    assert online_steps.index(replay) < install_index

    workflow_text = workflow_path.read_text(encoding="utf-8")
    assert '- "scripts/posix_python_bootstrap_e2e.py"' in workflow_text


def test_linux_online_guide_bootstraps_python_with_real_apt() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    job = workflow["jobs"]["linux-python-bootstrap"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["container"]["image"] == "debian:12-slim"
    assert job["strategy"]["matrix"]["project_state"] == ["new", "existing"]
    steps = job["steps"]
    checkout = next(
        step for step in steps if step.get("name") == "Check out exact guide inputs"
    )
    prepare = next(
        step
        for step in steps
        if step.get("name") == "Replay exact Linux route prerequisites"
    )
    bootstrap = next(
        step
        for step in steps
        if step.get("name") == "Install Python through the exact online installer"
    )
    complete = next(
        step
        for step in steps
        if step.get("name") == "Complete the exact Linux route after bootstrap"
    )
    assert steps.index(checkout) < steps.index(prepare)
    assert "AI-SDLC-USER-GUIDE-ROUTE" in prepare["run"]
    assert "USER_GUIDE.zh-CN.md" in prepare["run"]
    assert 'bash "${prerequisite_script}"' in prepare["run"]
    assert 'test -z "$(command -v python3.11 || true)"' in prepare["run"]
    assert 'test -z "$(command -v python3 || true)"' in prepare["run"]
    assert 'test -z "$(command -v python || true)"' in prepare["run"]
    assert "command -v curl" in prepare["run"]
    assert "ca-certificates.crt" in prepare["run"]
    assert 'bash "${installer_path}" "${install_root}"' in bootstrap["run"]
    assert "dpkg-query -W" in bootstrap["run"]
    assert "python3.11 python3.11-venv python3-pip" in bootstrap["run"]
    assert "fake" not in bootstrap["run"].lower()
    installer_command = 'bash "${installer_path}" "${install_root}" --add-to-path'
    assert installer_command in bootstrap["run"]
    for identity_check in (
        ". /etc/os-release",
        'test "${ID}" = "debian"',
        'test "${VERSION_ID}" = "12"',
        "uname -m",
        "getconf GNU_LIBC_VERSION",
    ):
        assert identity_check in bootstrap["run"]
        assert bootstrap["run"].index(identity_check) < bootstrap["run"].index(
            installer_command
        )
    assert 'stable_cli="${HOME}/.local/bin/ai-sdlc"' in bootstrap["run"]
    assert 'test -L "${stable_cli}"' in bootstrap["run"]
    assert "profile-export-count.txt" in bootstrap["run"]
    assert 'test "${profile_export_count}" -eq 1' in bootstrap["run"]
    assert 'bash --noprofile --rcfile "${HOME}/.bashrc" -ic' in bootstrap["run"]
    assert "ai-sdlc --version" in bootstrap["run"]
    assert 'test "${fresh_shell_version}" = "${RELEASE_TAG#v}"' in bootstrap["run"]
    assert steps.index(bootstrap) < steps.index(complete)
    assert 'if [[ "${PROJECT_STATE}" == "existing" ]]' in complete["run"]
    assert "python3 scripts/posix_clean_user_e2e.py" in complete["run"]
    assert '--cli "${install_root}/bin/ai-sdlc"' in complete["run"]
    assert '--project "${project_root}"' in complete["run"]
    assert "POSIX_INTERACTIVE_SELECTION_COMPLETED" in complete["run"]
    assert 'test -f "${project_root}/.cursor/rules/ai-sdlc.mdc"' in complete["run"]
    assert 'grep -F "AI 代理入口: Cursor"' in complete["run"]
    assert 'grep -F "Project shell: bash"' in complete["run"]
    assert 'test -f "${project_root}/AGENTS.md"' not in complete["run"]
    assert "--agent-target" not in complete["run"]
    assert "--shell bash" not in complete["run"]
    assert '"${module_python}" -m ai_sdlc adopt .' in complete["run"]
    assert "AI-SDLC-USER-GUIDE-STEP: success" in complete["run"]
    assert "AI-SDLC-USER-GUIDE-STEP: recover" in complete["run"]
    assert 'bash "${success_script}"' in complete["run"]
    assert 'bash "${recover_script}"' in complete["run"]
    assert "git status --short --untracked-files=all" in complete["run"]
    assert "business-before.sha256" in complete["run"]
    assert "business-after.sha256" in complete["run"]
    assert "route-completion.json" in complete["run"]

    driver = (_REPO_ROOT / "scripts" / "posix_python_bootstrap_e2e.py").read_text(
        encoding="utf-8"
    )
    assert 'shim_bin / "apt-get"' not in driver


def test_linux_unsupported_python_bootstrap_fails_closed_without_mutation() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-user-guide-e2e.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    job = workflow["jobs"]["linux-unsupported-python-bootstrap"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["container"]["image"] == "ubuntu:22.04"
    assert job["strategy"]["matrix"]["project_state"] == ["new", "existing"]
    steps = job["steps"]
    checkout = next(
        step
        for step in steps
        if step.get("name") == "Check out the exact unsupported-host installer"
    )
    assert checkout["with"]["persist-credentials"] is False
    assert (
        "github.event.pull_request.head.repo.full_name"
        in checkout["with"]["repository"]
    )
    assert "github.event.pull_request.head.sha" in checkout["with"]["ref"]

    verify = next(
        step
        for step in steps
        if step.get("name") == "Require unsupported Ubuntu to fail closed"
    )
    run = verify["run"]
    assert (
        'export HOME="${RUNNER_TEMP}/ubuntu-unsupported-home-${PROJECT_STATE}"' in run
    )
    assert 'export SHELL="/bin/bash"' in run
    assert (
        'project_root="${RUNNER_TEMP}/ubuntu-unsupported-${PROJECT_STATE}-project"'
        in run
    )
    assert "project-before.sha256" in run
    assert "project-after.sha256" in run
    for command in ("python3.11", "python3", "python"):
        assert f'"${{shadow_bin}}/{command}"' in run
    assert 'apt_sentinel_log="${evidence_root}/apt-get-calls.log"' in run
    assert '"${shadow_bin}/apt-get"' in run
    assert 'AI_SDLC_PACKAGE_SPEC="${GITHUB_WORKSPACE}"' in run
    installer_command = 'bash "${installer_path}" "${install_root}" --add-to-path'
    assert installer_command in run
    installer_from_hashed_project = (
        "(\n"
        '  cd "${project_root}"\n'
        '  AI_SDLC_PACKAGE_SPEC="${GITHUB_WORKSPACE}" \\\n'
        '    bash "${installer_path}" "${install_root}" --add-to-path\n'
        ') > "${evidence_root}/install-online.txt" 2>&1'
    )
    assert installer_from_hashed_project in run
    assert "installer_exit=$?" in run
    assert 'test "${installer_exit}" -ne 0' in run
    assert "distro=ubuntu version=22\\.04" in run
    assert "Debian GNU/Linux 12 (bookworm)" in run
    assert "ai-sdlc-offline-3.2.1-linux-amd64.tar.gz" in run
    assert "route 6/12" in run
    assert 'test "${python_package_install_calls}" -eq 0' in run
    assert 'test ! -e "${install_root}"' in run
    assert 'test ! -L "${stable_cli}"' in run
    assert "profile-before.sha256" in run
    assert "profile-after.sha256" in run
    assert "diff -u" in run
    assert 'installer_failed_closed="true"' in run
    assert 'venv_created="false"' in run
    assert 'project_mutated="false"' in run
    receipt_write = run.index('> "${evidence_root}/fail-closed.json"')
    for assertion in (
        'test "${installer_exit}" -ne 0',
        'test "${python_package_install_calls}" -eq 0',
        'test ! -e "${install_root}"',
        'test ! -L "${stable_cli}"',
        "profile-after.sha256",
        "project-after.sha256",
    ):
        assert run.index(assertion) < receipt_write
    for evidence_field in (
        '"platform":"ubuntu-22.04"',
        '"project_state":"%s"',
        '"installer_failed_closed":%s',
        '"python_package_install_calls":%s',
        '"venv_created":%s',
        '"project_mutated":%s',
        '"offline_recovery_asset":"ai-sdlc-offline-3.2.1-linux-amd64.tar.gz"',
    ):
        assert evidence_field in run

    upload = next(
        step
        for step in steps
        if step.get("name") == "Upload unsupported Linux bootstrap evidence"
    )
    assert upload["if"] == "always()"
    assert "ubuntu-unsupported-python-bootstrap-evidence" in upload["with"]["name"]
    assert "ubuntu-unsupported-python-bootstrap-evidence" in upload["with"]["path"]


def test_posix_python_bootstrap_driver_runs_from_checkout(tmp_path: Path) -> None:
    driver_path = _REPO_ROOT / "scripts" / "posix_python_bootstrap_e2e.py"
    if sys.platform != "darwin":
        assert driver_path.is_file()
        return

    env = os.environ.copy()
    env["RUNNER_TEMP"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(driver_path)],
        cwd=_REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "POSIX_PYTHON_BOOTSTRAP_OK" in completed.stdout
    evidence = json.loads(
        (
            tmp_path / "posix-online-user-guide-e2e-evidence" / "python-bootstrap.json"
        ).read_text(encoding="utf-8")
    )
    assert evidence["python_missing_before_install"] is True
    assert evidence["python_available_after_install"] is True
    assert evidence["package_manager_install_calls"] == 1
    assert evidence["installer_completed"] is True


def test_windows_clean_user_e2e_uses_remote_install_and_real_interactive_init() -> None:
    workflow_path = _WORKFLOWS_DIR / "windows-user-guide-e2e.yml"
    driver_path = _REPO_ROOT / "scripts" / "windows_clean_user_e2e.py"

    assert workflow_path.is_file()
    assert driver_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")
    driver = driver_path.read_text(encoding="utf-8")

    install_inputs = (
        '- "src/**"',
        '- "pyproject.toml"',
        '- "packaging_backend.py"',
        '- "README.md"',
        '- "templates/**"',
        '- "scripts/frontend_browser_gate_probe_runner.mjs"',
        '- "packaging/install_online.ps1"',
    )
    assert all(path_filter in workflow for path_filter in install_inputs)
    assert "clean-online-interactive-user-journey:" in workflow
    assert "remote-release-tag" in workflow
    assert (
        "raw.githubusercontent.com/$sourceRepository/"
        "$remoteSha/packaging/install_online.ps1" in workflow
    )
    assert "git+https://github.com/$sourceRepository.git@$remoteSha" in workflow
    assert "pywinpty" in workflow
    assert "windows-clean-online-user-e2e-evidence" in workflow
    assert "PtyProcess.spawn" in driver
    assert '[cli_path, "init", "."]' in driver
    assert "请选择当前实际用于聊天开发的 AI 代理入口" in driver
    assert "请选择当前项目默认使用的命令 Shell" in driver
    assert 'process.write("2\\r\\n")' in driver
    assert 'process.write("1\\r\\n")' in driver
    assert '"--agent-target"' not in driver
    assert '"--shell"' not in driver
    assert "import ai_sdlc" not in driver

    init_contract = driver.split("def _verify_interactive_init", 1)[1].split(
        "def _run_requirement_and_workitem_flow", 1
    )[0]
    for required in (
        "基于项目已有技术栈、约束和交付目标给出一个推荐方案",
        "至少一个可选 / 自定义方案",
        "明确区分“规范正文”“可选建议”“已经落地”",
        "等待用户明确确认",
        "通用规则不得硬编码框架、组件库、provider 或 style pack",
        "只有项目事实或用户明确选择才能确定具体方案",
    ):
        assert required in init_contract
    assert "forbidden_agent_tokens" in init_contract
    assert "if token in agents_text" in init_contract
    for forbidden in (
        "进入实现前必须先给出技术栈 / 组件库建议",
        "public-primevue",
        "enterprise-vue2",
        "modern-saas",
        "data-console",
    ):
        assert forbidden in init_contract


def test_windows_clean_user_e2e_uses_real_codex_cli_and_archives_adapter_files() -> (
    None
):
    workflow_path = _WORKFLOWS_DIR / "windows-user-guide-e2e.yml"
    driver_path = _REPO_ROOT / "scripts" / "windows_clean_user_e2e.py"

    workflow = workflow_path.read_text(encoding="utf-8")
    driver = driver_path.read_text(encoding="utf-8")

    assert "actions/setup-node@v6" in workflow
    assert '"@openai/codex@0.138.0"' in workflow
    assert 'shutil.which("codex")' in driver
    assert "codex-cli-version.txt" in driver
    assert "codex-adapter-files" in driver
    assert "codex-adapter-manifest.json" in driver
    assert 'project_root / "AGENTS.md"' in driver
    assert 'project_root / ".ai-sdlc" / "project" / "config"' in driver
    assert '"project-config.yaml"' in driver
    assert "hashlib.sha256" in driver
    clean_upload = workflow.split("Upload clean online ordinary-user evidence", 1)[1]
    assert "include-hidden-files: true" in clean_upload


def test_windows_clean_user_e2e_pins_release_tag_before_online_install() -> None:
    workflow_path = _WORKFLOWS_DIR / "windows-user-guide-e2e.yml"

    workflow = workflow_path.read_text(encoding="utf-8").split(
        "clean-online-interactive-user-journey:", 1
    )[1]
    resolve_release_tag = (
        "git ls-remote https://github.com/panguosong/AI-SDLC.git "
        '"refs/tags/$env:RELEASE_TAG" "refs/tags/$env:RELEASE_TAG^{}"'
    )
    pinned_installer = (
        "raw.githubusercontent.com/$sourceRepository/"
        "$remoteSha/packaging/install_online.ps1"
    )
    pinned_package = "git+https://github.com/$sourceRepository.git@$remoteSha"

    assert resolve_release_tag in workflow
    assert '$sourceKind = "remote-release-tag"' in workflow
    assert pinned_installer in workflow
    assert pinned_package in workflow
    assert workflow.index(resolve_release_tag) < workflow.index(pinned_installer)
    assert workflow.index(pinned_installer) < workflow.index("Invoke-WebRequest")
    assert workflow.count(resolve_release_tag) == 1
    assert "$directUrl.vcs_info.requested_revision -ne $remoteSha" in workflow


def test_windows_clean_user_e2e_installs_pull_request_head_on_pr_runs() -> None:
    workflow_path = _WORKFLOWS_DIR / "windows-user-guide-e2e.yml"
    driver_path = _REPO_ROOT / "scripts" / "windows_clean_user_e2e.py"
    support_path = _REPO_ROOT / "scripts" / "windows_clean_user_e2e_support.py"

    workflow = workflow_path.read_text(encoding="utf-8").split(
        "clean-online-interactive-user-journey:", 1
    )[1]
    driver = driver_path.read_text(encoding="utf-8")
    contract = driver + support_path.read_text(encoding="utf-8")

    assert "PR_HEAD_REPOSITORY:" in workflow
    assert "github.event.pull_request.head.repo.full_name" in workflow
    assert "PR_HEAD_SHA:" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert 'if ($env:GITHUB_EVENT_NAME -eq "pull_request")' in workflow
    assert "$sourceRepository = $env:PR_HEAD_REPOSITORY" in workflow
    assert "$remoteSha = $env:PR_HEAD_SHA" in workflow
    assert (
        "raw.githubusercontent.com/$sourceRepository/"
        "$remoteSha/packaging/install_online.ps1" in workflow
    )
    assert "git+https://github.com/$sourceRepository.git@$remoteSha" in workflow
    assert "AI_SDLC_E2E_INSTALL_SOURCE=$sourceKind" in workflow
    assert "AI_SDLC_E2E_SOURCE_REVISION=$remoteSha" in workflow
    assert 'os.environ.get("AI_SDLC_E2E_INSTALL_SOURCE", "remote-main")' in contract
    assert 'os.environ.get("AI_SDLC_E2E_SOURCE_REVISION", "")' in contract


def test_windows_clean_user_e2e_covers_project_fact_recommendation_and_custom_choice() -> (
    None
):
    driver_path = _REPO_ROOT / "scripts" / "windows_clean_user_e2e.py"
    support_path = _REPO_ROOT / "scripts" / "windows_clean_user_e2e_support.py"

    assert driver_path.is_file()
    assert support_path.is_file()

    driver = driver_path.read_text(encoding="utf-8")
    contract = driver + support_path.read_text(encoding="utf-8")

    assert '"loop",' in driver
    assert '"frontend-evidence",' in driver
    assert '"solution-confirm",' in driver
    assert '"--wi",' in driver
    assert '"--json",' in driver
    assert '"--frontend-stack",' in driver
    assert '"vue3",' in driver
    assert '"--provider-id",' in driver
    assert '"public-primevue",' in driver
    assert '"--style-pack-id",' in driver
    assert '"data-console",' in driver
    assert "recommended_option_source" in contract
    assert "existing-project-facts" in contract
    assert "custom_choice_supported" in contract
    assert '"option_id": "custom"' in contract
    assert "data-console" in contract
    assert "--execute" not in driver
    assert '"program",' not in driver


def test_windows_clean_user_e2e_uses_public_requirement_and_workitem_flow() -> None:
    driver_path = _REPO_ROOT / "scripts" / "windows_clean_user_e2e.py"
    workflow_path = _WORKFLOWS_DIR / "windows-user-guide-e2e.yml"
    driver = driver_path.read_text(encoding="utf-8")
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "requirement-start.json" in driver
    assert '"--input-file"' in driver
    assert "requirement-status.json" in driver
    assert "requirement-freeze.json" in driver
    assert '"--yes"' in driver
    assert "workitem-init.txt" in driver
    assert "windows_clean_user_e2e_support.py" in workflow
    assert 'spec_root / "spec.md"' not in driver


def test_historical_update_prompt_workflow_is_not_published() -> None:
    assert not (_WORKFLOWS_DIR / "windows-update-prompt-e2e.yml").exists()


def test_windows_online_job_does_not_install_retired_lean_governance() -> None:
    workflow = (_WORKFLOWS_DIR / "windows-user-guide-e2e.yml").read_text(
        encoding="utf-8"
    )

    assert "Run the installed Lean Code user journey" not in workflow
    assert "windows_lean_code_e2e.py" not in workflow
    assert "windows_lean_code_e2e_support.py" not in workflow
    assert "windows-clean-online-user-e2e-evidence" in workflow


def test_posix_offline_smoke_matrix_concurrency_is_job_scoped() -> None:
    workflow_path = _WORKFLOWS_DIR / "posix-offline-smoke.yml"

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert "concurrency" not in workflow
    assert workflow["jobs"]["smoke"]["concurrency"] == {
        "group": "posix-offline-smoke-${{ github.event.pull_request.number || github.ref }}-${{ matrix.os }}",
        "cancel-in-progress": True,
    }


def test_compatibility_gate_statically_layers_fast_and_full_assurance() -> None:
    workflow_path = _WORKFLOWS_DIR / "compatibility-gate.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = workflow[True]

    assert {
        "pull_request",
        "merge_group",
        "workflow_dispatch",
        "workflow_call",
        "schedule",
    } <= set(triggers)
    assert triggers["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
        "converted_to_draft",
    ]
    assert triggers["workflow_call"]["inputs"]["force_full"] == {
        "description": "Force source, platform and Python compatibility assurance.",
        "required": False,
        "type": "boolean",
        "default": False,
    }
    jobs = workflow["jobs"]
    assert jobs["fast-gate"]["runs-on"] == "ubuntu-latest"
    fast_run = next(s["run"] for s in jobs["fast-gate"]["steps"]
                    if s.get("name") == "Run fixed fast suite")
    assert 'pytest -q -x "${fast_tests[@]}"' in fast_run
    fast_nodes = [
        line.strip()
        for line in (_REPO_ROOT / ".github/ci/fast-gate-tests.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for failed_node in (
        "tests/integration/test_simulation_quantified_loop.py::"
        "test_concurrent_begin_has_one_identity_and_frozen_start",
        "tests/integration/test_pr_review_diff_capacity.py::"
        "test_v12_feedback_rejected_cannot_expand_scope",
        "tests/integration/test_counterexample_lifecycle.py::"
        "test_actual_observation_or_v0_cannot_change_the_state_claimed_by_v1[v0]",
    ):
        assert fast_nodes.count(failed_node) == 1
    assert "authority-check" not in jobs
    assert "baseline-preflight" not in jobs
    cells = jobs["cross-platform-validation"]["strategy"]["matrix"]["include"]
    assert [(c["os"], c["python-version"], c["suite"]) for c in cells] == [
        ("ubuntu-latest", "3.11", "full"),
        ("macos-latest", "3.11", "platform"),
        ("windows-latest", "3.11", "platform"),
        ("ubuntu-latest", "3.12", "python"),
        ("ubuntu-latest", "3.13", "python"),
        ("ubuntu-latest", "3.14", "python"),
        ("windows-latest", "3.14", "platform"),
    ]
    assert jobs["cross-platform-validation"]["needs"] == "fast-gate"
    assert jobs["windows-shell-smoke"]["needs"] == "fast-gate"
    # PR 验证合并树；发行核对相同树，避免合并后再重复整库执行。
    assert "push" not in triggers
    aggregate = next(s["run"] for s in jobs["merge-assurance"]["steps"]
                     if s.get("name") == "Rebuild, verify, and aggregate full evidence")
    for c in cells:
        assert f'{c["os"]}-py{c["python-version"]}' in aggregate
    full_condition = (
        "github.event_name != 'pull_request' || "
        "github.event.pull_request.draft == false || inputs.force_full == true"
    )
    assert jobs["cross-platform-validation"]["if"] == full_condition
    assert jobs["windows-shell-smoke"]["if"] == full_condition
    assert jobs["merge-assurance"]["if"] == "always()"
    assert jobs["merge-assurance"]["needs"] == [
        "fast-gate",
        "cross-platform-validation",
        "windows-shell-smoke",
    ]
    assert jobs["compatibility-gate-result"]["name"] == "Compatibility Gate Result"


def test_release_entrypoints_require_successful_assurance_for_the_exact_tree() -> None:
    for filename, consumers in (
        ("release-build.yml", ("build-smoke",)),
        ("release-artifact-smoke.yml", ("windows-zip", "posix-tar")),
    ):
        workflow = yaml.safe_load((_WORKFLOWS_DIR / filename).read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        for consumer in consumers:
            assert jobs[consumer]["needs"] == "release-assurance"
        assurance = jobs["release-assurance"]
        assert assurance["permissions"]["actions"] == "read"
        step = next(s for s in assurance["steps"] if s.get("name") == "Require tested release tree")
        assert "scripts/ci_static_assurance.py release-check" in step["run"]
        assert '--run-id "${ASSURANCE_RUN_ID}"' in step["run"]
        assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
        assert "continue-on-error" not in assurance
    compatibility = (_WORKFLOWS_DIR / "compatibility-gate.yml").read_text(encoding="utf-8")
    assert '--candidate-tree "$(git rev-parse HEAD^{tree})"' in compatibility


def test_primary_reuse_keeps_full_collection_and_reruns_all_affected_files():
    module = runpy.run_path(_REPO_ROOT / "scripts" / "ci_static_assurance.py")
    workflow = yaml.safe_load((_WORKFLOWS_DIR / "compatibility-gate.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["cross-platform-validation"]
    steps = {s.get("name"): s for s in job["steps"]}
    collect = steps["Collect exact candidate members"]["run"]
    assert 'if [[ "${TEST_SUITE}" != "full" ]]' in collect
    prepare = steps["Prepare unchanged primary evidence"]
    assert prepare["if"] == "matrix.suite == 'full'"
    assert "prepare-primary" in prepare["run"]
    run = steps["Run selected pytest suite"]["run"]
    assert "-n auto --dist worksteal --max-worker-restart=0" in run
    assert module["PRIMARY_REPAIR_SELECTION"] in run
    # 受影响进程测试必须整体替换并行参数，不能在并行参数后追加文件。
    assert "test_args=(" in module["PRIMARY_REPAIR_SELECTION"]
    assert "test_args+=(" not in module["PRIMARY_REPAIR_SELECTION"]
    for path in module["PRIMARY_REPAIR_TESTS"]:
        assert path in run
    aggregate = next(s["run"] for s in workflow["jobs"]["merge-assurance"]["steps"]
                     if s.get("name") == "Rebuild, verify, and aggregate full evidence")
    assert "combine-primary" in aggregate
    assert "fresh-manifest.json" in aggregate


def test_compatibility_gate_uses_candidate_artifacts_and_exact_results(
    tmp_path: Path,
) -> None:
    workflow = (_WORKFLOWS_DIR / "compatibility-gate.yml").read_text(encoding="utf-8")

    assert "trusted-base" not in workflow
    assert "authority_ref" not in workflow
    assert "test-baseline.json" not in workflow
    assert "test-lineage.json" not in workflow
    assert "python scripts/ci_static_assurance.py collect" in workflow
    assert "python scripts/ci_static_assurance.py cell-evidence" in workflow
    assert "python scripts/ci_static_assurance.py aggregate" in workflow
    assert "--ignore=tests/e2e/stage_review" not in workflow
    assert "--junitxml=ci-evidence/${CELL}/compatibility-results.xml" in workflow
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    )
    assert "if-no-files-found: error" in workflow
    assert "--maxfail" not in workflow
    assert "continue-on-error" not in workflow

    parsed = yaml.safe_load(workflow)
    matrix_steps = parsed["jobs"]["cross-platform-validation"]["steps"]
    assert all("mapfile" not in str(step.get("run", "")) for step in matrix_steps)
    assert all("cell-evidence" not in str(step.get("run", "")) for step in matrix_steps)
    full_pytest_step = next(
        step for step in matrix_steps if step.get("name") == "Run selected pytest suite"
    )
    assert "uv run pytest" in full_pytest_step["run"]
    assert (
        "--junitxml=ci-evidence/${CELL}/compatibility-results.xml"
        in full_pytest_step["run"]
    )
    step_names = [step.get("name") for step in matrix_steps]
    assert step_names.index("Doctor") < step_names.index("Run selected pytest suite")
    assert "--dist worksteal" in full_pytest_step["run"]
    assert '"${test_args[@]}"' in full_pytest_step["run"]
    collect_step = next(s for s in matrix_steps if s.get("name") == "Collect exact candidate members")
    assert "--pytest-arg" in collect_step["run"]
    suite_path = '".github/ci/${TEST_SUITE}-gate-tests.txt"'
    assert suite_path in collect_step["run"]
    assert suite_path in full_pytest_step["run"]
    platform_nodes = [
        line.strip()
        for line in (_REPO_ROOT / ".github/ci/platform-gate-tests.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    upgrade_node = (
        "tests/integration/test_cli_update_notice_process.py::"
        "test_windows_launcher_can_upgrade_its_live_installed_wheel"
    )
    assert platform_nodes.count(upgrade_node) == 1
    merge_steps = parsed["jobs"]["merge-assurance"]["steps"]
    assert all("uv run python" not in str(step.get("run", "")) for step in merge_steps)
    gate_script = merge_steps[0]["run"]
    assert "needs.cross-platform-validation.result" in gate_script
    aggregate_script = next(
        step["run"]
        for step in merge_steps
        if step.get("name") == "Rebuild, verify, and aggregate full evidence"
    )
    assert "python scripts/ci_static_assurance.py cell-evidence" in aggregate_script
    assert "python scripts/ci_static_assurance.py aggregate" in aggregate_script
    assert "started-at.txt" in aggregate_script
    assert "finished-at.txt" in aggregate_script
    assert "--baseline" not in aggregate_script
    assert "--lineage" not in aggregate_script

    sentinel = runpy.run_path(
        _REPO_ROOT / "scripts" / "ci_snapshot_control_sentinel.py"
    )
    expected_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-cov",
        "tests/unit/test_review_kernel.py::"
        "test_merge_expert_findings_deduplicates_without_deciding_close",
    ]
    assert sentinel["SENTINEL_NODE"] == expected_command[-1]
    assert sentinel["SENTINEL_ROUNDS"] == 5
    success_calls: list[list[str]] = []

    def successful_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        success_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    success = sentinel["run_snapshot_control_sentinel"](successful_runner)
    assert success["success"] is True
    assert success["exit_code"] == 0
    assert success["declared_rounds"] == sentinel["SENTINEL_ROUNDS"]
    assert success["executed_rounds"] == sentinel["SENTINEL_ROUNDS"]
    assert len(success_calls) == sentinel["SENTINEL_ROUNDS"]
    assert all(command == expected_command for command in success_calls)
    assert [attempt["command"] for attempt in success["attempts"]] == [
        expected_command
    ] * sentinel["SENTINEL_ROUNDS"]
    assert [attempt["returncode"] for attempt in success["attempts"]] == [0] * 5

    failure_calls: list[list[str]] = []

    def failing_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        failure_calls.append(command)
        return subprocess.CompletedProcess(
            command, 17 if len(failure_calls) == 2 else 0
        )

    failure = sentinel["run_snapshot_control_sentinel"](failing_runner)
    assert failure["success"] is False
    assert failure["exit_code"] == 17
    assert failure["declared_rounds"] == sentinel["SENTINEL_ROUNDS"]
    assert failure["executed_rounds"] == 2
    assert failure_calls == [expected_command, expected_command]
    assert [attempt["returncode"] for attempt in failure["attempts"]] == [0, 17]

    cli_success_output = tmp_path / "nested" / "success.json"
    assert (
        sentinel["main"](["--output", str(cli_success_output)], successful_runner) == 0
    )
    cli_success_text = cli_success_output.read_text(encoding="utf-8")
    cli_success = json.loads(cli_success_text)
    assert cli_success_output.parent.is_dir()
    assert cli_success["success"] is True
    assert cli_success["exit_code"] == 0
    assert cli_success["declared_rounds"] == 5
    assert cli_success["executed_rounds"] == 5
    assert cli_success_text == (
        json.dumps(
            cli_success,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )

    cli_failure_calls: list[list[str]] = []

    def cli_failing_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        cli_failure_calls.append(command)
        return subprocess.CompletedProcess(
            command, 23 if len(cli_failure_calls) == 2 else 0
        )

    cli_failure_output = tmp_path / "nested" / "failure.json"
    assert (
        sentinel["main"](["--output", str(cli_failure_output)], cli_failing_runner)
        == 23
    )
    cli_failure = json.loads(cli_failure_output.read_text(encoding="utf-8"))
    assert cli_failure_calls == [expected_command, expected_command]
    assert cli_failure["success"] is False
    assert cli_failure["exit_code"] == 23
    assert cli_failure["declared_rounds"] == 5
    assert cli_failure["executed_rounds"] == 2
    assert [attempt["returncode"] for attempt in cli_failure["attempts"]] == [0, 23]

    def unavailable_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise OSError("secret-token=/private/ci/runner")

    runner_error_output = tmp_path / "nested" / "runner-error.json"
    assert (
        sentinel["main"](["--output", str(runner_error_output)], unavailable_runner)
        == 1
    )
    runner_error_text = runner_error_output.read_text(encoding="utf-8")
    runner_error = json.loads(runner_error_text)
    assert runner_error["success"] is False
    assert runner_error["exit_code"] == 1
    assert runner_error["declared_rounds"] == 5
    assert runner_error["executed_rounds"] == 0
    assert runner_error["attempts"] == []
    assert runner_error["runner_error"] == {
        "reason": "runner_exception",
        "type": "OSError",
    }
    assert "secret-token" not in runner_error_text
    assert "/private/ci/runner" not in runner_error_text

    sentinel_step = next(
        step
        for step in matrix_steps
        if step.get("name") == "Run fixed SnapshotControl stability sentinel"
    )
    assert sentinel_step["if"] == (
        "matrix.os == 'windows-latest' && matrix.python-version == '3.14'"
    )
    assert sentinel_step["run"] == (
        "uv run python scripts/ci_snapshot_control_sentinel.py "
        "--output ci-evidence/${{ env.CELL }}/snapshot-control-sentinel.json"
    )
    assert (
        step_names.index("Run selected pytest suite")
        < step_names.index("Run fixed SnapshotControl stability sentinel")
        < step_names.index("Record raw cell completion")
    )
    evidence_upload = next(
        step
        for step in matrix_steps
        if step.get("name") == "Upload compatibility evidence"
    )
    assert evidence_upload["if"] == "always()"


def test_compatibility_gate_uses_candidate_local_execution_evidence_only() -> None:
    """普通 CI 不得以 protected baseline/lineage 阻止有意删除废止测试。"""
    workflow_text = (_WORKFLOWS_DIR / "compatibility-gate.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]

    assert "authority-check" not in jobs
    assert "baseline-preflight" not in jobs
    assert "trusted-base" not in workflow_text
    assert "test-baseline.json" not in workflow_text
    assert "test-lineage.json" not in workflow_text
    assert "baseline-preflight" not in workflow_text
    assert "verify-transition" not in workflow_text
    assert "validate-lineage" not in workflow_text
    assert "decide-mode" not in workflow_text
    assert "collect" in workflow_text
    assert "cell-evidence" in workflow_text
    assert "aggregate" in workflow_text
    cells = jobs["cross-platform-validation"]["strategy"]["matrix"]["include"]
    assert sum(c["suite"] == "full" for c in cells) == 1
    assert {c["os"] for c in cells} == {"ubuntu-latest", "macos-latest", "windows-latest"}
    assert "windows-shell-smoke" in jobs
    assert "fast-gate" in jobs


def test_compatibility_gate_pull_request_executes_merge_commit() -> None:
    workflow_text = (_WORKFLOWS_DIR / "compatibility-gate.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.safe_load(workflow_text)

    merge_candidate_ref = "inputs.candidate_ref || github.sha"
    checkout_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkout_steps
    assert all(
        step["with"]["ref"] == f"${{{{ {merge_candidate_ref} }}}}"
        for step in checkout_steps
    )
    assert "github.event.pull_request.head.sha" not in workflow_text


def test_candidate_ci_helper_is_not_packaged_for_ordinary_users() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "scripts/ci_static_assurance.py" not in pyproject
    assert (_REPO_ROOT / ".github" / "ci" / "fast-gate-tests.txt").is_file()
    assert not (_REPO_ROOT / ".github" / "ci" / "test-baseline.json").exists()
    assert not (_REPO_ROOT / ".github" / "ci" / "test-lineage.json").exists()


def test_github_workflows_use_node24_compatible_core_actions() -> None:
    legacy_actions = {
        "actions/checkout@v4",
        "actions/setup-python@v5",
    }

    for workflow_path in sorted(_WORKFLOWS_DIR.glob("*.yml")):
        workflow = workflow_path.read_text(encoding="utf-8")
        for legacy_action in legacy_actions:
            assert legacy_action not in workflow, (
                f"{workflow_path.relative_to(_REPO_ROOT)} still uses {legacy_action}"
            )

    release_build = (_WORKFLOWS_DIR / "release-build.yml").read_text(encoding="utf-8")
    assert "actions/checkout@v6" in release_build
    assert "actions/setup-python@v6" in release_build
    assert "astral-sh/setup-uv@v7" in release_build
    assert "actions/upload-artifact@v7" in release_build
    assert "actions/attest" not in release_build

    compatibility = yaml.safe_load(
        (_WORKFLOWS_DIR / "compatibility-gate.yml").read_text(encoding="utf-8")
    )
    external_uses = [
        step["uses"]
        for job in compatibility["jobs"].values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and not step["uses"].startswith("./")
    ]
    assert external_uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in external_uses)
