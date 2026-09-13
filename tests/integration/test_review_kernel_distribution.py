"""Installed-package contract for the intentionally small review product."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import packaging_backend
import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app

_REVIEW_ADAPTER_MEMBERS = {
    "ai_sdlc/adapters/codex/AI-SDLC.md",
    "ai_sdlc/adapters/claude_code/AI-SDLC.md",
    "ai_sdlc/adapters/cursor/rules/ai-sdlc.md",
    "ai_sdlc/adapters/vscode/AI-SDLC.md",
}
_REQUIRED_PACKAGE_MEMBERS = {
    "ai_sdlc/cli/loop_cmd.py",
    "ai_sdlc/cli/loop_review_cmd.py",
    "ai_sdlc/cli/pr_review_cmd.py",
    "ai_sdlc/cli/self_update_cmd.py",
    "ai_sdlc/core/requirement_loop.py",
    "ai_sdlc/core/design_contract_loop.py",
    "ai_sdlc/core/implementation_loop.py",
    "ai_sdlc/core/frontend_evidence_loop.py",
    "ai_sdlc/core/frontend_delivery_service.py",
    "ai_sdlc/core/frontend_browser_gate_runtime.py",
    "ai_sdlc/core/frontend_visual_a11y_evidence_provider.py",
    "ai_sdlc/core/review_kernel.py",
    "ai_sdlc/core/slimming_advice.py",
    "ai_sdlc/core/pr_review_provider.py",
    "ai_sdlc/core/update_advisor.py",
    *_REVIEW_ADAPTER_MEMBERS,
    "ai_sdlc/rules/verification.md",
}
_FORBIDDEN_MEMBER_FRAGMENTS = (
    "/core/loop_review_continuation.py",
    "/cli/agentops_cmd.py",
    "/cli/enterprise_cmd.py",
    "/cli/host_runtime_cmd.py",
    "/cli/program_cmd.py",
    "/cli/provenance_cmd.py",
    "/cli/stage_cmd.py",
    "/cli/sub_apps.py",
    "/cli/telemetry_cmd.py",
    "/cli/trace_cmd.py",
    "/core/agentops_bridge.py",
    "/core/dispatcher.py",
    "/core/executor.py",
    "/core/host_runtime_manager.py",
    "/core/program_service.py",
    "/core/provenance_gate.py",
    "/core/release_gate.py",
    "/core/runner.py",
    "/backends/",
    "/parallel/",
    "/stages/",
    "/studios/",
    "/telemetry/",
    "/frontend_contract_runtime_attachment.py",
    "/frontend_cross_provider_consistency",
    "/frontend_delivery_truth.py",
    "/frontend_gate_policy",
    "/frontend_gate_verification.py",
    "/frontend_inheritance_truth.py",
    "/frontend_generation_constraint",
    "/frontend_page_ui_schema",
    "/frontend_provider_expansion",
    "/frontend_provider_profile",
    "/frontend_provider_runtime_adapter",
    "/frontend_quality_platform",
    "/frontend_solution_confirmation_artifacts.py",
    "/frontend_theme_token_governance",
    "/frontend_ui_kernel",
    "/frontend-governance/",
    "/program-manifest.example.yaml",
)
_FORBIDDEN_ROOTS = {"governance", "kernel", "managed", "providers"}
_HISTORICAL_WORKITEM_IDS = (
    "003",
    "012",
    "014",
    "015",
    "016",
    "017",
    "018",
    "067",
    "068",
    "069",
    "071",
    "073",
    "085",
    "086",
    "123",
    "147",
    "148",
    "149",
    "150",
    "151",
    "153",
    "189",
    "190",
    "191",
    "192",
    "193",
    "194",
    "195",
)


def _normalized_member(name: str, *, sdist: bool) -> str:
    normalized = name
    if sdist:
        _, normalized = normalized.split("/", 1)
        if normalized.startswith("src/"):
            normalized = normalized.removeprefix("src/")
    return normalized


def _assert_distribution_membership(
    names: set[str],
    *,
    read_member,
    sdist: bool,
) -> None:
    normalized_to_raw = {
        _normalized_member(name, sdist=sdist): name
        for name in names
        if "/" in name or not sdist
    }
    normalized = set(normalized_to_raw)

    assert normalized >= _REQUIRED_PACKAGE_MEMBERS
    if sdist:
        assert {
            "templates/spec-template.md",
            "templates/plan-template.md",
            "templates/tasks-template.md",
            "templates/execution-log-template.md",
        } <= normalized
        assert "scripts/frontend_browser_gate_probe_runner.mjs" in normalized
    else:
        assert {
            "ai_sdlc/templates/spec-template.md",
            "ai_sdlc/templates/plan-template.md",
            "ai_sdlc/templates/tasks-template.md",
            "ai_sdlc/templates/execution-log-template.md",
        } <= normalized
        assert (
            "ai_sdlc/runtime_assets/frontend_browser_gate_probe_runner.mjs"
            in normalized
        )

    for name in normalized:
        rooted = f"/{name}"
        assert not any(fragment in rooted for fragment in _FORBIDDEN_MEMBER_FRAGMENTS)
        assert name.split("/", 1)[0] not in _FORBIDDEN_ROOTS

    historical_tokens = tuple(
        token
        for workitem_id in _HISTORICAL_WORKITEM_IDS
        for token in (
            f"specs/{workitem_id}".encode(),
            f"work item {workitem_id}".encode(),
            f"work_item_{workitem_id}".encode(),
            f"WI{workitem_id}".encode(),
        )
    )
    text_suffixes = {".json", ".j2", ".md", ".mjs", ".py", ".yaml", ".yml"}
    for normalized_name, raw_name in normalized_to_raw.items():
        if Path(normalized_name).suffix not in text_suffixes:
            continue
        content = read_member(raw_name)
        assert not any(token in content for token in historical_tokens), normalized_name
        if normalized_name in _REVIEW_ADAPTER_MEMBERS:
            guidance = content.decode("utf-8")
            assert "ai-sdlc pr-review verify --cwd . -- <argv...>" in guidance
            assert "ai-sdlc pr-review record-evidence" not in guidance
            assert "平台策略拒绝不属于普通技术重试" in guidance


def _load_sdist_backend(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("pr4_sdist_packaging_backend", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wheel_and_sdist_ship_only_the_retained_review_runtime(tmp_path: Path) -> None:
    wheel_path = tmp_path / packaging_backend.build_wheel(str(tmp_path))
    sdist_path = tmp_path / packaging_backend.build_sdist(str(tmp_path))

    with zipfile.ZipFile(wheel_path) as archive:
        wheel_names = set(archive.namelist())
        _assert_distribution_membership(
            wheel_names,
            read_member=archive.read,
            sdist=False,
        )
    with tarfile.open(sdist_path, "r:gz") as archive:
        sdist_names = set(archive.getnames())
        _assert_distribution_membership(
            sdist_names,
            read_member=lambda name: archive.extractfile(name).read(),  # type: ignore[union-attr]
            sdist=True,
        )

    extracted = tmp_path / "sdist"
    extracted.mkdir()
    with tarfile.open(sdist_path, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    source_root = next(extracted.iterdir())
    rebuilt_dir = tmp_path / "rebuilt"
    rebuilt_dir.mkdir()
    sdist_backend = _load_sdist_backend(source_root / "packaging_backend.py")
    rebuilt_wheel = rebuilt_dir / sdist_backend.build_wheel(str(rebuilt_dir))
    with zipfile.ZipFile(rebuilt_wheel) as archive:
        _assert_distribution_membership(
            set(archive.namelist()),
            read_member=archive.read,
            sdist=False,
        )


def test_offline_bundle_nested_wheel_obeys_distribution_contract(
    tmp_path: Path,
) -> None:
    built_dir = tmp_path / "built"
    built_dir.mkdir()
    wheel = built_dir / packaging_backend.build_wheel(str(built_dir))
    bundle_wheels = tmp_path / "ai-sdlc-offline" / "wheels"
    bundle_wheels.mkdir(parents=True)
    nested_wheel = bundle_wheels / wheel.name
    shutil.copy2(wheel, nested_wheel)

    with zipfile.ZipFile(nested_wheel) as archive:
        _assert_distribution_membership(
            set(archive.namelist()),
            read_member=archive.read,
            sdist=False,
        )


@pytest.mark.parametrize(
    ("agent_target", "guidance_path"),
    [
        ("codex", "AGENTS.md"),
        ("claude_code", ".claude/CLAUDE.md"),
        ("cursor", ".cursor/rules/ai-sdlc.mdc"),
        ("vscode", ".github/copilot-instructions.md"),
    ],
)
def test_fresh_init_installs_minimal_review_guidance_without_legacy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_target: str,
    guidance_path: str,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "init",
            str(tmp_path),
            "--agent-target",
            agent_target,
            "--shell",
            "powershell",
        ],
    )

    assert result.exit_code == 0, result.output
    guidance = (tmp_path / guidance_path).read_text(encoding="utf-8")
    assert "五类结果的内置动态专家复核" in guidance
    assert "expert_roles" in guidance
    assert "每个角色启动一个全新且只读的独立上下文" in guidance
    assert "ai-sdlc loop review-record" in guidance
    assert "不得要求用户手动触发专家" in guidance
    assert "review-outcome-round-2.json" in guidance
    assert "只提供建议" in guidance
    assert "Local PR Review" in guidance
    monkeypatch.chdir(tmp_path)
    rejected = CliRunner().invoke(
        app,
        ["pr-review", "record-evidence", "--evidence", "pytest passed", "--json"],
    )
    assert rejected.exit_code == 1, rejected.output
    next_action = json.loads(rejected.stdout)["next_action"]
    assert next_action == "Run ai-sdlc pr-review verify --cwd . -- <argv...>."
    assert next_action.removeprefix("Run ").removesuffix(".") in guidance
    assert "ai-sdlc pr-review record-evidence" not in guidance
    assert "CLI 返回的 Next" in guidance
    assert "不得把人工字符串作为验证证据" in guidance
    assert "平台策略拒绝不属于普通技术重试" in guidance
    for retired in (
        "StageReviewSession",
        "FindingLedger",
        "StageCloseCertificate",
        "lean-check",
        "lean-verify",
        "lean-regression",
        "ci-certificate",
    ):
        assert retired not in guidance

    for retired_path in (
        ".ai-sdlc/sessions",
        ".ai-sdlc/certificates",
        ".ai-sdlc/attestations",
        ".ai-sdlc/activation",
        ".ai-sdlc/policies/stage-gate-activation-policy.json",
    ):
        assert not (tmp_path / retired_path).exists()
