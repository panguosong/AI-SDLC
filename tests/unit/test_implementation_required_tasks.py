"""验证显式必做任务与原有优先级规则在实现阶段保持一致。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_sdlc.core.implementation_loop import (
    ImplementationCloseOptions,
    ImplementationRecordOptions,
    ImplementationStartOptions,
    _task_required,
    close_implementation_loop,
    record_implementation_progress,
    start_implementation_loop,
)
from tests.unit.test_implementation_loop import (
    _close_design_contract_for_work_item,
    _record_successful_quality_result,
    _write_ready_work_item,
)

_TASK_IDS = ("T11", "T12", "T13", "T14")


def _explicit_required_work_item(root: Path) -> Path:
    work_item = _write_ready_work_item(root)
    sections = ["# 任务分解：Implementation Demo"]
    for task_id in _TASK_IDS:
        sections.append(
            "\n".join(
                [
                    f"### Task {task_id} 完成约定的工作",
                    f"- task_id: {task_id}",
                    "- required: true",
                    f"- scope: src/{task_id}.py",
                    "- acceptance criteria: FR-IMPL-001 and SC-IMPL-001 are covered.",
                    f"- verification: python check_{task_id}.py",
                ]
            )
        )
    (work_item / "tasks.md").write_text("\n\n".join(sections), encoding="utf-8")
    return work_item


def test_explicit_required_tasks_preserve_all_tasks_without_inventing_priority(
    tmp_path: Path,
) -> None:
    work_item = _explicit_required_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-explicit-required",
        )
    )

    assert result.status == "ready", result.blocker
    assert result.required_task_count == 4
    directory = tmp_path / ".ai-sdlc/loops/implementation/impl-explicit-required"
    tasks = json.loads((directory / "implementation-tasks.json").read_text(encoding="utf-8"))
    assert [item["task_id"] for item in tasks["items"]] == list(_TASK_IDS)
    assert all(item["required"] for item in tasks["items"])
    assert all(item["priority"] == "" for item in tasks["items"])
    for item in tasks["items"]:
        assert item["files"] == [f"src/{item['task_id']}.py"]
        assert item["acceptance"] == [
            "FR-IMPL-001 and SC-IMPL-001 are covered."
        ]
        assert item["verification_hints"] == [f"python check_{item['task_id']}.py"]
    bound_input = json.loads((directory / "implementation-input.json").read_text(encoding="utf-8"))
    assert bound_input["task_scopes"] == {
        task_id: [f"src/{task_id}.py"] for task_id in _TASK_IDS
    }


@pytest.mark.parametrize("missing_task", _TASK_IDS)
def test_each_explicit_required_task_blocks_report_and_close_until_done(
    tmp_path: Path, missing_task: str
) -> None:
    work_item = _explicit_required_work_item(tmp_path)
    _close_design_contract_for_work_item(tmp_path, work_item)
    loop_id = "impl-required-progress"
    started = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path, work_item="specs/demo-implementation-loop", loop_id=loop_id
        )
    )
    assert started.status == "ready", started.blocker

    for task_id in _TASK_IDS:
        if task_id == missing_task:
            continue
        recorded = record_implementation_progress(
            ImplementationRecordOptions(
                root=tmp_path,
                loop_id=loop_id,
                task_id=task_id,
                status="done",
                verification=(f"python check_{task_id}.py",),
            )
        )
        assert recorded.status == "ready", recorded.blocker
        report = _record_successful_quality_result(tmp_path, loop_id, task_id)
    assert report.required_task_count == 4
    assert report.done_count == 3
    assert report.loop_status == "running"
    assert missing_task in report.next_action

    closed = close_implementation_loop(
        ImplementationCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )
    assert closed.status == "needs_fix"
    assert closed.blocker == f"{missing_task} is not done."
    assert not (
        tmp_path / f".ai-sdlc/loops/implementation/{loop_id}/implementation-close.json"
    ).exists()


@pytest.mark.parametrize(
    "metadata,priority,required",
    [
        ("- priority: P0", "P0", True),
        ("- priority: P1", "P1", True),
        ("- priority: P2", "P2", False),
        ("", "", False),
        ("- required: false", "", False),
        ("- priority: P2\n- required: false", "P2", False),
        ("- priority: P2\n- required: true", "P2", True),
        ("**Required**：TRUE", "", True),
    ],
)
def test_required_field_preserves_legacy_priority_and_legal_optional_tasks(
    tmp_path: Path, metadata: str, priority: str, required: bool
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace("- **优先级**：P2", metadata), encoding="utf-8"
    )
    _close_design_contract_for_work_item(tmp_path, work_item)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path, work_item="specs/demo-implementation-loop", loop_id="impl-fields"
        )
    )
    assert result.status == "ready", result.blocker
    path = tmp_path / ".ai-sdlc/loops/implementation/impl-fields/implementation-tasks.json"
    item = json.loads(path.read_text(encoding="utf-8"))["items"][1]
    assert item["priority"] == priority
    assert item["required"] is required


@pytest.mark.parametrize(
    "metadata",
    [
        "- required: true\n- required: true",
        "- required: true\n**Required**: false",
        "- required: yes",
        "- required: 1",
        "- required: 'true'",
        "- required:",
        "- required: true because the task matters",
        "- priority: P0\n- required: false",
        "- priority: P1\n- required: false",
    ],
)
def test_invalid_or_conflicting_required_fields_block_before_snapshot(
    tmp_path: Path, metadata: str
) -> None:
    work_item = _write_ready_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace("- **优先级**：P2", metadata), encoding="utf-8"
    )
    _close_design_contract_for_work_item(tmp_path, work_item)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path, work_item="specs/demo-implementation-loop", loop_id="impl-invalid"
        )
    )
    assert result.status == "blocked"
    assert "required" in result.blocker
    assert "T21" in result.blocker
    assert not (tmp_path / ".ai-sdlc/loops/implementation/impl-invalid").exists()


def test_required_field_does_not_match_prose_or_similar_field_names(
    tmp_path: Path,
) -> None:
    work_item = _explicit_required_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- required: true",
            "- required: true\n"
            "正文引用 required: false，不是字段。\n"
            "- notes: required: false\n"
            "- required behavior: false\n"
            "- not_required: false",
        ),
        encoding="utf-8",
    )
    _close_design_contract_for_work_item(tmp_path, work_item)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path, work_item="specs/demo-implementation-loop", loop_id="impl-prose"
        )
    )
    assert result.status == "ready", result.blocker
    assert result.required_task_count == 4


def test_tasks_still_require_at_least_one_required_item(tmp_path: Path) -> None:
    work_item = _explicit_required_work_item(tmp_path)
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace("- required: true", "- required: false"),
        encoding="utf-8",
    )
    _close_design_contract_for_work_item(tmp_path, work_item)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=tmp_path, work_item="specs/demo-implementation-loop", loop_id="impl-optional"
        )
    )
    assert result.status == "blocked"
    assert "at least one" in result.blocker
    assert not (tmp_path / ".ai-sdlc/loops/implementation/impl-optional").exists()


@pytest.mark.parametrize(
    "section,priority,required",
    [
        ("- required: true\n\n```yaml\nrequired: false\n```", "", True),
        ("- required: true\n\n~~~yaml\nrequired: false\n~~~", "", True),
        ("```yaml\nrequired: true\n```", "P2", False),
        ("~~~yaml\nrequired: true\n~~~", "P2", False),
        ("    required: true", "P2", False),
        ("\trequired: true", "P2", False),
        ("- required: true\n\n    required: false", "", True),
        ("````yaml\n```\nrequired: true\n````", "P2", False),
        ("~~~yaml\n```\nrequired: true\n~~~", "P2", False),
        ("   ```yaml\nrequired: false\n   ```\n**Required**：TRUE", "", True),
        ("```yaml\nrequired: false\n```\n   - required: true", "", True),
    ],
)
def test_required_field_ignores_markdown_code_examples(
    section: str, priority: str, required: bool
) -> None:
    assert _task_required(section, priority) == (required, "")
