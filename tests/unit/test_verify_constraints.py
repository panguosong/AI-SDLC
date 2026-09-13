"""Unit tests for verify_constraints."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import ai_sdlc.core.verify_constraints as verify_constraints_module
from ai_sdlc.context.state import save_checkpoint
from ai_sdlc.core.verify_constraints import (
    ConstraintProfile,
    ConstraintReport,
)
from ai_sdlc.core.verify_constraints import (
    build_constraint_report as _build_constraint_report,
)
from ai_sdlc.core.verify_constraints import (
    build_verification_gate_context as _build_verification_gate_context,
)
from ai_sdlc.core.verify_constraints import (
    collect_constraint_blockers as _collect_constraint_blockers,
)
from ai_sdlc.models.state import Checkpoint, FeatureInfo


# This module exercises AI-SDLC's own governance contract. Consumer-profile
# behavior is tested through the module-qualified calls below so the historical
# framework assertions remain explicit about their intended scope.
def build_constraint_report(root: Path) -> ConstraintReport:
    return _build_constraint_report(
        root,
        profile=ConstraintProfile.SELF_DEVELOPMENT,
    )


def build_verification_gate_context(root: Path) -> dict[str, object]:
    return _build_verification_gate_context(
        root,
        profile=ConstraintProfile.SELF_DEVELOPMENT,
    )


def collect_constraint_blockers(root: Path) -> list[str]:
    return _collect_constraint_blockers(
        root,
        profile=ConstraintProfile.SELF_DEVELOPMENT,
    )


def _write_consumer_checkpoint(root: Path, work_item_id: str) -> None:
    memory = root / ".ai-sdlc" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "constitution.md").write_text(
        "# Consumer constitution\n",
        encoding="utf-8",
    )
    spec_dir = root / "specs" / f"{work_item_id}-consumer-work"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Consumer work\n", encoding="utf-8")
    save_checkpoint(
        root,
        Checkpoint(
            current_stage="verify",
            feature=FeatureInfo(
                id=work_item_id,
                spec_dir=f"specs/{work_item_id}-consumer-work",
                design_branch="team/design",
                feature_branch="team/feature",
                current_branch="developer-branch",
            ),
        ),
    )


@pytest.mark.parametrize(
    "work_item_id",
    ("consumer-alpha", "consumer-beta"),
)
def test_project_profile_ignores_framework_work_item_number_collisions(
    tmp_path: Path,
    work_item_id: str,
) -> None:
    _write_consumer_checkpoint(tmp_path, work_item_id)

    report = verify_constraints_module.build_constraint_report(
        tmp_path,
        profile=verify_constraints_module.ConstraintProfile.PROJECT,
    )
    context = verify_constraints_module.build_verification_gate_context(
        tmp_path,
        profile=verify_constraints_module.ConstraintProfile.PROJECT,
    )

    self_only_objects = {
        "framework_defect_backlog",
        "reconcile_smoke_contract",
        "verification_profiles",
    }
    assert report.profile is verify_constraints_module.ConstraintProfile.PROJECT
    assert not (self_only_objects & set(report.check_objects))
    assert not any(
        key.startswith("frontend_") for key in context if key.endswith("_verification")
    )
    assert context["verification_profile"] == "project"


def test_project_profile_keeps_consumer_agents_frontend_confirmation_rule(
    tmp_path: Path,
) -> None:
    memory = tmp_path / ".ai-sdlc" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "constitution.md").write_text(
        "# Consumer constitution\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text(
        "前端需求必须先讨论，但这里缺少确认边界。\n",
        encoding="utf-8",
    )

    blockers = verify_constraints_module.collect_constraint_blockers(
        tmp_path,
        profile=verify_constraints_module.ConstraintProfile.PROJECT,
    )

    assert any(
        "frontend solution confirmation instruction drift" in item for item in blockers
    )
    assert any("AGENTS.md" in item for item in blockers)


def test_self_development_profile_keeps_framework_report_objects(
    tmp_path: Path,
) -> None:
    memory = tmp_path / ".ai-sdlc" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "constitution.md").write_text(
        "# Framework constitution\n",
        encoding="utf-8",
    )

    report = verify_constraints_module.build_constraint_report(
        tmp_path,
        profile=verify_constraints_module.ConstraintProfile.SELF_DEVELOPMENT,
    )

    assert (
        report.profile is verify_constraints_module.ConstraintProfile.SELF_DEVELOPMENT
    )
    assert {
        "framework_defect_backlog",
        "reconcile_smoke_contract",
        "verification_profiles",
    }.issubset(report.check_objects)


def _write_framework_backlog(root: Path, entry_body: str) -> None:
    path = root / "docs" / "framework-defect-backlog.zh-CN.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# 框架缺陷待办池\n\n## FD-2026-03-26-001 | 示例条目\n\n{entry_body}",
        encoding="utf-8",
    )


def _write_verification_profile_docs(
    root: Path,
    *,
    include_rules_only: bool = True,
    include_checklist_clean_gate: bool = True,
) -> None:
    rules_dir = root / "src" / "ai_sdlc" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    verification = (
        "# 完成前验证协议\n\n"
        "## 最小 fresh verification 画像\n\n"
        "- `docs-only`：至少执行 `uv run ai-sdlc verify constraints`\n"
    )
    if include_rules_only:
        verification += "- `rules-only`：至少执行 `uv run ai-sdlc verify constraints`\n"
    verification += (
        "- `truth-only`：执行 `uv run ai-sdlc verify constraints`\n"
        "- `code-change`：执行 `uv run pytest`、`uv run ruff check`、`uv run ai-sdlc verify constraints`\n"
        "- 既有能力未退化：既有入口 / 既有选项 / 既有输出必须有回归证据，不能只验证新功能 happy path。\n"
    )
    (rules_dir / "verification.md").write_text(verification, encoding="utf-8")
    (rules_dir / "code-review.md").write_text(
        "# 代码审查协议\n\n"
        "### 维度 2.1：既有能力不退化\n\n"
        "- 检查既有用户可见能力。\n"
        "- 未声明废弃不得删除既有入口。\n"
        "- 必须有回归测试，不能只覆盖新能力 happy path。\n",
        encoding="utf-8",
    )

    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    checklist = (
        "# 合并前自检清单\n\n"
        "- `uv run ai-sdlc verify constraints`\n"
        "- `uv run pytest -q`\n"
        "- `uv run ruff check src tests scripts`\n"
    )
    if include_checklist_clean_gate:
        checklist += "- `python scripts/validate_public_release_identity.py .`\n"
    checklist += (
        "- 用户可见行为与文档一致。\n"
        "- 修复必须包含回归测试。\n"
        "- 高影响动作具备确认、回滚或恢复路径。\n"
    )
    (docs_dir / "pull-request-checklist.zh.md").write_text(checklist, encoding="utf-8")


def _write_reconcile_smoke_contract_surfaces(
    root: Path,
    *,
    include_workflow_markers: bool = True,
) -> None:
    rules_dir = root / "src" / "ai_sdlc" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "verification.md").write_text(
        "# 完成前验证协议\n\n"
        "## Reconcile Smoke Contract\n\n"
        "- `Existing Artifact Probe` 与 `ai-sdlc recover --reconcile` 属于 Windows smoke 依赖的仓库状态诊断契约。\n"
        "- 变更上述诊断输出契约时，必须同步更新 `.github/workflows/windows-offline-smoke.yml` 与相关测试。\n",
        encoding="utf-8",
    )

    cli_dir = root / "src" / "ai_sdlc" / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    (cli_dir / "commands.py").write_text(
        "table_title = 'Existing Artifact Probe'\n"
        "next_step = 'ai-sdlc recover --reconcile'\n",
        encoding="utf-8",
    )
    (cli_dir / "run_cmd.py").write_text(
        "message = '已停止当前运行，避免基于过时 checkpoint 继续执行。'\n",
        encoding="utf-8",
    )

    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    workflow_text = "Existing Artifact Probe\nrecover --reconcile\n"
    if include_workflow_markers:
        workflow_text += (
            "ai-sdlc run --dry-run reported repo-state reconciliation diagnostics; "
            "treating this as smoke pass.\n"
        )
    (workflow_dir / "windows-offline-smoke.yml").write_text(
        workflow_text,
        encoding="utf-8",
    )


def _write_doc_first_rule_surfaces(
    root: Path,
    *,
    include_pipeline_terms: bool = True,
    include_skip_registry_terms: bool = True,
) -> None:
    rules_dir = root / "src" / "ai_sdlc" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    pipeline = (
        "# 流水线总控规则\n\n"
        "16. 宿主规划与仓库阶段区分：法定下一步是 design/decompose，再 verify，再 execute。\n"
    )
    if include_pipeline_terms:
        pipeline += (
            "当用户明确要求“先文档 / 先需求 / 先 spec-plan-tasks”时，默认动作必须停在 "
            "design/decompose，不得直接改产品代码。\n"
        )
    (rules_dir / "pipeline.md").write_text(pipeline, encoding="utf-8")

    skip_registry = (
        "# 代理跳过记录\n\n"
        "| 日期 | 发现阶段 | 跳过内容摘要 | 根因 | 框架强化建议 | wi_id | 状态 |\n"
        "|------|----------|--------------|------|--------------|-------|------|\n"
    )
    if include_skip_registry_terms:
        skip_registry += (
            "\n仅文档 / 仅需求沉淀任务必须先更新 spec.md / plan.md / tasks.md；"
            "禁止默认修改 `src/`、`tests/`。\n"
        )
    (rules_dir / "agent-skip-registry.zh.md").write_text(
        skip_registry, encoding="utf-8"
    )


def _init_git_repo(root: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=root, check=True, capture_output=True
    )


def _create_branch_ahead_of_main(root: Path, branch_name: str) -> None:
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "scratch.txt").write_text(f"{branch_name}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"feat: {branch_name}"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=root, check=True, capture_output=True
    )


def _write_branch_lifecycle_fixture(
    root: Path,
    *,
    wi_name: str = "001-wi",
    branch_disposition_status: str = "待最终收口",
) -> None:
    _init_git_repo(root)
    mem = root / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "constitution.md").write_text("# Constitution\n", encoding="utf-8")
    _write_framework_backlog(
        root,
        (
            "- 现象: 发现框架缺陷\n"
            "- 触发场景: 用户要求登记\n"
            "- 影响范围: 规则与流程\n"
            "- 根因分类: B\n"
            "- 建议改动层级: rule / policy, workflow\n"
            "- prompt / context: 会话内发现偏离\n"
            "- rule / policy: pipeline.md 条款 17\n"
            "- middleware: 无\n"
            "- workflow: 需登记再继续\n"
            "- tool: ai-sdlc verify constraints\n"
            "- eval: 结构化字段完整率\n"
            "- 风险等级: 中\n"
            "- 可验证成功标准: verify constraints 无 BLOCKER\n"
            "- 是否需要回归测试补充: 是\n"
        ),
    )
    _write_verification_profile_docs(root)
    _write_doc_first_rule_surfaces(root)

    wi_dir = root / "specs" / wi_name
    wi_dir.mkdir(parents=True, exist_ok=True)
    (wi_dir / "tasks.md").write_text(
        "### Task 1.1 — 示例\n- **依赖**：无\n- **验收标准（AC）**：\n  1. 示例\n",
        encoding="utf-8",
    )
    (wi_dir / "task-execution-log.md").write_text(
        "# Log\n\n"
        "### Batch 2026-03-31-001 | Batch 1 demo\n\n"
        "#### 2.5 任务/计划同步状态（Mandatory）\n"
        "- 关联 branch/worktree disposition 计划：`待最终收口`\n"
        "#### 2.8 归档后动作\n"
        f"- 当前批次 branch disposition 状态：`{branch_disposition_status}`\n"
        "- 当前批次 worktree disposition 状态：`待最终收口`\n",
        encoding="utf-8",
    )
    save_checkpoint(
        root,
        Checkpoint(
            current_stage="verify",
            feature=FeatureInfo(
                id=wi_name.split("-", 1)[0],
                spec_dir=f"specs/{wi_name}",
                design_branch=f"design/{wi_name}",
                feature_branch=f"feature/{wi_name}",
                current_branch="main",
            ),
        ),
    )
    _commit_all(root, "docs: seed branch lifecycle fixture")


def test_blocker_missing_constitution(tmp_path: Path) -> None:
    (tmp_path / ".ai-sdlc" / "state").mkdir(parents=True)
    b = collect_constraint_blockers(tmp_path)
    assert len(b) == 1
    assert "BLOCKER" in b[0]
    assert "constitution.md" in b[0]


def test_collect_constraint_blockers_deduplicates_cross_helper_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    monkeypatch.setattr(verify_constraints_module, "load_checkpoint", lambda _: None)
    monkeypatch.setattr(
        verify_constraints_module,
        "_framework_defect_backlog_blockers",
        lambda _: [
            "BLOCKER: duplicate helper blocker",
            "BLOCKER: duplicate helper blocker",
        ],
    )
    monkeypatch.setattr(
        verify_constraints_module,
        "_formal_artifact_target_blockers",
        lambda _: [
            "BLOCKER: duplicate helper blocker",
            "BLOCKER: distinct helper blocker",
        ],
    )
    monkeypatch.setattr(
        verify_constraints_module,
        "_backlog_breach_reference_blockers",
        lambda _: [],
    )
    monkeypatch.setattr(
        verify_constraints_module,
        "_release_docs_consistency_blockers",
        lambda _: [],
    )
    monkeypatch.setattr(
        verify_constraints_module,
        "_reconcile_smoke_contract_blockers",
        lambda _: [],
    )
    monkeypatch.setattr(
        verify_constraints_module,
        "_doc_first_surface_blockers",
        lambda _: [],
    )
    monkeypatch.setattr(
        verify_constraints_module,
        "_verification_profile_blockers",
        lambda _: [],
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert blockers == [
        "BLOCKER: duplicate helper blocker",
        "BLOCKER: distinct helper blocker",
    ]


def test_blocker_spec_dir_missing(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="x",
            spec_dir="specs/does-not-exist",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    b = collect_constraint_blockers(tmp_path)
    assert any("spec_dir" in x for x in b)
    assert any("BLOCKER" in x for x in b)


def test_pass_constitution_and_spec_dir(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    assert collect_constraint_blockers(tmp_path) == []


def test_structured_constraint_report_preserves_blockers(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)

    report = build_constraint_report(tmp_path)

    assert report.root == str(tmp_path)
    assert report.blockers == tuple(collect_constraint_blockers(tmp_path))
    assert report.source_name == "verify constraints"


def test_skip_registry_unmapped_rows_are_ignored(tmp_path: Path) -> None:
    """Only rows matching the current wi_id participate in the gate."""
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "spec.md").write_text("- **FR-001**: x\n", encoding="utf-8")
    (spec / "tasks.md").write_text(
        "### Task 1.1\n- **依赖**：无\n- **验收标准（AC）**：\n  1. ok\n",
        encoding="utf-8",
    )

    registry = tmp_path / "src" / "ai_sdlc" / "rules"
    registry.mkdir(parents=True)
    (registry / "agent-skip-registry.zh.md").write_text(
        "| 日期 | 发现阶段 | 跳过内容摘要 | 根因 | 框架强化建议 | wi_id | 状态 |\n"
        "|------|----------|--------------|------|--------------|-------|------|\n"
        "| 2026-03-26 | 执行 | x | A | 引入 FR-999 并补 Task 9.9 |  | 已记录 |\n"
        "| 2026-03-26 | 执行 | y | A | 引入 FR-888 | other-wi | 已记录 |\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    assert collect_constraint_blockers(tmp_path) == []


def test_scoped_skip_registry_lines_deduplicates_matching_rows() -> None:
    from ai_sdlc.core.verify_constraints import _scoped_skip_registry_lines

    reg_text = (
        "| 日期 | 发现阶段 | 跳过内容摘要 | 根因 | 框架强化建议 | wi_id | 状态 |\n"
        "|------|----------|--------------|------|--------------|-------|------|\n"
        "| 2026-03-26 | 执行 | z | A | 引入 FR-777 | 001-wi | 已记录 |\n"
        "| 2026-03-26 | 执行 | z | A | 引入 FR-777 | 001-wi | 已记录 |\n"
        "| 2026-03-26 | 执行 | y | A | 引入 FR-888 | other-wi | 已记录 |\n"
    )

    assert _scoped_skip_registry_lines(reg_text, "001-wi") == [
        "| 2026-03-26 | 执行 | z | A | 引入 FR-777 | 001-wi | 已记录 |"
    ]


def test_branch_lifecycle_blockers_deduplicate_helper_output(
    tmp_path: Path, monkeypatch
) -> None:
    spec_dir = tmp_path / "specs" / "001-wi"
    spec_dir.mkdir(parents=True)
    (tmp_path / ".git").mkdir()

    monkeypatch.setattr(
        verify_constraints_module,
        "evaluate_work_item_branch_lifecycle",
        lambda **_kwargs: type(
            "Result",
            (),
            {"blockers": ["branch blocker", "branch blocker"]},
        )(),
    )

    assert verify_constraints_module._branch_lifecycle_blockers(tmp_path, spec_dir) == [
        "branch blocker"
    ]


def test_formal_artifact_target_blockers_deduplicate_repeated_violations(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        verify_constraints_module,
        "detect_misplaced_formal_artifacts",
        lambda _root: (
            type(
                "Violation",
                (),
                {"path": "docs/superpowers/spec.md", "artifact_kind": "spec"},
            )(),
            type(
                "Violation",
                (),
                {"path": "docs/superpowers/spec.md", "artifact_kind": "spec"},
            )(),
        ),
    )

    assert verify_constraints_module._formal_artifact_target_blockers(
        Path("/tmp/project")
    ) == [
        "BLOCKER: misplaced formal artifact detected under docs/superpowers/*: "
        "docs/superpowers/spec.md (spec)"
    ]


def test_backlog_breach_reference_blockers_deduplicate_repeated_violations(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        verify_constraints_module,
        "collect_missing_backlog_entry_references",
        lambda _root: (
            type(
                "Violation",
                (),
                {"path": "specs/001-wi/spec.md", "missing_ids": ("FD-2026-04-22-001",)},
            )(),
            type(
                "Violation",
                (),
                {"path": "specs/001-wi/spec.md", "missing_ids": ("FD-2026-04-22-001",)},
            )(),
        ),
    )

    assert verify_constraints_module._backlog_breach_reference_blockers(
        Path("/tmp/project")
    ) == [
        "BLOCKER: breach_detected_but_not_logged: "
        "specs/001-wi/spec.md references missing backlog ids: FD-2026-04-22-001"
    ]


def test_framework_defect_backlog_blockers_deduplicate_repeated_entries(
    tmp_path: Path, monkeypatch
) -> None:
    backlog_path = tmp_path / "docs" / "framework-defect-backlog.zh-CN.md"
    backlog_path.parent.mkdir(parents=True)
    backlog_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(
        verify_constraints_module,
        "_parse_framework_defect_backlog",
        lambda _text: [
            ("FD-1", {"问题描述": "", "影响范围": ""}),
            ("FD-1", {"问题描述": "", "影响范围": ""}),
        ],
    )

    blockers = verify_constraints_module._framework_defect_backlog_blockers(tmp_path)

    assert blockers == [blockers[0]]
    assert blockers[0].startswith(
        "BLOCKER: framework-defect-backlog entry 'FD-1' missing required fields: "
    )


def test_skip_registry_blocks_only_matching_wi_row(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "spec.md").write_text("- **FR-001**: x\n", encoding="utf-8")
    (spec / "tasks.md").write_text(
        "### Task 1.1\n- **依赖**：无\n- **验收标准（AC）**：\n  1. ok\n",
        encoding="utf-8",
    )

    registry = tmp_path / "src" / "ai_sdlc" / "rules"
    registry.mkdir(parents=True)
    (registry / "agent-skip-registry.zh.md").write_text(
        "| 日期 | 发现阶段 | 跳过内容摘要 | 根因 | 框架强化建议 | wi_id | 状态 |\n"
        "|------|----------|--------------|------|--------------|-------|------|\n"
        "| 2026-03-26 | 执行 | x | A | 引入 FR-999 |  | 已记录 |\n"
        "| 2026-03-26 | 执行 | y | A | 引入 FR-888 | other-wi | 已记录 |\n"
        "| 2026-03-26 | 执行 | z | A | 引入 FR-777 | 001-wi | 已记录 |\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    b = collect_constraint_blockers(tmp_path)
    assert any("skip-registry" in x for x in b)
    assert any("FR-777" in x for x in b)
    assert not any("FR-999" in x or "FR-888" in x for x in b)


def test_skip_registry_linked_wi_id_over_spec_basename(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "spec.md").write_text("- **FR-001**: x\n", encoding="utf-8")
    (spec / "tasks.md").write_text(
        "### Task 1.1\n- **依赖**：无\n- **验收标准（AC）**：\n  1. ok\n",
        encoding="utf-8",
    )

    registry = tmp_path / "src" / "ai_sdlc" / "rules"
    registry.mkdir(parents=True)
    (registry / "agent-skip-registry.zh.md").write_text(
        "| 日期 | 发现阶段 | 跳过内容摘要 | 根因 | 框架强化建议 | wi_id | 状态 |\n"
        "|------|----------|--------------|------|--------------|-------|------|\n"
        "| 2026-03-26 | 执行 | z | A | 引入 FR-777 | linked-id | 已记录 |\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
        linked_wi_id="linked-id",
    )
    save_checkpoint(tmp_path, cp)

    b = collect_constraint_blockers(tmp_path)
    assert any("FR-777" in x for x in b)


def test_blocker_tasks_md_missing_task_acceptance(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "tasks.md").write_text(
        "### Task 1.1 — 示例\n- **依赖**：无\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    b = collect_constraint_blockers(tmp_path)
    assert any("BLOCKER" in x for x in b)
    assert any("SC-014" in x for x in b)
    assert any("1.1" in x for x in b)


def test_blocker_skip_registry_unmapped_to_spec_or_tasks(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "spec.md").write_text("- **FR-001**: x\n", encoding="utf-8")
    (spec / "tasks.md").write_text(
        "### Task 1.1\n- **依赖**：无\n- **验收标准（AC）**：\n  1. ok\n",
        encoding="utf-8",
    )

    registry = tmp_path / "src" / "ai_sdlc" / "rules"
    registry.mkdir(parents=True)
    (registry / "agent-skip-registry.zh.md").write_text(
        "| 日期 | 发现阶段 | 跳过内容摘要 | 根因 | 框架强化建议 | wi_id | 状态 |\n"
        "|------|----------|--------------|------|--------------|-------|------|\n"
        "| 2026-03-26 | 执行 | x | A | 引入 FR-999 并补 Task 9.9 | 001-wi | 已记录 |\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    b = collect_constraint_blockers(tmp_path)
    assert any("BLOCKER" in x for x in b)
    assert any("skip-registry" in x for x in b)


def test_pass_skip_registry_with_mapped_refs(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "spec.md").write_text("- **FR-001**: x\n", encoding="utf-8")
    (spec / "tasks.md").write_text(
        "### Task 1.1\n- **依赖**：无\n- **验收标准（AC）**：\n  1. ok\n",
        encoding="utf-8",
    )

    registry = tmp_path / "src" / "ai_sdlc" / "rules"
    registry.mkdir(parents=True)
    (registry / "agent-skip-registry.zh.md").write_text(
        "| 日期 | 发现阶段 | 跳过内容摘要 | 根因 | 框架强化建议 | wi_id | 状态 |\n"
        "|------|----------|--------------|------|--------------|-------|------|\n"
        "| 2026-03-26 | 执行 | x | A | 引入 FR-001 并补 Task 1.1 | 001-wi | 已记录 |\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="init",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    assert collect_constraint_blockers(tmp_path) == []


def test_framework_backlog_missing_required_fields_blocks(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    _write_framework_backlog(
        tmp_path,
        "- 现象: 发现框架缺陷\n"
        "- 触发场景: 用户要求登记\n"
        "- 影响范围: 规则与流程\n"
        "- 根因分类: B\n"
        "- 建议改动层级: rule / policy, workflow\n"
        "- prompt / context: 会话内发现偏离\n"
        "- rule / policy: pipeline.md 条款 17\n"
        "- middleware: 无\n"
        "- workflow: 需登记再继续\n"
        "- tool: ai-sdlc verify constraints\n"
        "- 风险等级: 中\n"
        "- 可验证成功标准: verify constraints 可识别结构问题\n"
        "- 是否需要回归测试补充: 是\n",
    )

    blockers = collect_constraint_blockers(tmp_path)
    assert any("framework-defect-backlog" in x for x in blockers)
    assert any("eval" in x for x in blockers)


def test_framework_backlog_well_formed_passes(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")

    _write_framework_backlog(
        tmp_path,
        "- 现象: 发现框架缺陷\n"
        "- 触发场景: 用户要求登记\n"
        "- 影响范围: 规则与流程\n"
        "- 根因分类: B\n"
        "- 建议改动层级: rule / policy, workflow\n"
        "- prompt / context: 会话内发现偏离\n"
        "- rule / policy: pipeline.md 条款 17\n"
        "- middleware: 无\n"
        "- workflow: 需登记再继续\n"
        "- tool: ai-sdlc verify constraints\n"
        "- eval: 结构化字段完整率\n"
        "- 风险等级: 中\n"
        "- 可验证成功标准: verify constraints 无 BLOCKER\n"
        "- 是否需要回归测试补充: 是\n",
    )

    assert collect_constraint_blockers(tmp_path) == []


def test_verify_constraints_blocks_misplaced_formal_artifact_under_superpowers(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    misplaced = tmp_path / "docs" / "superpowers" / "specs" / "2026-04-07-misplaced.md"
    misplaced.parent.mkdir(parents=True, exist_ok=True)
    misplaced.write_text(
        "# 功能规格：Misplaced\n\n"
        "**功能编号**：`sample-demo`\n"
        "**创建日期**：2026-04-07\n"
        "**状态**：草稿\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("misplaced formal artifact" in x for x in blockers)


def test_verify_constraints_blocks_missing_backlog_for_referenced_defect(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    spec_dir = tmp_path / "specs" / "117-formal-artifact-target-guard-baseline"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(
        "# 功能规格：Demo\n\n承接 `FD-2026-04-07-002`。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("breach_detected_but_not_logged" in x for x in blockers)


def _copy_release_contract_surfaces(root: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    paths = (
        "README.md",
        "USER_GUIDE.zh-CN.md",
        "packaging/offline/README.md",
        "packaging/install_online.ps1",
        "packaging/install_online.sh",
        "docs/v3-migration.zh-CN.md",
        "docs/框架自迭代开发与发布约定.md",
        "docs/pull-request-checklist.zh.md",
        ".github/workflows/release-build.yml",
        ".github/workflows/release-artifact-smoke.yml",
        ".github/workflows/windows-offline-smoke.yml",
    )
    for relative in paths:
        source = repo_root / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def test_release_docs_consistency_blocks_when_current_identity_is_missing(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _copy_release_contract_surfaces(tmp_path)
    (tmp_path / "README.md").write_text("# AI-SDLC\n", encoding="utf-8")

    blockers = collect_constraint_blockers(tmp_path)

    assert any("release docs consistency drift" in item for item in blockers)
    assert any("README.md" in item and "3.1.0" in item for item in blockers)


def test_release_docs_consistency_passes_when_current_surfaces_align(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _copy_release_contract_surfaces(tmp_path)

    blockers = collect_constraint_blockers(tmp_path)
    release_blockers = [
        item for item in blockers if "release docs consistency drift" in item
    ]

    assert release_blockers == []


def test_release_docs_consistency_blocks_windows_smoke_without_current_init(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _copy_release_contract_surfaces(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / "windows-offline-smoke.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "ai-sdlc init . --agent-target codex --shell powershell",
            "ai-sdlc status",
        ),
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any(
        "release docs consistency drift" in item and "windows-offline-smoke.yml" in item
        for item in blockers
    )


def test_beginner_guide_blocks_missing_current_sections(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    (tmp_path / "USER_GUIDE.zh-CN.md").write_text(
        "# AI-SDLC 1.0.4 中文用户指南\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("beginner guide CLI path missing" in item for item in blockers)


def test_beginner_guide_accepts_current_1_0_1_path(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _copy_release_contract_surfaces(tmp_path)

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("beginner guide" in item for item in blockers)


def test_beginner_guide_blocks_when_runtime_adapter_list_is_incomplete(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _copy_release_contract_surfaces(tmp_path)
    guide = tmp_path / "USER_GUIDE.zh-CN.md"
    guide.write_text(
        guide.read_text(encoding="utf-8").replace("Claude Code", "Claude"),
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any(
        "beginner guide CLI path missing" in item and "Claude Code" in item
        for item in blockers
    )


def test_beginner_guide_blocks_old_source_and_upgrade_paths(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _copy_release_contract_surfaces(tmp_path)
    guide = tmp_path / "USER_GUIDE.zh-CN.md"
    guide.write_text(
        guide.read_text(encoding="utf-8")
        + "\n## 老版本升级\n\n从源码运行：`uv sync` 后使用 `@main`。\n"
        + "https://github.com/SinclairPan/Ai_AutoSDLC/"
        "releases/download/v1.0.4/ai-sdlc-offline-1.0.4-linux-amd64.tar.gz\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any(
        "beginner guide CLI path contains out-of-scope content" in item
        and "老版本升级" in item
        and "从源码运行" in item
        and "releases/download/v1.0.4/" in item
        for item in blockers
    )


def test_readme_blocks_missing_codex_init_path(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# AI-SDLC 1.0.4\n\n## 快速开始\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("README CLI path missing" in item for item in blockers)


def test_readme_accepts_current_codex_init_path(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _copy_release_contract_surfaces(tmp_path)

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("README CLI path" in item for item in blockers)


def test_agent_instructions_block_old_manual_startup_path(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    template = tmp_path / "src" / "ai_sdlc" / "adapters" / "codex" / "AI-SDLC.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "- 初始化入口（普通用户先执行）：`ai-sdlc init .`\n"
        "- `init` 会自动执行必要检查与安全预演。\n"
        "- 排查入口（仅当 CLI 明确要求时执行）：`ai-sdlc adapter status`\n"
        "如果 `init` 已完成，不要再要求用户手动执行 `adapter status` 或 `run --dry-run`。\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text(
        "- 先检查接入真值：`ai-sdlc adapter status`\n"
        "- 启动入口（先执行）：`ai-sdlc run --dry-run`\n"
        "当用户输入需求时，优先引导并先执行上述启动入口。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("AGENTS.md CLI path regressed" in x for x in blockers)


def test_agent_instructions_do_not_block_custom_project_agents(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "# Custom project instructions\n\n"
        "This project keeps hand-written agent guidance.\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("AGENTS.md CLI path" in x for x in blockers)


def test_agent_instructions_block_missing_agents_in_framework_source(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    template = tmp_path / "src" / "ai_sdlc" / "adapters" / "codex" / "AI-SDLC.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "- 初始化入口（普通用户先执行）：`ai-sdlc init .`\n"
        "- `init` 会自动执行必要检查与安全预演。\n"
        "- 排查入口（仅当 CLI 明确要求时执行）：`ai-sdlc adapter status`\n"
        "如果 `init` 已完成，不要再要求用户手动执行 `adapter status` 或 `run --dry-run`。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("AGENTS.md CLI path missing" in x for x in blockers)


def test_agent_instructions_accept_current_init_first_path(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    template = tmp_path / "src" / "ai_sdlc" / "adapters" / "codex" / "AI-SDLC.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "- 初始化入口（普通用户先执行）：`ai-sdlc init .`\n"
        "- `init` 会自动执行必要检查与安全预演。\n"
        "- 排查入口（仅当 CLI 明确要求时执行）：`ai-sdlc adapter status`\n"
        "如果 `init` 已完成，不要再要求用户手动执行 `adapter status` 或 `run --dry-run`。\n"
        "后续 agent 或人工需要维护的代码，涉及认证、XHR/API 调用、payload 字段映射、"
        "加密、阶段流程时，必须补维护契约和 docstring。\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text(
        "- 初始化入口（普通用户先执行）：`ai-sdlc init .`\n"
        "- `init` 会自动执行必要检查与安全预演。\n"
        "- 排查入口（仅当 CLI 明确要求时执行）：`ai-sdlc adapter status`\n"
        "如果 `init` 已完成，不要再要求用户手动执行 `adapter status` 或 `run --dry-run`。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("AGENTS.md CLI path" in x for x in blockers)


def test_adapter_template_blocks_old_manual_startup_path(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    template = tmp_path / "src" / "ai_sdlc" / "adapters" / "codex" / "AI-SDLC.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "- 先检查接入真值：`ai-sdlc adapter status`\n"
        "- 启动入口（先执行）：`ai-sdlc run --dry-run`\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("adapter template CLI path regressed" in x for x in blockers)


def test_adapter_template_accepts_current_init_first_path(tmp_path: Path) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    template = tmp_path / "src" / "ai_sdlc" / "adapters" / "codex" / "AI-SDLC.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "- 初始化入口（普通用户先执行）：`ai-sdlc init .`\n"
        "- `init` 会自动执行必要检查与安全预演。\n"
        "- 排查入口（仅当 CLI 明确要求时执行）：`ai-sdlc adapter status`\n"
        "如果 `init` 已完成，不要再要求用户手动执行 `adapter status` 或 `run --dry-run`。\n"
        "后续 agent 或人工需要维护的代码，涉及认证、XHR/API 调用、payload 字段映射、"
        "加密、阶段流程时，必须补维护契约和 docstring。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("adapter template CLI path" in x for x in blockers)
    assert not any("adapter template comment policy" in x for x in blockers)


def test_adapter_template_blocks_missing_maintainability_comment_policy(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    template = tmp_path / "src" / "ai_sdlc" / "adapters" / "codex" / "AI-SDLC.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "- 初始化入口（普通用户先执行）：`ai-sdlc init .`\n"
        "- `init` 会自动执行必要检查与安全预演。\n"
        "- 排查入口（仅当 CLI 明确要求时执行）：`ai-sdlc adapter status`\n"
        "如果 `init` 已完成，不要再要求用户手动执行 `adapter status` 或 `run --dry-run`。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("adapter template comment policy missing" in x for x in blockers)


def test_reconcile_smoke_contract_blocks_when_workflow_is_not_synced(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_reconcile_smoke_contract_surfaces(
        tmp_path,
        include_workflow_markers=False,
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("reconcile smoke contract" in x for x in blockers)
    assert any("windows-offline-smoke.yml" in x for x in blockers)


def test_reconcile_smoke_contract_does_not_require_retired_run_message(
    tmp_path: Path,
) -> None:
    _write_reconcile_smoke_contract_surfaces(tmp_path)
    (tmp_path / "src" / "ai_sdlc" / "cli" / "run_cmd.py").write_text(
        "from ai_sdlc.core.loop_router import route_five_loops\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert not [item for item in blockers if "reconcile smoke contract" in item]


def test_verification_profile_docs_block_when_rules_surface_missing_profile(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_verification_profile_docs(tmp_path, include_rules_only=False)

    blockers = collect_constraint_blockers(tmp_path)
    assert any("verification profile" in x for x in blockers)
    assert any("rules-only" in x for x in blockers)


def test_verification_profile_docs_block_when_checklist_missing_clean_gate(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_verification_profile_docs(tmp_path, include_checklist_clean_gate=False)

    blockers = collect_constraint_blockers(tmp_path)
    assert any("verification profile" in x for x in blockers)
    assert any("validate_public_release_identity.py" in x for x in blockers)


def test_verification_profile_docs_block_when_truth_only_profile_missing(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_verification_profile_docs(tmp_path)

    verification_path = tmp_path / "src" / "ai_sdlc" / "rules" / "verification.md"
    verification_path.write_text(
        verification_path.read_text(encoding="utf-8").replace(
            "truth-only", "truth-profile"
        ),
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)
    assert any("verification profile" in x for x in blockers)
    assert any("truth-only" in x for x in blockers)


def test_verification_profile_docs_pass_when_both_surfaces_complete(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_verification_profile_docs(tmp_path)

    assert collect_constraint_blockers(tmp_path) == []


def test_framework_rule_guards_ignore_user_project_checklist_only(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "pull-request-checklist.zh.md").write_text(
        "# 用户项目合并清单\n\n- 按业务项目要求检查。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("verification profile" in x for x in blockers)
    assert not any("feature regression guard" in x for x in blockers)


def test_feature_regression_guard_blocks_when_review_surface_missing_old_capability_terms(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_verification_profile_docs(tmp_path)
    review_path = tmp_path / "src" / "ai_sdlc" / "rules" / "code-review.md"
    review_path.write_text(
        "# 代码审查协议\n\n只检查新功能 happy path。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("feature regression guard" in x for x in blockers)
    assert any("既有能力不退化" in x for x in blockers)
    assert any("回归测试" in x for x in blockers)


def test_feature_regression_guard_accepts_complete_rule_surfaces(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_verification_profile_docs(tmp_path)

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("feature regression guard" in x for x in blockers)


def test_doc_first_rule_surfaces_block_when_pipeline_terms_missing(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_doc_first_rule_surfaces(tmp_path, include_pipeline_terms=False)

    blockers = collect_constraint_blockers(tmp_path)
    assert any("doc-first" in x for x in blockers)
    assert any("pipeline.md" in x for x in blockers)


def test_frontend_solution_confirmation_instruction_blocks_missing_pipeline_guard(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    rules_dir = tmp_path / "src" / "ai_sdlc" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "pipeline.md").write_text(
        "# 流水线总控规则\n\n前端需求可以按普通需求直接进入实现。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any("frontend solution confirmation instruction" in x for x in blockers)
    assert any("项目已有技术栈" in x for x in blockers)
    assert any("一个推荐方案" in x for x in blockers)
    assert any("至少一个可选 / 自定义方案" in x for x in blockers)
    assert any("规范正文" in x for x in blockers)
    assert any("Applicable Rules" in x for x in blockers)


def test_frontend_solution_confirmation_instruction_accepts_required_pipeline_guard(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    rules_dir = tmp_path / "src" / "ai_sdlc" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "pipeline.md").write_text(
        "# 流水线总控规则\n\n"
        "前端需求进入实现前必须基于项目已有技术栈给出一个推荐方案，"
        "并同时提供至少一个可选 / 自定义方案，等待用户明确确认。"
        "确认前不得进入 execute、不得生成前端实现代码。"
        "输出必须区分规范正文、可选建议、已经落地。"
        "通用规则不得硬编码框架、组件库、provider 或 style pack。"
        "正常路径直接使用 Applicable Rules，"
        "不要求用户手动执行 `rules show` 或 `stage show`。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert not any("frontend solution confirmation instruction" in x for x in blockers)


def test_frontend_solution_confirmation_instruction_blocks_fixed_stack_defaults(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    rules_dir = tmp_path / "src" / "ai_sdlc" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "pipeline.md").write_text(
        "# 流水线总控规则\n\n"
        "前端需求进入实现前必须基于项目已有技术栈给出一个推荐方案，"
        "并同时提供至少一个可选 / 自定义方案，等待用户明确确认。"
        "确认前不得进入 execute、不得生成前端实现代码。"
        "输出必须区分规范正文、可选建议、已经落地。"
        "通用规则不得硬编码框架、组件库、provider 或 style pack。"
        "正常路径直接使用 Applicable Rules，"
        "不要求用户手动执行 `rules show` 或 `stage show`。"
        "但这里又固定 provider_id=public-primevue。\n",
        encoding="utf-8",
    )

    blockers = collect_constraint_blockers(tmp_path)

    assert any(
        "stale default tooling" in x and "provider_id=public-primevue" in x
        for x in blockers
    )


def test_doc_first_rule_surfaces_block_when_doc_first_task_targets_code(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_doc_first_rule_surfaces(tmp_path)

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "tasks.md").write_text(
        "### Task 6.44 — 仅文档：冻结需求\n"
        "- **依赖**：Task 6.43\n"
        "- **验收标准（AC）**：\n"
        "  1. 先更新 specs\n"
        "- **产物**：`src/ai_sdlc/core/verify_constraints.py`\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="design",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    blockers = collect_constraint_blockers(tmp_path)
    assert any("doc-first task" in x for x in blockers)
    assert any("Task 6.44" in x for x in blockers)


def test_doc_first_rule_surfaces_pass_with_consistent_terms_and_docs_scope(
    tmp_path: Path,
) -> None:
    mem = tmp_path / ".ai-sdlc" / "memory"
    mem.mkdir(parents=True)
    (mem / "constitution.md").write_text("# C\n", encoding="utf-8")
    _write_doc_first_rule_surfaces(tmp_path)

    spec = tmp_path / "specs" / "001-wi"
    spec.mkdir(parents=True)
    (spec / "tasks.md").write_text(
        "### Task 6.44 — 先 spec-plan-tasks 后实现\n"
        "- **依赖**：Task 6.43\n"
        "- **验收标准（AC）**：\n"
        "  1. 先更新 specs\n"
        "- **产物**：`specs/001-wi/tasks.md`、`src/ai_sdlc/rules/pipeline.md`\n",
        encoding="utf-8",
    )

    cp = Checkpoint(
        current_stage="design",
        feature=FeatureInfo(
            id="001",
            spec_dir="specs/001-wi",
            design_branch="d",
            feature_branch="f",
            current_branch="main",
        ),
    )
    save_checkpoint(tmp_path, cp)

    assert collect_constraint_blockers(tmp_path) == []


def test_build_verification_gate_context_degrades_to_advisory_when_governance_is_incomplete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def _fake_governance_bundle(report, **kwargs):
        return {
            "audit_summary": {"audit_status": "inconclusive"},
            "gate_decision_payload": {
                "decision_subject": "verify:/tmp/project",
                "decision_result": "advisory",
                "confidence": "high",
                "evidence_refs": ["evd_0123456789abcdef0123456789abcdef"],
                "source_closure_status": "incomplete",
                "observer_version": "v1",
                "policy": "default",
                "profile": "self_hosting",
                "mode": "lite",
                "generated_at": "2026-03-30T00:00:00Z",
            },
            "advisories": ("observer bundle incomplete",),
        }

    monkeypatch.setattr(
        "ai_sdlc.core.verify_constraints.build_verification_governance_bundle",
        _fake_governance_bundle,
    )

    context = build_verification_gate_context(tmp_path)

    assert context["constraint_blockers"] == ()
    assert context["coverage_gaps"] == ()
    assert context["verification_governance"]["gate_decision_payload"][
        "decision_result"
    ] == ("advisory")
    assert context["verification_governance"]["advisories"] == (
        "observer bundle incomplete",
    )


def test_collect_constraint_blockers_includes_active_work_item_branch_lifecycle_drift(
    tmp_path: Path,
) -> None:
    _write_branch_lifecycle_fixture(tmp_path)
    _create_branch_ahead_of_main(tmp_path, "codex/001-verify-drift")

    blockers = collect_constraint_blockers(tmp_path)

    assert any("branch lifecycle" in item.lower() for item in blockers)
    assert any("codex/001-verify-drift" in item for item in blockers)


def test_collect_constraint_blockers_does_not_escalate_archived_branch_lifecycle(
    tmp_path: Path,
) -> None:
    _write_branch_lifecycle_fixture(
        tmp_path, branch_disposition_status="archived(non-mainline evidence)"
    )
    _create_branch_ahead_of_main(tmp_path, "archive/001-verify-archived")

    blockers = collect_constraint_blockers(tmp_path)

    assert all("branch lifecycle" not in item.lower() for item in blockers)


def test_collect_constraint_blockers_ignores_unrelated_historical_branch_lifecycle(
    tmp_path: Path,
) -> None:
    _write_branch_lifecycle_fixture(tmp_path)
    _create_branch_ahead_of_main(tmp_path, "codex/999-legacy-branch")

    blockers = collect_constraint_blockers(tmp_path)

    assert all("branch lifecycle" not in item.lower() for item in blockers)
