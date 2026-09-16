"""Tests for the deterministic implementation loop runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ai_sdlc.core.implementation_loop as implementation_loop_module
from ai_sdlc.cli.loop_review_cmd import (
    ReviewInputGuardError,
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.core.design_contract_loop import (
    DesignContractCheckOptions,
    DesignContractCloseOptions,
    check_design_contract_loop,
    close_design_contract_loop,
)
from ai_sdlc.core.implementation_loop import (
    CURRENT_IMPLEMENTATION_PATH,
    ImplementationCloseOptions,
    ImplementationCommandResult,
    ImplementationRecordOptions,
    ImplementationStartOptions,
    ImplementationVerifyOptions,
    close_implementation_loop,
    record_implementation_progress,
    start_implementation_loop,
    verify_implementation_task,
)
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.requirement_loop import (
    RequirementFreezeOptions,
    RequirementStartOptions,
    freeze_requirement_loop,
    start_requirement_loop,
)
from ai_sdlc.core.review_kernel import ReviewInput


@pytest.mark.parametrize(
    "overrides",
    [
        {"argv": (sys.executable, "-c", "raise RuntimeError('must not run')")},
        {"cwd": "nested"},
        {"timeout_seconds": 1.0},
        {"command_options_explicit": True},
    ],
)
def test_counterexample_plan_cannot_override_frozen_execution(tmp_path, overrides):
    options = dict(
        root=tmp_path, task_id="T01", cwd=".", argv=(), counterexample_plan="plan.json"
    )
    result = verify_implementation_task(
        ImplementationVerifyOptions(**(options | overrides))
    )
    assert result.status == "blocked"
    assert "cannot be combined" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


@pytest.mark.parametrize(
    "overrides",
    [
        ["--cwd", "."],
        ["--timeout-seconds", "300"],
        ["--", "python", "-c", "print('no')"],
    ],
)
def test_counterexample_cli_rejects_even_explicit_default_overrides(
    tmp_path, monkeypatch, overrides
):
    from typer.testing import CliRunner

    from ai_sdlc.cli import loop_cmd

    monkeypatch.setattr(loop_cmd, "_run_project_writer_adapter", lambda **kwargs: None)
    monkeypatch.setattr(loop_cmd, "_project_root_or_exit", lambda **kwargs: tmp_path)
    result = CliRunner().invoke(
        loop_cmd.implementation_app,
        [
            "verify",
            "--task-id",
            "T01",
            "--counterexample-plan",
            "plan.json",
            "--json",
            *overrides,
        ],
    )
    assert result.exit_code == 1, result.output
    assert "cannot be combined" in json.loads(result.output)["blocker"]
    assert not (tmp_path / ".ai-sdlc").exists()


def test_counterexample_new_plan_cannot_reset_original_limits(tmp_path):
    from ai_sdlc.core import counterexample_execution as execution
    from ai_sdlc.core.counterexample_models import counterexample_digest
    from tests.unit.test_counterexample_execution import execution_case

    root, _, plan = execution_case.__wrapped__(tmp_path)
    folder, _ = execution._plan_history(root, plan)
    execution._write_json(root, folder / f"{counterexample_digest(plan)}.json", plan)
    for replacement in (
        {"max_execution_attempts": plan.max_execution_attempts + 1},
        {"required_reserve_seconds": plan.required_reserve_seconds - 1},
        {"protected_paths": ("different.py",)},
        {"v0_digest": "e" * 64},
    ):
        with pytest.raises(ValueError, match="original-plan-scope-or-budget"):
            execution._plan_history(root, plan.model_copy(update=replacement))
    with pytest.raises(ValueError, match="reinforcement-batch-exhausted"):
        execution._plan_history(root, plan.model_copy(update={"v1_digest": "e" * 64}))
    assert execution._plan_history(root, plan)[1] == [plan]


@pytest.mark.parametrize(
    ("scope", "path", "allowed"),
    [
        ("src/*.py", "src/save.py", True),
        ("src/*.py", "src/private/save.py", False),
        ("src/**/save.py", "src/save.py", True),
        ("src/**/save.py", "src/deep/nested/save.py", True),
        ("src/storage", "src/storage/save.py", True),
        ("src/storage", "src/storage-other/save.py", False),
    ],
)
def test_counterexample_repair_scope_preserves_original_directory_boundaries(
    scope, path, allowed
):
    from types import SimpleNamespace

    from ai_sdlc.core.counterexample_execution import _original_task_allows

    original = SimpleNamespace(task_scopes={"T01": [scope]}, declared_scope=["**"])
    assert _original_task_allows(original, "T01", path) is allowed


def test_counterexample_repair_proof_requires_real_original_scope_code_delta(tmp_path):
    from types import SimpleNamespace

    from ai_sdlc.core import counterexample_execution as execution
    from ai_sdlc.core.counterexample_models import ArtifactRef
    from tests.unit.test_counterexample_models import make_plan

    plan = make_plan()
    initial_ref = execution._write_json(
        tmp_path, tmp_path / "initial.json", {"schema_version": 1, "files": []}
    )
    plan = plan.model_copy(
        update={
            "steps": tuple(
                step.model_copy(
                    update={
                        "binding": step.binding.model_copy(
                            update={
                                "resources": tuple(
                                    resource.model_copy(
                                        update={"initial_state": initial_ref}
                                    )
                                    for resource in step.binding.resources
                                )
                            }
                        )
                    }
                )
                for step in plan.steps
            )
        }
    )
    original = SimpleNamespace(
        task_scopes={plan.task_id: ["src/save.py"]}, declared_scope=[]
    )
    before_bytes, after_bytes = b"COMMIT = False\n", b"COMMIT = True\n"
    (tmp_path / "old.py").write_bytes(before_bytes)
    (tmp_path / "new.py").write_bytes(after_bytes)
    old_content = execution._ref(tmp_path, tmp_path / "old.py")
    new_content = execution._ref(tmp_path, tmp_path / "new.py")
    old_entry = {
        "path": "src/save.py",
        "sha256": old_content.sha256,
        "mode": "100644",
        "content_ref": old_content.model_dump(),
    }
    new_entry = {
        **old_entry,
        "sha256": new_content.sha256,
        "content_ref": new_content.model_dump(),
    }
    before_ref = execution._write_json(
        tmp_path, tmp_path / "before.json", {"files": [old_entry]}
    )
    after_ref = execution._write_json(
        tmp_path, tmp_path / "after.json", {"files": [new_entry]}
    )
    current = next(subject for subject in plan.subjects if subject.role == "current")
    old_plan = plan.model_copy(
        update={"subjects": (current.model_copy(update={"snapshot": before_ref}),)}
    )
    new_plan = plan.model_copy(
        update={
            "candidate_digest": "f" * 64,
            "subjects": (current.model_copy(update={"snapshot": after_ref}),),
        }
    )
    failed = SimpleNamespace(status="FAIL", evidence_refs=(before_ref,))
    passed = SimpleNamespace(status="PASS", evidence_refs=(after_ref,))
    previous = SimpleNamespace(
        plan=old_plan, assessment=SimpleNamespace(current_result=failed)
    )
    assessment = SimpleNamespace(current_result=passed)
    record_ref = ArtifactRef(path="record.json", sha256="a" * 64)
    new_plan_ref = ArtifactRef(path="plan.json", sha256="b" * 64)
    proof = execution._repair_proof_payload(
        tmp_path, original, record_ref, previous, new_plan_ref, new_plan, assessment
    )
    assert proof["changes"] == [
        {"path": "src/save.py", "before": old_entry, "after": new_entry}
    ]
    witness = new_plan.witnesses[0]
    with pytest.raises(ValueError, match="original-defect-path-not-retested"):
        execution._repair_proof_payload(
            tmp_path,
            original,
            record_ref,
            previous,
            new_plan_ref,
            new_plan.model_copy(
                update={
                    "witnesses": (
                        witness.model_copy(
                            update={
                                "input_ref": ArtifactRef(
                                    path="different-input.json", sha256="c" * 64
                                )
                            }
                        ),
                    )
                }
            ),
            assessment,
        )
    with pytest.raises(ValueError, match="outside-original-task"):
        execution._repair_proof_payload(
            tmp_path,
            original,
            record_ref,
            previous,
            new_plan_ref,
            new_plan.model_copy(update={"protected_paths": ("src/save.py",)}),
            assessment,
        )
    with pytest.raises(ValueError, match="has-no-code-change"):
        execution._repair_proof_payload(
            tmp_path,
            original,
            record_ref,
            previous,
            new_plan_ref,
            new_plan.model_copy(update={"subjects": old_plan.subjects}),
            assessment,
        )
    previous.assessment.current_result = passed
    with pytest.raises(ValueError, match="actual-business-before-and-after"):
        execution._repair_proof_payload(
            tmp_path, original, record_ref, previous, new_plan_ref, new_plan, assessment
        )


def test_non_git_implementation_lock_dir_is_user_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        implementation_loop_module.tempfile,
        "gettempdir",
        lambda: str(tmp_path / "shared-temp"),
    )
    monkeypatch.setattr(
        implementation_loop_module.os,
        "getuid",
        lambda: 1001,
        raising=False,
    )
    first = implementation_loop_module._implementation_lock_dir(project)
    monkeypatch.setattr(implementation_loop_module.os, "getuid", lambda: 1002)
    second = implementation_loop_module._implementation_lock_dir(project)

    assert first.name == "ai-sdlc-loop-locks-1001"
    assert second.name == "ai-sdlc-loop-locks-1002"
    assert first != second


@pytest.mark.parametrize(
    "acceptance_label,verification_label",
    [
        ("- **验收标准**：", "- **验证**："),
        ("- acceptance criteria:", "- verification:"),
        ("- **验收标准（AC）**：", "- **Verification Commands**:"),
    ],
)
def test_task_labels_ignore_prose_and_file_names_across_consumers(
    tmp_path: Path, acceptance_label: str, verification_label: str
) -> None:
    from ai_sdlc.core.design_contract_store import _verification_task_entry

    work_item = _write_ready_work_item(tmp_path)
    tasks = work_item / "tasks.md"
    content = "\n".join(
        [
            "# 任务分解：Implementation Demo",
            "### Task 1.1 Verification and acceptance report",
            "正文讨论验证合同和 acceptance，不是字段。",
            "- **任务编号**：T11",
            "- **优先级**：P0",
            "- **文件**：src/verification.py",
            acceptance_label,
            "  1. FR-IMPL-001 and SC-IMPL-001 are covered.",
            verification_label + " `python verify_result.py`",
            "- notes: This is not an acceptance item.",
            "  - This belongs to notes, not verification.",
        ]
    )
    tasks.write_text(content, encoding="utf-8")
    _close_design_contract_for_work_item(tmp_path, work_item)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path, work_item="specs/demo-implementation-loop", loop_id="impl-labels"
        )
    )
    assert result.status == "ready"
    path = tmp_path / ".ai-sdlc/loops/implementation/impl-labels/implementation-tasks.json"
    original = path.read_bytes()
    item = json.loads(original)["items"][0]
    assert item["files"] == ["src/verification.py"]
    assert item["acceptance"] == ["1. FR-IMPL-001 and SC-IMPL-001 are covered."]
    assert item["verification_hints"] == ["python verify_result.py"]
    assert _verification_task_entry(content.encode(), "T11", "T11/acceptance/1") == (
        b"1. FR-IMPL-001 and SC-IMPL-001 are covered."
    )
    # 已冻结任务作为原快照读取，标签修复不回填旧工件。
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "label,body,expected",
    [
        ("- **验证**：", "`python check.py`", ["python check.py"]),
        ("* verification:", "\n  - python check.py\n  - python legal.py", ["python check.py", "python legal.py"]),
        ("**Verification**:", "python check.py", ["python check.py"]),
        ("验证：", "\n  1. python check.py", ["1. python check.py"]),
    ],
)
def test_task_verification_labels_keep_legal_formats_and_field_boundaries(
    label: str, body: str, expected: list[str]
) -> None:
    section = "\n".join(
        [
            "### Task 1 普通任务",
            label + body,
            "- files: src/verification.py",
            "  - src/extra.py",
            "### Notes",
            "- not-a-command",
        ]
    )
    assert implementation_loop_module._task_list_after_label(
        section, "验证", "verification"
    ) == expected


def test_start_implementation_loop_writes_artifacts(tmp_path: Path) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-001",
        )
    )

    assert result.status == "ready"
    assert result.loop_status == "running"
    assert result.work_item_id == "demo-implementation-loop"
    assert result.required_task_count == 1
    assert result.done_count == 0
    assert result.next_guidance.requires_model is False
    assert result.next_guidance.writes_artifacts is True
    assert result.next_guidance.writes_code is False
    assert result.implementation is not None
    assert result.implementation.report_path.endswith(
        ".ai-sdlc/loops/implementation/impl-001/implementation-report.json"
    )

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "implementation" / "impl-001"
    assert (loop_dir / "loop-run.json").is_file()
    assert (loop_dir / "implementation-input.json").is_file()
    assert (loop_dir / "implementation-tasks.json").is_file()
    assert (loop_dir / "implementation-progress.json").is_file()
    assert (loop_dir / "verification-evidence.json").is_file()
    assert (loop_dir / "implementation-report.json").is_file()
    assert (loop_dir / "implementation-report.md").is_file()
    assert (tmp_path / CURRENT_IMPLEMENTATION_PATH).is_file()

    tasks = json.loads((loop_dir / "implementation-tasks.json").read_text("utf-8"))
    assert tasks["artifact_kind"] == "implementation-tasks"
    assert tasks["created_by"] == "ai-sdlc"
    assert [item["task_id"] for item in tasks["items"]] == ["T11", "T21"]
    assert [item["required"] for item in tasks["items"]] == [True, False]


def test_start_implementation_loop_preserves_four_digit_task_ids(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8")
        + "\n".join(
            [
                "",
                "### Task 10.1 Build bounded storage",
                "",
                "- task_id: T1001",
                "- priority: P1",
                "- depends: T903",
                "- scope: src/ai_sdlc/core/storage.py",
                "- acceptance: Storage remains bounded.",
                "- verify: uv run pytest tests/unit/test_implementation_loop.py -q",
                "- **验收标准**：Storage remains bounded.",
                "- **验证**：`uv run pytest tests/unit/test_implementation_loop.py -q`",
                "",
                "### Task 11.1 Run final acceptance",
                "",
                "- task_id: T1101",
                "- priority: P0",
                "- depends: T701, T1002",
                "- scope: tests/unit/test_implementation_loop.py",
                "- acceptance: All acceptance evidence is present.",
                "- verify: uv run pytest tests/unit/test_implementation_loop.py -q",
                "- **验收标准**：All acceptance evidence is present.",
                "- **验证**：`uv run pytest tests/unit/test_implementation_loop.py -q`",
            ]
        ),
        encoding="utf-8",
    )
    _close_design_contract_for_work_item(tmp_path, work_item)

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-four-digit-task-ids",
        )
    )

    assert result.status == "ready"
    tasks = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "implementation"
            / "impl-four-digit-task-ids"
            / "implementation-tasks.json"
        ).read_text("utf-8")
    )
    assert [item["task_id"] for item in tasks["items"]] == [
        "T11",
        "T21",
        "T1001",
        "T1101",
    ]


def test_start_implementation_loop_prefers_canonical_task_scope(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_text = tasks_path.read_text(encoding="utf-8")
    tasks_text = tasks_text.replace(
        "- **任务编号**：T11\n",
        "- task_id: T11\n"
        "- status: doing\n"
        "- scope:\n"
        "  - src/runtime/*.py\n"
        "  - tests/runtime/*.py\n"
        "- **任务编号**：T11\n",
    ).replace(
        "- **任务编号**：T21\n",
        "- task_id: T21\n"
        "- status: needs-review\n"
        "- scope: docs/runtime/*.md\n"
        "- **任务编号**：T21\n",
    )
    tasks_path.write_text(tasks_text, encoding="utf-8")
    _close_design_contract_for_work_item(tmp_path, work_item)

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-canonical-task-scope",
        )
    )

    assert result.status == "ready"
    loop_dir = (
        tmp_path / ".ai-sdlc" / "loops" / "implementation" / "impl-canonical-task-scope"
    )
    impl_input = json.loads(
        (loop_dir / "implementation-input.json").read_text(encoding="utf-8")
    )
    tasks = json.loads(
        (loop_dir / "implementation-tasks.json").read_text(encoding="utf-8")
    )
    assert impl_input["declared_scope"] == [
        "src/runtime/*.py",
        "tests/runtime/*.py",
        "docs/runtime/*.md",
    ]
    assert impl_input["task_scopes"] == {
        "T11": ["src/runtime/*.py", "tests/runtime/*.py"],
        "T21": ["docs/runtime/*.md"],
    }
    assert [item["files"] for item in tasks["items"]] == [
        ["src/runtime/*.py", "tests/runtime/*.py"],
        ["docs/runtime/*.md"],
    ]


def test_direct_formal_work_item_does_not_enable_blocking_quality_profile(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    spec_path = work_item / "spec.md"
    spec_text = spec_path.read_text(encoding="utf-8")
    spec_path.write_text(
        spec_text.replace("---\n", "---\nwork_type: new_requirement\n", 1),
        encoding="utf-8",
    )
    _close_design_contract_for_work_item(tmp_path, work_item)

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-direct-formal-quality-profile",
        )
    )

    assert result.status == "ready"
    impl_input = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "implementation"
            / "impl-direct-formal-quality-profile"
            / "implementation-input.json"
        ).read_text("utf-8")
    )
    assert impl_input["work_type"] == "new_requirement"
    assert impl_input["quality_profiles"] == []


def test_direct_formal_work_item_rejects_invalid_work_type_metadata(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    spec_path = work_item / "spec.md"
    spec_text = spec_path.read_text(encoding="utf-8")
    spec_path.write_text(
        spec_text.replace("---\n", "---\nwork_type: typo_requirement\n", 1),
        encoding="utf-8",
    )
    _close_design_contract_for_work_item(tmp_path, work_item)

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-invalid-formal-work-type",
        )
    )

    assert result.status == "blocked"
    assert "work_type" in result.blocker
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "implementation"
        / "impl-invalid-formal-work-type"
    ).exists()


def test_start_implementation_loop_dry_run_does_not_write(tmp_path: Path) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-dry-run",
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.required_task_count == 1
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "implementation" / "impl-dry-run"
    ).exists()


def test_start_implementation_loop_ignores_legacy_lean_policy(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    policy = tmp_path / ".ai-sdlc" / "project" / "config" / "loop-policy.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("remote_model_policy: strict\n", encoding="utf-8")

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-malformed-policy",
        )
    )

    assert result.status == "ready"
    assert (
        tmp_path / ".ai-sdlc" / "loops" / "implementation" / "impl-malformed-policy"
    ).is_dir()


def test_start_implementation_loop_blocks_unclosed_design_contract(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _start_frozen_requirement(tmp_path, work_item)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            requirement_loop_id="req-demo-implementation-loop",
            loop_id="dc-demo-implementation-loop",
        )
    )

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-unclosed-design",
        )
    )

    assert result.status == "blocked"
    assert "must be closed" in result.blocker
    assert result.next_action == (
        "Run ai-sdlc loop review --type design-contract "
        "--loop-id dc-demo-implementation-loop."
    )


def test_start_implementation_loop_blocks_cross_loop_design_artifacts(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _start_frozen_requirement(tmp_path, work_item)
    for loop_id in ("dc-target-a", "dc-source-b"):
        check = check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-implementation-loop",
                requirement_loop_id="req-demo-implementation-loop",
                loop_id=loop_id,
            )
        )
        assert check.status == "ready"
        close = close_design_contract_loop(
            DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
        )
        assert close.status == "ready"
    design_root = tmp_path / ".ai-sdlc" / "loops" / "design-contract"
    target = design_root / "dc-target-a"
    source = design_root / "dc-source-b"

    for index, artifact_name in enumerate(
        ("design-contract-report.json", "design-contract-close.json"),
        start=1,
    ):
        original = (target / artifact_name).read_text(encoding="utf-8")
        (target / artifact_name).write_text(
            (source / artifact_name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        result = start_implementation_loop(
            ImplementationStartOptions(
                root=tmp_path,
                work_item="specs/demo-implementation-loop",
                design_contract_loop_id="dc-target-a",
                loop_id=f"impl-cross-loop-{index}",
            )
        )
        assert result.status == "blocked"
        assert "identity" in result.blocker.lower()
        (target / artifact_name).write_text(original, encoding="utf-8")


def test_record_implementation_progress_updates_evidence_and_report(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-record",
        )
    )

    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-record",
            task_id="T11",
            status="done",
            evidence=("src/ai_sdlc/core/implementation_loop.py",),
            verification=("uv run pytest tests/unit/test_implementation_loop.py -q",),
            note="核心 runtime 已验证",
        )
    )

    result = _record_successful_quality_result(tmp_path, "impl-record", "T11")
    assert result.status == "ready"
    assert result.loop_status == "needs_review"
    assert result.done_count == 1
    assert result.evidence_count == 3
    assert result.next_action == (
        "Run ai-sdlc loop review --type implementation --loop-id impl-record."
    )
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "implementation" / "impl-record"
    progress = json.loads(
        (loop_dir / "implementation-progress.json").read_text("utf-8")
    )
    task = next(item for item in progress["tasks"] if item["task_id"] == "T11")
    assert task["status"] == "done"
    assert task["evidence"] == ["src/ai_sdlc/core/implementation_loop.py"]
    assert task["verification_commands"] == [
        "uv run pytest tests/unit/test_implementation_loop.py -q"
    ]
    assert task["quality_results"][0]["status"] == "passed"
    assert task["quality_results"][0]["argv"] == [
        sys.executable,
        "-c",
        "print('verified')",
    ]


def test_verify_implementation_task_persists_nonzero_result_without_promotion(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-failed-quality",
        )
    )
    _ensure_git_repository(tmp_path)

    result = verify_implementation_task(
        ImplementationVerifyOptions(
            root=tmp_path,
            loop_id="impl-failed-quality",
            task_id="T11",
            cwd=".",
            argv=(sys.executable, "-c", "raise SystemExit(7)"),
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "running"
    progress = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "implementation"
            / "impl-failed-quality"
            / "implementation-progress.json"
        ).read_text(encoding="utf-8")
    )
    quality = progress["tasks"][0]["quality_results"][0]
    assert quality["status"] == "failed"
    assert quality["exit_code"] == 7


def test_verify_implementation_task_rejects_source_mutation(tmp_path: Path) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-mutating-quality",
        )
    )
    _ensure_git_repository(tmp_path)

    result = verify_implementation_task(
        ImplementationVerifyOptions(
            root=tmp_path,
            loop_id="impl-mutating-quality",
            task_id="T11",
            cwd=".",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('changed.py').write_text('changed')",
            ),
        )
    )

    assert result.status == "needs_fix"
    assert result.blocker == "Verification status is source_changed."


def test_successful_verification_becomes_stale_after_source_change(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-stale-quality",
        )
    )
    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-stale-quality",
            task_id="T11",
            status="done",
            verification=("pytest -q",),
        )
    )
    _record_successful_quality_result(tmp_path, "impl-stale-quality", "T11")
    spec_path = work_item / "spec.md"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8") + "\nUnreviewed source change.\n",
        encoding="utf-8",
    )

    result = close_implementation_loop(
        ImplementationCloseOptions(
            root=tmp_path,
            loop_id="impl-stale-quality",
            yes=True,
        )
    )

    assert result.status == "needs_fix"
    assert result.blocker == ("T11 has no successful verification for current source.")


def test_slimming_advice_never_blocks_implementation_close(tmp_path: Path) -> None:
    work_item = _write_ready_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "src/ai_sdlc/core/implementation_loop.py",
            "src/ai_sdlc/core/*.py",
        ),
        encoding="utf-8",
    )
    source = tmp_path / "src" / "ai_sdlc" / "core" / "implementation_loop.py"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join(f"line_{index} = {index}" for index in range(510)))
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-advisory-only",
        )
    )

    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-advisory-only",
            task_id="T11",
            status="done",
            verification=("pytest -q",),
        )
    )

    result = _record_successful_quality_result(
        tmp_path,
        "impl-advisory-only",
        "T11",
    )
    assert result.loop_status == "needs_review"
    assert result.next_action == (
        "Run ai-sdlc loop review --type implementation --loop-id impl-advisory-only."
    )
    assert any("510 lines" in advice for advice in result.advisories)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable")
def test_slimming_advice_skips_nested_external_symlinks(tmp_path: Path) -> None:
    work_item = _write_ready_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "src/ai_sdlc/core/implementation_loop.py",
            "src/ai_sdlc/core",
        ),
        encoding="utf-8",
    )
    source_dir = tmp_path / "src" / "ai_sdlc" / "core"
    source_dir.mkdir(parents=True)
    external = tmp_path.with_name(f"{tmp_path.name}-external.py")
    external.write_text(
        "\n".join(f"secret_line_{index} = {index}" for index in range(510)),
        encoding="utf-8",
    )
    (source_dir / "external.py").symlink_to(external)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-skip-external-symlink",
        )
    )

    try:
        result = record_implementation_progress(
            ImplementationRecordOptions(
                root=tmp_path,
                loop_id="impl-skip-external-symlink",
                task_id="T11",
                status="done",
                verification=("pytest -q",),
            )
        )
    finally:
        external.unlink(missing_ok=True)

    assert not any("external.py" in advice for advice in result.advisories)
    assert not any("secret_line" in advice for advice in result.advisories)


def test_record_implementation_progress_blocks_unknown_task(tmp_path: Path) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-unknown-task",
        )
    )

    result = record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-unknown-task",
            task_id="T99",
            status="done",
            evidence=("x.py",),
        )
    )

    assert result.status == "blocked"
    assert result.blocker == "Unknown implementation task id: T99."


def test_record_implementation_progress_blocks_done_without_evidence(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-no-evidence",
        )
    )

    result = record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-no-evidence",
            task_id="T11",
            status="done",
        )
    )

    assert result.status == "blocked"
    assert "must include --evidence or --verification" in result.blocker


def test_record_implementation_progress_blocked_state_has_no_fake_command(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-task-blocked",
        )
    )

    result = record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-task-blocked",
            task_id="T11",
            status="blocked",
            note="等待用户提供验证环境。",
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert (
        result.next_action
        == "Resolve implementation blocker for T11, then record progress."
    )
    assert result.next_guidance.command == ""


def test_close_implementation_loop_blocks_incomplete_required_tasks(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-incomplete",
        )
    )

    result = close_implementation_loop(
        ImplementationCloseOptions(root=tmp_path, loop_id="impl-incomplete", yes=True)
    )

    assert result.status == "needs_fix"
    assert result.blocker == "T11 is not done."
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "implementation"
        / "impl-incomplete"
        / "implementation-close.json"
    ).exists()


def test_close_implementation_loop_rejects_replaced_loop_identity(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    for loop_id in ("impl-reviewed", "impl-unreviewed"):
        start_implementation_loop(
            ImplementationStartOptions(
                root=tmp_path,
                work_item="specs/demo-implementation-loop",
                loop_id=loop_id,
            )
        )
    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-unreviewed",
            task_id="T11",
            status="done",
            verification=("uv run pytest tests/unit/test_implementation_loop.py -q",),
        )
    )
    reviewed_run = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "implementation"
        / "impl-reviewed"
        / "loop-run.json"
    )
    unreviewed_run = reviewed_run.parent.parent / "impl-unreviewed" / "loop-run.json"
    reviewed_run.write_bytes(unreviewed_run.read_bytes())

    result = close_implementation_loop(
        ImplementationCloseOptions(root=tmp_path, loop_id="impl-reviewed", yes=True)
    )

    assert result.status == "blocked"
    assert "identity" in result.blocker.lower()
    assert not (unreviewed_run.parent / "implementation-close.json").exists()


def test_close_implementation_loop_writes_close_artifact(tmp_path: Path) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-close",
        )
    )
    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-close",
            task_id="T11",
            status="done",
            verification=("uv run pytest tests/unit/test_implementation_loop.py -q",),
        )
    )

    blocked = close_implementation_loop(
        ImplementationCloseOptions(root=tmp_path, loop_id="impl-close", yes=True)
    )
    assert blocked.status == "needs_fix"
    assert blocked.blocker == ("T11 has no successful verification for current source.")

    _record_successful_quality_result(tmp_path, "impl-close", "T11")

    loop_dir = tmp_path / ".ai-sdlc/loops/implementation/impl-close"
    reviewed_reports = {
        name: (loop_dir / name).read_bytes()
        for name in ("implementation-report.json", "implementation-report.md")
    }
    result = close_implementation_loop(
        ImplementationCloseOptions(root=tmp_path, loop_id="impl-close", yes=True)
    )

    assert {
        name: (loop_dir / name).read_bytes() for name in reviewed_reports
    } == reviewed_reports
    assert result.status == "ready"
    assert result.closed is True
    assert result.loop_status == "closed"
    assert result.next_action == "Run ai-sdlc pr-review start."
    close_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "implementation"
            / "impl-close"
            / "implementation-close.json"
        ).read_text("utf-8")
    )
    assert close_payload["artifact_kind"] == "implementation-close"
    assert close_payload["next_loop_type"] == "local-pr-review"
    spec_path = tmp_path / "specs" / "demo-implementation-loop" / "spec.md"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8") + "\n前端页面和浏览器证据。\n",
        encoding="utf-8",
    )

    repeated = close_implementation_loop(
        ImplementationCloseOptions(root=tmp_path, loop_id="impl-close", yes=True)
    )

    assert repeated.next_action == "Run ai-sdlc pr-review start."
    assert repeated.next_guidance.reason.endswith("local-pr-review.")
    assert repeated.next_guidance.requires_model is True


def test_close_implementation_loop_rechecks_review_digest_at_final_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    loop_id = "impl-final-review-guard"
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id=loop_id,
        )
    )
    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id=loop_id,
            task_id="T11",
            status="done",
            verification=("uv run pytest tests/unit/test_implementation_loop.py -q",),
        )
    )
    _record_successful_quality_result(tmp_path, loop_id, "T11")
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id=loop_id,
        review_round_number=1,
    )
    _write_clean_implementation_review(tmp_path, loop_id, reviewed)
    original_blockers = implementation_loop_module._close_blockers

    def mutate_after_state_validation(*args: object, **kwargs: object) -> list[str]:
        blockers = original_blockers(*args, **kwargs)
        report_path = (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "implementation"
            / loop_id
            / "implementation-report.md"
        )
        report_path.write_text(
            report_path.read_text(encoding="utf-8") + "\n评审后发生变化。\n",
            encoding="utf-8",
        )
        return blockers

    monkeypatch.setattr(
        implementation_loop_module,
        "_close_blockers",
        mutate_after_state_validation,
    )

    with pytest.raises(ReviewInputGuardError, match="review-input-drift"):
        close_implementation_loop(
            ImplementationCloseOptions(
                root=tmp_path,
                loop_id=loop_id,
                yes=True,
                expected_review_digest=reviewed.input_digest,
            ),
            review_input_validator=validate_review_input_for_close,
        )

    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "implementation"
        / loop_id
        / "implementation-close.json"
    ).exists()


def test_close_implementation_loop_routes_frontend_work_to_frontend_evidence(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path, frontend=True)
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-frontend",
        )
    )
    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-frontend",
            task_id="T11",
            status="done",
            verification=("uv run pytest tests/unit/test_implementation_loop.py -q",),
        )
    )
    _record_successful_quality_result(tmp_path, "impl-frontend", "T11")

    result = close_implementation_loop(
        ImplementationCloseOptions(root=tmp_path, loop_id="impl-frontend", yes=True)
    )

    assert result.status == "ready"
    assert result.next_action == (
        "Run ai-sdlc loop frontend-evidence start --wi specs/demo-implementation-loop."
    )
    close_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "implementation"
            / "impl-frontend"
            / "implementation-close.json"
        ).read_text("utf-8")
    )
    assert close_payload["next_loop_type"] == "frontend-evidence"


def test_close_implementation_loop_ignores_frontend_signal_inside_words(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(
        tmp_path,
        extra_spec="This backend build guidance uses a test suite.",
    )
    _close_design_contract_for_work_item(tmp_path, work_item)
    start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-non-frontend-words",
        )
    )
    record_implementation_progress(
        ImplementationRecordOptions(
            root=tmp_path,
            loop_id="impl-non-frontend-words",
            task_id="T11",
            status="done",
            verification=("uv run pytest tests/unit/test_implementation_loop.py -q",),
        )
    )
    _record_successful_quality_result(
        tmp_path,
        "impl-non-frontend-words",
        "T11",
    )

    result = close_implementation_loop(
        ImplementationCloseOptions(
            root=tmp_path,
            loop_id="impl-non-frontend-words",
            yes=True,
        )
    )

    assert result.next_action == "Run ai-sdlc pr-review start."
    close_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "implementation"
            / "impl-non-frontend-words"
            / "implementation-close.json"
        ).read_text("utf-8")
    )
    assert close_payload["next_loop_type"] == "local-pr-review"


def test_start_implementation_loop_blocks_mutated_design_snapshot(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text("utf-8") + "\n### Task 9.9 Injected\n",
        "utf-8",
    )

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-mutated-design-snapshot",
        )
    )

    assert result.status == "blocked"
    assert "design" in result.blocker.lower()
    assert "changed" in result.blocker.lower()


@pytest.mark.parametrize(
    "requirement_binding", ["explicit", "absent-legacy", "blank-legacy"]
)
def test_implementation_uses_only_the_saved_requirement_binding(
    tmp_path: Path, requirement_binding: str,
) -> None:
    from ai_sdlc.core.design_contract_models import DesignContractInput
    from ai_sdlc.core.design_contract_store import design_contract_input_digest

    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    design = tmp_path / ".ai-sdlc/loops/design-contract/dc-demo-implementation-loop"
    if requirement_binding != "explicit":
        # 历史 schema 允许空绑定；不能把当前 pointer 追加成旧 Design 的新前置。
        input_path = design / "design-contract-input.json"
        payload = json.loads(input_path.read_bytes())
        payload.pop("requirement_loop_id")
        if requirement_binding == "blank-legacy":
            payload["requirement_loop_id"] = " "
        contract_input = DesignContractInput.model_validate(payload)
        input_path.write_text(json.dumps(payload), encoding="utf-8")
        run_path = design / "loop-run.json"
        run = json.loads(run_path.read_bytes())
        run["input_digest"] = design_contract_input_digest(contract_input)
        run_path.write_text(json.dumps(run), encoding="utf-8")
    requirement = tmp_path / ".ai-sdlc/loops/requirement"
    (requirement / "current-requirement.json").write_text(
        '{"loop_id": "unrelated-current", "loop_run_path": "missing.json"}',
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in design.iterdir() if path.is_file()}
    result = start_implementation_loop(ImplementationStartOptions(
        root=tmp_path, work_item="specs/demo-implementation-loop",
        design_contract_loop_id="dc-demo-implementation-loop",
        loop_id="impl-saved-binding",
    ))
    assert result.status == "ready", result.blocker
    for path, content in before.items():
        assert path.read_bytes() == content


def test_implementation_rejects_changed_bound_requirement_scope(tmp_path: Path) -> None:
    from ai_sdlc.core.design_contract_models import DesignContractInput
    from ai_sdlc.core.design_contract_store import design_contract_input_digest

    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    design = tmp_path / ".ai-sdlc/loops/design-contract/dc-demo-implementation-loop"
    input_path = design / "design-contract-input.json"
    payload = json.loads(input_path.read_bytes())
    payload["authorized_scope_families"] = ["implementation", "frontend-evidence"]
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    run_path = design / "loop-run.json"
    run = json.loads(run_path.read_bytes())
    run["input_digest"] = design_contract_input_digest(
        DesignContractInput.model_validate(payload)
    )
    run_path.write_text(json.dumps(run), encoding="utf-8")
    before = {path: path.read_bytes() for path in design.iterdir() if path.is_file()}
    result = start_implementation_loop(ImplementationStartOptions(
        root=tmp_path, work_item="specs/demo-implementation-loop",
        design_contract_loop_id="dc-demo-implementation-loop",
        loop_id="impl-scope-drift",
    ))
    assert result.status == "blocked"
    assert "scope changed" in result.blocker
    assert not (tmp_path / ".ai-sdlc/loops/implementation").exists()
    for path, content in before.items():
        assert path.read_bytes() == content


def test_start_implementation_loop_ignores_copied_legacy_authority_artifact(
    tmp_path: Path,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    legacy = tmp_path / ".ai-sdlc" / "state" / "shared" / "scope-authority"
    legacy.mkdir(parents=True)
    (legacy / "copied-design-close.json").write_text("{not-json", encoding="utf-8")

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-legacy-authority-artifact",
        )
    )

    assert result.status == "ready"
    assert result.loop_status == "running"


@pytest.mark.parametrize(
    "artifact_name",
    (
        "design-contract-input.json",
        "design-contract-report.json",
        "design-contract-close.json",
    ),
)
def test_start_implementation_loop_rejects_malformed_design_close_artifact(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    artifact = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-demo-implementation-loop"
        / artifact_name
    )
    artifact.write_text("{not-json", "utf-8")

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id=f"impl-tampered-{artifact_name.removesuffix('.json')}",
        )
    )

    assert result.status == "blocked"
    assert "design" in result.blocker.lower()


@pytest.mark.parametrize(
    ("artifact_name", "updates"),
    (
        (
            "design-contract-report.json",
            {"status": "needs_fix", "blocker_count": 1},
        ),
        ("design-contract-close.json", {"blocker_count": 1}),
    ),
)
def test_start_implementation_loop_rejects_design_artifact_blockers(
    tmp_path: Path,
    artifact_name: str,
    updates: dict[str, object],
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    artifact = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-demo-implementation-loop"
        / artifact_name
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload.update(updates)
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id=f"impl-blocked-{artifact_name.removesuffix('.json')}",
        )
    )

    assert result.status == "blocked"
    assert "still contains blockers" in result.blocker.lower()


@pytest.mark.parametrize("missing", ["digest", "validator", "both"])
def test_b1_core_close_requires_actual_guard_not_only_done_tasks(
    tmp_path: Path, missing
):
    from ai_sdlc.core.loop_decision_models import DecisionPrepareInput
    from ai_sdlc.core.loop_decision_service import prepare_implementation_decision
    from tests.integration.test_quantified_implementation import (
        _ready_project,
        _request,
    )

    root = _ready_project(tmp_path)
    loop_id = "b1-core-close"
    assert (
        start_implementation_loop(
            ImplementationStartOptions(
                root=root,
                work_item="specs/demo-implementation-loop",
                loop_id=loop_id,
                decision_mode="adaptive-quantified",
            )
        ).status
        == "ready"
    )
    request_path = _request(root, root / "request.json")
    request = DecisionPrepareInput.model_validate_json(request_path.read_bytes())
    preview = prepare_implementation_decision(root, loop_id, request)
    prepare_implementation_decision(
        root, loop_id, request, dry_run=False, expected_digest=preview.prepare_digest
    )
    record_implementation_progress(
        ImplementationRecordOptions(
            root=root,
            loop_id=loop_id,
            task_id="T11",
            status="done",
            evidence=("src/ai_sdlc/core/implementation_loop.py",),
        )
    )
    assert _record_successful_quality_result(root, loop_id, "T11").done_count == 1
    reviewed = resolve_review_input(
        root, loop_type="implementation", loop_id=loop_id, review_round_number=1
    )
    directory = root / ".ai-sdlc/loops/implementation" / loop_id
    original = {
        path: path.read_bytes() for path in directory.iterdir() if path.is_file()
    }
    result = close_implementation_loop(
        ImplementationCloseOptions(
            root=root,
            loop_id=loop_id,
            yes=True,
            expected_review_digest=""
            if missing in {"digest", "both"}
            else reviewed.input_digest,
        ),
        review_input_validator=(
            None
            if missing in {"validator", "both"}
            else validate_review_input_for_close
        ),
    )
    assert result.status == "blocked"
    assert "review" in result.blocker
    assert not result.closed
    assert not (
        root / ".ai-sdlc/loops/implementation" / loop_id / "implementation-close.json"
    ).exists()
    assert original == {
        path: path.read_bytes() for path in directory.iterdir() if path.is_file()
    }


def _write_ready_work_item(
    tmp_path: Path,
    *,
    frontend: bool = False,
    extra_spec: str = "",
) -> Path:
    work_item = tmp_path / "specs" / "demo-implementation-loop"
    work_item.mkdir(parents=True)
    frontend_text = " 前端页面和浏览器证据。" if frontend else ""
    extra_spec_text = f" {extra_spec}" if extra_spec else ""
    (work_item / "spec.md").write_text(
        "\n".join(
            [
                "---",
                "design_scope_families:",
                "  - implementation",
                "---",
                "# PRD：Implementation Demo",
                "",
                "**状态**：formal baseline 已冻结",
                "",
                "## 需求",
                "",
                f"- **FR-IMPL-001**：系统必须记录实现任务证据。{frontend_text}{extra_spec_text}",
                "",
                "## 成功标准",
                "",
                "- **SC-IMPL-001**：完成任务后可以关闭 implementation loop。",
            ]
        ),
        encoding="utf-8",
    )
    (work_item / "plan.md").write_text(
        "\n".join(
            [
                "# 实施计划：Implementation Demo",
                "",
                "## 技术背景",
                "Python runtime.",
                "## 阶段计划",
                "Phase 1.",
                "## 验证",
                "Run pytest.",
                "## 回退",
                "Revert the commit.",
            ]
        ),
        encoding="utf-8",
    )
    (work_item / "tasks.md").write_text(
        "\n".join(
            [
                "# 任务分解：Implementation Demo",
                "",
                "### Task 1.1 Implement runtime",
                "",
                "- **任务编号**：T11",
                "- **优先级**：P0",
                "- **文件**：src/ai_sdlc/core/implementation_loop.py",
                "- **验收标准**：",
                "  1. FR-IMPL-001 and SC-IMPL-001 are covered.",
                "- **验证**：`uv run pytest tests/unit/test_implementation_loop.py -q`",
                "",
                "### Task 2.1 Deferred polish",
                "",
                "- **任务编号**：T21",
                "- **优先级**：P2",
                "- **文件**：README.md",
                "- **验收标准**：",
                "  1. Deferred.",
                "- **验证**：`uv run pytest tests/unit/test_implementation_loop.py -q`",
            ]
        ),
        encoding="utf-8",
    )
    return work_item


def _start_frozen_requirement(tmp_path: Path, work_item: Path) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="Implementation loop needs evidence tracking.",
            acceptance=("Implementation evidence can be closed.",),
            design_scope_families=("implementation",),
            work_item_id=work_item.name,
            loop_id="req-demo-implementation-loop",
        )
    )
    freeze_requirement_loop(
        RequirementFreezeOptions(
            root=tmp_path,
            loop_id="req-demo-implementation-loop",
            yes=True,
        )
    )


def _close_design_contract_for_work_item(tmp_path: Path, work_item: Path) -> None:
    _start_frozen_requirement(tmp_path, work_item)
    check = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            requirement_loop_id="req-demo-implementation-loop",
            loop_id="dc-demo-implementation-loop",
        )
    )
    assert check.status == "ready"
    close = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-demo-implementation-loop",
            yes=True,
        )
    )
    assert close.status == "ready"
    assert close.closed is True


def _record_successful_quality_result(
    root: Path,
    loop_id: str,
    task_id: str,
) -> ImplementationCommandResult:
    _ensure_git_repository(root)
    result = verify_implementation_task(
        ImplementationVerifyOptions(
            root=root,
            loop_id=loop_id,
            task_id=task_id,
            cwd=".",
            argv=(sys.executable, "-c", "print('verified')"),
        )
    )
    assert result.status == "ready"
    return result


def _write_clean_implementation_review(
    root: Path,
    loop_id: str,
    reviewed: ReviewInput,
) -> None:
    outcome = LoopReviewOutcome(
        loop_id=loop_id,
        loop_type="implementation",
        round_number=reviewed.round_number,
        input_digest=reviewed.input_digest,
        status="completed",
        expert_roles=reviewed.expert_roles,
        findings=[],
        recorded_at="2026-08-18T00:00:00Z",
    )
    loop_dir = root / ".ai-sdlc" / "loops" / "implementation" / loop_id
    (loop_dir / f"review-outcome-round-{reviewed.round_number}.json").write_text(
        outcome.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def _ensure_git_repository(root: Path) -> None:
    if (root / ".git").exists():
        return
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "specs"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "test baseline"], cwd=root, check=True)
