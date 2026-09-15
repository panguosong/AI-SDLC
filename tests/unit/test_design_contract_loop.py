"""Tests for the deterministic design-contract loop runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ai_sdlc.core.design_contract_loop as design_contract_loop_module
from ai_sdlc.cli.loop_review_cmd import (
    ReviewInputGuardError,
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.cli.main import app
from ai_sdlc.core.design_contract_loop import (
    CURRENT_DESIGN_CONTRACT_PATH,
    DesignContractCheckOptions,
    DesignContractCloseOptions,
    check_design_contract_loop,
    close_design_contract_loop,
)
from ai_sdlc.core.design_contract_models import (
    DesignContractInput,
    DesignContractReport,
)
from ai_sdlc.core.design_contract_store import (
    DesignContractArtifacts,
    require_design_check_published,
    resolve_design_contract_loop_run_path,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRun
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.requirement_loop import (
    RequirementFreezeOptions,
    RequirementIntake,
    RequirementStartOptions,
    _requirement_intake_digest,
    freeze_requirement_loop,
    start_requirement_loop,
)


def test_public_design_contract_resolver_keeps_two_value_signature(
    tmp_path: Path,
) -> None:
    path, blocker = resolve_design_contract_loop_run_path(tmp_path, "")

    assert path == tmp_path / CURRENT_DESIGN_CONTRACT_PATH
    assert blocker == "No current design-contract loop exists."


def test_check_design_contract_loop_waits_for_expert_review(tmp_path: Path) -> None:
    work_item = _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-001",
        )
    )

    assert result.status == "ready"
    assert result.loop_status == "needs_review"
    assert result.work_item_id == "demo-contract"
    assert result.work_item_path == "specs/demo-contract"
    assert result.blocker_count == 0
    assert result.coverage_count == 2
    assert result.design_contract is not None
    assert result.design_contract.status == "needs_review"
    assert result.design_contract.coverage_count == 2
    assert result.design_contract.coverage_matrix_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-001/coverage-matrix.json"
    )
    assert result.design_contract.report_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-001/design-contract-report.json"
    )
    assert (
        result.next_action
        == "Run ai-sdlc loop review --type design-contract --loop-id dc-001."
    )
    assert (
        result.next_guidance.command
        == "ai-sdlc loop review --type design-contract --loop-id dc-001"
    )
    assert result.next_guidance.requires_model is True
    assert result.next_guidance.writes_artifacts is False
    assert result.next_guidance.writes_code is False

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-001"
    assert (loop_dir / "loop-run.json").is_file()
    assert (loop_dir / "design-contract-input.json").is_file()
    assert (loop_dir / "coverage-matrix.json").is_file()
    assert (loop_dir / "design-contract-report.json").is_file()
    assert (loop_dir / "design-contract-report.md").is_file()
    assert (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).is_file()

    report = json.loads(
        (loop_dir / "design-contract-report.json").read_text(encoding="utf-8")
    )
    assert report["artifact_kind"] == "design-contract-report"
    assert report["status"] == "needs_review"
    assert report["coverage_count"] == 2
    assert {item["source_id"] for item in report["coverage_items"]} == {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }
    assert {
        item["source_id"]: item["covered_by"] for item in report["coverage_items"]
    } == {
        "FR-DEMO-001": ["T11"],
        "SC-DEMO-001": ["T11"],
    }
    coverage = json.loads(
        (loop_dir / "coverage-matrix.json").read_text(encoding="utf-8")
    )
    assert coverage["artifact_kind"] == "coverage-matrix"
    assert coverage["created_by"] == "ai-sdlc"
    assert coverage["created_at"]
    assert coverage["ai_sdlc_version"]
    pointer = json.loads(
        (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).read_text(encoding="utf-8")
    )
    assert pointer["artifact_kind"] == "current-design-contract-pointer"
    assert pointer["created_by"] == "ai-sdlc"
    assert pointer["created_at"]
    assert pointer["ai_sdlc_version"]

    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["loop_type"] == "design-contract"
    assert loop_run["status"] == "needs_review"
    assert loop_run["work_item_id"] == work_item.name
    contract_input = json.loads(
        (loop_dir / "design-contract-input.json").read_text(encoding="utf-8")
    )
    assert contract_input["requirement_loop_id"] == "req-current"


def test_check_design_contract_loop_allows_fix_and_recheck_in_same_loop(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )
    first = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-fix-and-recheck",
        )
    )
    assert first.status == "needs_fix"

    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "Cover contract docs.\n- **验证**：",
            "Cover FR-DEMO-001 and SC-DEMO-001.\n"
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
        ),
        encoding="utf-8",
    )
    second = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-fix-and-recheck",
        )
    )

    assert second.status == "ready"
    assert second.loop_status == "needs_review"
    closed = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-fix-and-recheck",
            yes=True,
        )
    )
    assert closed.status == "ready"
    assert closed.closed is True


def test_check_design_contract_loop_recovers_initial_partial_artifact_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-initial-write-recovery"
    original_write = design_contract_loop_module._write_check_artifacts
    original_build = design_contract_loop_module.build_contract_input
    timestamps = iter(("2030-01-01T00:00:00Z", "2030-01-01T00:00:01Z"))

    def build_with_advancing_timestamp(
        *,
        root: Path,
        loop_id: str,
        work_item_dir: Path,
        requirement_loop_id: str,
        **verification_options,
    ) -> DesignContractInput:
        built = original_build(
            root=root,
            loop_id=loop_id,
            work_item_dir=work_item_dir,
            requirement_loop_id=requirement_loop_id,
            **verification_options,
        )
        return built.model_copy(update={"created_at": next(timestamps)})

    def write_input_then_fail(
        root: Path,
        contract_input: DesignContractInput,
        report: DesignContractReport,
        loop_run: LoopRun,
        artifacts: DesignContractArtifacts,
        *,
        loop_run_must_be_absent: bool = False,
    ) -> None:
        LoopArtifactStore(root).write_json_artifact(
            artifacts.input_path,
            contract_input,
        )
        raise OSError("injected initial artifact write failure")

    monkeypatch.setattr(
        design_contract_loop_module,
        "build_contract_input",
        build_with_advancing_timestamp,
    )
    monkeypatch.setattr(
        design_contract_loop_module,
        "_write_check_artifacts",
        write_input_then_fail,
    )
    failed = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert failed.status == "blocked"
    assert "injected initial artifact write failure" in failed.blocker
    assert f"--loop-id {loop_id}" in failed.next_action

    monkeypatch.setattr(
        design_contract_loop_module,
        "_write_check_artifacts",
        original_write,
    )
    retried = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert retried.status == "ready", retried.blocker
    assert retried.loop_status == "needs_review"


@pytest.mark.parametrize(
    "drift,expected_side",
    [
        ("target-before-write", "old"),
        ("target-at-finish", "new"),
        ("journal-at-finish", "new"),
    ],
)
def test_fix24_design_initial_publication_drift_recovers_same_cli(
    tmp_path, monkeypatch, drift, expected_side
):
    _write_work_item(tmp_path)
    monkeypatch.chdir(tmp_path)
    loop_id = "dc-initial-drift"
    artifacts = design_contract_loop_module.design_contract_artifacts(tmp_path, loop_id)
    paths = design_contract_loop_module._design_publication_paths(artifacts)
    pending = artifacts.loop_dir / design_contract_loop_module.DESIGN_CHECK_PENDING
    target = artifacts.coverage_matrix_path
    original_write = LoopArtifactStore.write_bytes_artifact
    runner = CliRunner()
    command = [
        "loop", "design-contract", "check", "--wi", "specs/demo-contract",
        "--loop-id", loop_id, "--json",
    ]
    changed_bytes = {}

    def introduce_drift(self, path, content, **kwargs):
        result = original_write(self, path, content, **kwargs)
        if path == pending and drift == "target-before-write":
            states = design_contract_loop_module._validate_design_publication(
                content, artifacts
            )
            # 模拟并发者已写入日志中的新件；恢复只能消费已记录的 old/new。
            target.write_bytes(states[target.name]["new"])
            changed_bytes["target"] = target.read_bytes()
        if path == artifacts.pointer_path and drift == "target-at-finish":
            target.unlink()
            changed_bytes["target"] = None
        if path == artifacts.pointer_path and drift == "journal-at-finish":
            # 字节改变但身份与摘要仍有效，恢复必须以实际保留的 journal 为准。
            pending.write_bytes(pending.read_bytes() + b"\n")
            changed_bytes["journal"] = pending.read_bytes()
        return result

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            LoopArtifactStore, "write_bytes_artifact", introduce_drift
        )
        first = runner.invoke(app, command)
    assert first.exit_code == 1
    payload = json.loads(first.stdout)
    assert payload["status"] == "blocked"
    expected_error = "journal-drift" if drift == "journal-at-finish" else "target-drift"
    assert expected_error in payload["blocker"]
    assert f"--loop-id {loop_id}" in payload["next_action"]
    assert '--wi "specs/demo-contract"' in payload["next_action"]
    assert "--requirement-loop-id req-current" in payload["next_action"]
    raw = pending.read_bytes()
    before = {
        name: path.read_bytes() if path.exists() else None
        for name, path in paths.items()
    }
    if "target" in changed_bytes:
        assert before[target.name] == changed_bytes["target"]
    if "journal" in changed_bytes:
        assert raw == changed_bytes["journal"]
    preview = runner.invoke(app, [*command, "--dry-run"])
    assert preview.exit_code == 1
    assert json.loads(preview.stdout)["status"] == "blocked"
    assert pending.read_bytes() == raw
    assert {
        name: path.read_bytes() if path.exists() else None
        for name, path in paths.items()
    } == before

    states = design_contract_loop_module._validate_design_publication(raw, artifacts)
    recovered_states = []
    original_recover = design_contract_loop_module._recover_design_publication

    def observe_actual_recovery(root, current_artifacts):
        original_recover(root, current_artifacts)
        recovered_states.append({
            name: path.read_bytes() if path.exists() else None
            for name, path in paths.items()
        })
        archive = (
            artifacts.loop_dir / "design-check-publications"
            / (hashlib.sha256(raw).hexdigest() + ".json")
        )
        assert archive.read_bytes() == raw
        assert not pending.exists()

    recovery_command = shlex.split(
        payload["next_action"].removeprefix("Run ").removesuffix(
            " to recover this publication."
        )
    )
    with monkeypatch.context() as recovering:
        recovering.setattr(
            design_contract_loop_module,
            "_recover_design_publication",
            observe_actual_recovery,
        )
        completed = runner.invoke(app, [*recovery_command[1:], "--json"])
    assert completed.exit_code == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready"
    assert recovered_states == [{
        name: values[expected_side] for name, values in states.items()
    }]
    assert not pending.exists()
    current_input = DesignContractInput.model_validate_json(
        artifacts.input_path.read_bytes()
    )
    current_run = LoopRun.model_validate_json(artifacts.loop_run_path.read_bytes())
    assert current_run.input_digest == (
        design_contract_loop_module.design_contract_input_digest(current_input)
    )
    assert resolve_review_input(
        root=tmp_path,
        loop_type="design-contract",
        loop_id=loop_id,
        review_round_number=1,
    ).loop_id == loop_id


@pytest.mark.parametrize("damage", ["target", "journal"])
def test_fix24_design_initial_publication_damage_stays_blocked(
    tmp_path, monkeypatch, damage
):
    _write_work_item(tmp_path)
    monkeypatch.chdir(tmp_path)
    loop_id = "dc-initial-damaged"
    artifacts = design_contract_loop_module.design_contract_artifacts(tmp_path, loop_id)
    paths = design_contract_loop_module._design_publication_paths(artifacts)
    pending = artifacts.loop_dir / design_contract_loop_module.DESIGN_CHECK_PENDING
    original_write = LoopArtifactStore.write_bytes_artifact
    runner = CliRunner()
    command = [
        "loop", "design-contract", "check", "--wi", "specs/demo-contract",
        "--loop-id", loop_id, "--json",
    ]
    corrupted = b"unrecognized concurrent bytes\n"

    def introduce_damage(self, path, content, **kwargs):
        result = original_write(self, path, content, **kwargs)
        if path == pending and damage == "target":
            artifacts.coverage_matrix_path.write_bytes(corrupted)
        if path == artifacts.pointer_path and damage == "journal":
            pending.write_bytes(corrupted)
        return result

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            LoopArtifactStore, "write_bytes_artifact", introduce_damage
        )
        first = runner.invoke(app, command)
    assert first.exit_code == 1
    first_payload = json.loads(first.stdout)
    assert first_payload["status"] == "blocked"
    assert f"--loop-id {loop_id}" in first_payload["next_action"]
    raw = pending.read_bytes()
    before = {
        name: path.read_bytes() if path.exists() else None
        for name, path in paths.items()
    }
    assert (
        before[artifacts.coverage_matrix_path.name] if damage == "target" else raw
    ) == corrupted
    repeated = runner.invoke(app, command)
    assert repeated.exit_code == 1
    assert json.loads(repeated.stdout)["status"] == "blocked"
    assert pending.read_bytes() == raw
    assert {
        name: path.read_bytes() if path.exists() else None
        for name, path in paths.items()
    } == before
    if damage == "target":
        # 初次写入尚未到 run/指针，当前入口应先因没有发布的指针拒绝。
        assert not artifacts.loop_run_path.exists()
        assert not artifacts.pointer_path.exists()
        rejection = r"current-design-contract\.json"
    else:
        assert artifacts.loop_run_path.is_file()
        assert artifacts.pointer_path.is_file()
        rejection = "publication-pending"
    with pytest.raises((ValueError, ReviewInputGuardError), match=rejection):
        resolve_review_input(
            root=tmp_path,
            loop_type="design-contract",
            loop_id=loop_id,
            review_round_number=1,
        )
    with pytest.raises(ValueError, match="publication-pending"):
        require_design_check_published(artifacts.loop_dir)
    assert pending.read_bytes() == raw
    assert {
        name: path.read_bytes() if path.exists() else None
        for name, path in paths.items()
    } == before


@pytest.mark.parametrize(
    "message",
    ["unexpected publisher programming error", "design-contract-publication-target-drift"],
)
def test_fix24_design_initial_publication_unknown_value_error_propagates(
    tmp_path, monkeypatch, message
):
    _write_work_item(tmp_path)
    monkeypatch.chdir(tmp_path)
    loop_id = "dc-initial-unknown-error"
    artifacts = design_contract_loop_module.design_contract_artifacts(tmp_path, loop_id)
    pending = artifacts.loop_dir / design_contract_loop_module.DESIGN_CHECK_PENDING
    original_write = LoopArtifactStore.write_bytes_artifact
    raised = ValueError(message)
    written = []

    def write_journal_then_raise(self, path, content, **kwargs):
        result = original_write(self, path, content, **kwargs)
        if path == pending:
            written.append(content)
            raise raised
        return result

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            LoopArtifactStore, "write_bytes_artifact", write_journal_then_raise
        )
        result = CliRunner().invoke(app, [
            "loop", "design-contract", "check", "--wi", "specs/demo-contract",
            "--loop-id", loop_id, "--json",
        ])
    assert result.exit_code == 1
    assert result.exception is raised
    assert not result.stdout.strip()
    assert written == [pending.read_bytes()]
    assert not artifacts.input_path.exists()


@pytest.mark.parametrize(
    "failed_target",
    ["coverage-matrix.json", "loop-run.json", "current-design-contract.json"],
)
def test_design_check_recheck_recovers_interrupted_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_target: str
) -> None:
    work_item = _write_work_item(tmp_path)
    options = DesignContractCheckOptions(
        root=tmp_path,
        work_item="specs/demo-contract",
        loop_id="dc-publication-recovery",
    )
    assert check_design_contract_loop(options).status == "ready"
    plan = work_item / "plan.md"
    plan.write_text(plan.read_text() + "\n补充有界执行说明。\n", encoding="utf-8")
    original_json = LoopArtifactStore.write_json_artifact
    original_bytes = LoopArtifactStore.write_bytes_artifact

    def fail_json(self, path, payload):
        if path.name == failed_target:
            raise OSError("interrupted design publication")
        return original_json(self, path, payload)

    def fail_bytes(self, path, content, **kwargs):
        if path.name == failed_target:
            raise OSError("interrupted design publication")
        return original_bytes(self, path, content, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(LoopArtifactStore, "write_json_artifact", fail_json)
        interrupted.setattr(LoopArtifactStore, "write_bytes_artifact", fail_bytes)
        try:
            failed = check_design_contract_loop(options)
        except OSError:
            pass
        else:
            assert failed.status == "blocked"
            assert "--loop-id dc-publication-recovery" in failed.next_action
    retried = check_design_contract_loop(options)
    assert retried.status == "ready", retried.blocker
    artifacts = design_contract_loop_module.design_contract_artifacts(
        tmp_path, options.loop_id
    )
    current_input = DesignContractInput.model_validate_json(
        artifacts.input_path.read_bytes()
    )
    current_run = LoopRun.model_validate_json(artifacts.loop_run_path.read_bytes())
    assert (
        current_run.input_digest
        == design_contract_loop_module.design_contract_input_digest(current_input)
    )
    assert current_run.loop_id == options.loop_id


def _interrupted_publication_case(
    tmp_path, monkeypatch, *, initial=False, target="coverage-matrix.json"
):
    work_item = _write_work_item(tmp_path)
    options = DesignContractCheckOptions(
        root=tmp_path, work_item="specs/demo-contract", loop_id="dc-interrupted"
    )
    if not initial:
        assert check_design_contract_loop(options).status == "ready"
        plan = work_item / "plan.md"
        plan.write_text(plan.read_text() + "\n补充执行说明。\n", encoding="utf-8")
    artifacts = design_contract_loop_module.design_contract_artifacts(
        tmp_path, options.loop_id
    )
    paths = design_contract_loop_module._design_publication_paths(artifacts)
    before = {
        name: path.read_bytes() if path.exists() else None
        for name, path in paths.items()
    }
    original = LoopArtifactStore.write_bytes_artifact

    def interrupt(self, path, content, **kwargs):
        if path.name == target or (
            target == "archive" and path.parent.name == "design-check-publications"
        ):
            raise OSError("publication write interrupted")
        return original(self, path, content, **kwargs)

    with monkeypatch.context() as blocked:
        blocked.setattr(LoopArtifactStore, "write_bytes_artifact", interrupt)
        result = check_design_contract_loop(options)
    assert result.status == "blocked", result
    assert result.loop_id == options.loop_id
    assert '--wi "specs/demo-contract"' in result.next_action
    pending = artifacts.loop_dir / design_contract_loop_module.DESIGN_CHECK_PENDING
    assert pending.is_file()
    return options, artifacts, paths, before, pending


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize(
    "target,side",
    [
        ("coverage-matrix.json", "old"),
        ("current-design-contract.json", "new"),
        ("archive", "new"),
    ],
)
def test_design_publication_restores_exact_old_or_committed_new_bytes(
    tmp_path, monkeypatch, initial, target, side
):
    options, artifacts, paths, before, pending = _interrupted_publication_case(
        tmp_path, monkeypatch, initial=initial, target=target
    )
    raw = pending.read_bytes()
    states = design_contract_loop_module._validate_design_publication(raw, artifacts)
    with design_contract_loop_module._stage_write_guard(
        tmp_path, "design-contract", options.loop_id
    ):
        design_contract_loop_module._recover_design_publication(tmp_path, artifacts)
    assert not pending.exists()
    actual = {
        name: path.read_bytes() if path.exists() else None
        for name, path in paths.items()
    }
    assert actual == {name: values[side] for name, values in states.items()}
    if side == "old":
        assert actual == before
    archive = (
        artifacts.loop_dir
        / "design-check-publications"
        / (hashlib.sha256(raw).hexdigest() + ".json")
    )
    assert archive.read_bytes() == raw
    assert check_design_contract_loop(options).status == "ready"


@pytest.mark.parametrize(
    "target", ["coverage-matrix.json", "current-design-contract.json"]
)
def test_design_publication_rejects_unknown_file_before_any_recovery_write(
    tmp_path, monkeypatch, target
):
    options, _, paths, _, pending = _interrupted_publication_case(tmp_path, monkeypatch)
    paths[target].write_text("another process wrote this value\n", encoding="utf-8")
    before = {name: path.read_bytes() for name, path in paths.items()}
    journal = pending.read_bytes()
    result = check_design_contract_loop(options)
    assert result.status == "blocked"
    assert "publication-target-drift" in result.blocker
    assert {name: path.read_bytes() for name, path in paths.items()} == before
    assert pending.read_bytes() == journal


@pytest.mark.parametrize("corruption", ["loop", "digest", "extra-target"])
def test_design_publication_rejects_inconsistent_journal_without_writes(
    tmp_path, monkeypatch, corruption
):
    options, _, paths, _, pending = _interrupted_publication_case(tmp_path, monkeypatch)
    journal = json.loads(pending.read_bytes())
    if corruption == "loop":
        journal["loop_id"] = "other-loop"
    elif corruption == "digest":
        journal["entries"]["loop-run.json"]["old"]["sha256"] = "0" * 64
    else:
        journal["entries"]["other-file.json"] = journal["entries"]["loop-run.json"]
    pending.write_text(json.dumps(journal), encoding="utf-8")
    before = {name: path.read_bytes() for name, path in paths.items()}
    raw = pending.read_bytes()
    result = check_design_contract_loop(options)
    assert result.status == "blocked"
    assert {name: path.read_bytes() for name, path in paths.items()} == before
    assert pending.read_bytes() == raw


def test_design_publication_pending_blocks_dry_run_close_review_and_downstream_reader(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from ai_sdlc.core.design_contract_store import read_loop_run
    from ai_sdlc.core.implementation_loop import _design_contract_gate

    options, artifacts, paths, _, pending = _interrupted_publication_case(
        tmp_path, monkeypatch
    )
    before = {name: path.read_bytes() for name, path in paths.items()}
    raw = pending.read_bytes()
    assert (
        check_design_contract_loop(replace(options, dry_run=True)).status == "blocked"
    )
    assert check_design_contract_loop(replace(options, loop_id="")).status == "blocked"
    closed = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=options.loop_id, yes=True)
    )
    assert closed.status == "blocked"
    assert not artifacts.close_path.exists()
    with pytest.raises(ValueError, match="publication-pending"):
        read_loop_run(artifacts.loop_run_path, root=tmp_path)
    gate = _design_contract_gate(
        tmp_path, options.loop_id, work_item_id="demo-contract"
    )
    assert "publication-pending" in gate[2]
    with pytest.raises(
        (ValueError, ReviewInputGuardError), match="publication-pending"
    ):
        resolve_review_input(
            root=tmp_path,
            loop_type="design-contract",
            loop_id=options.loop_id,
            review_round_number=1,
        )
    assert {name: path.read_bytes() for name, path in paths.items()} == before
    assert pending.read_bytes() == raw


@pytest.mark.parametrize(
    "target", ["design-check-publication.pending.json", "design-contract-input.json"]
)
def test_design_publication_replace_failure_keeps_original_complete_bytes(
    tmp_path, monkeypatch, target
):
    import ai_sdlc.core.loop_artifacts as artifact_module

    work_item = _write_work_item(tmp_path)
    options = DesignContractCheckOptions(
        root=tmp_path, work_item="specs/demo-contract", loop_id="dc-strict-publication"
    )
    assert check_design_contract_loop(options).status == "ready"
    artifacts = design_contract_loop_module.design_contract_artifacts(
        tmp_path, options.loop_id
    )
    paths = design_contract_loop_module._design_publication_paths(artifacts)
    before = {name: path.read_bytes() for name, path in paths.items()}
    plan = work_item / "plan.md"
    plan.write_text(plan.read_text() + "\n新增处理说明。\n", encoding="utf-8")
    original = artifact_module._replace_with_retry

    def fail_replace(source, destination):
        if destination.name == target:
            raise PermissionError("publication replace unavailable")
        return original(source, destination)

    if target == "design-check-publication.pending.json":
        original_link = artifact_module.os.link

        def fail_link(source, destination):
            if Path(destination).name == target:
                raise PermissionError("publication journal unavailable")
            return original_link(source, destination)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(artifact_module, "_replace_with_retry", fail_replace)
        if target == "design-check-publication.pending.json":
            interrupted.setattr(artifact_module.os, "link", fail_link)
        result = check_design_contract_loop(options)
    assert result.status == "blocked"
    assert f"--loop-id {options.loop_id}" in result.next_action
    if target == "design-check-publication.pending.json":
        assert {name: path.read_bytes() for name, path in paths.items()} == before
    else:
        assert paths[target].read_bytes() == before[target]
    assert check_design_contract_loop(options).status == "ready"


@pytest.mark.parametrize(
    "target", ["design-contract-input.json", "current-design-contract.json"]
)
def test_design_publication_recovery_can_resume_after_second_interruption(
    tmp_path, monkeypatch, target
):
    options, artifacts, _, _, pending = _interrupted_publication_case(
        tmp_path,
        monkeypatch,
        initial=target == "current-design-contract.json",
        target="coverage-matrix.json"
        if target == "design-contract-input.json"
        else "current-design-contract.json",
    )
    raw = pending.read_bytes()
    original = LoopArtifactStore.write_bytes_artifact

    def interrupt(self, path, content, **kwargs):
        if path.name == target:
            raise OSError("recovery interrupted")
        return original(self, path, content, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(LoopArtifactStore, "write_bytes_artifact", interrupt)
        assert check_design_contract_loop(options).status == "blocked"
    assert pending.read_bytes() == raw
    assert check_design_contract_loop(options).status == "ready"
    assert not pending.exists()
    current = LoopRun.model_validate_json(artifacts.loop_run_path.read_bytes())
    current_input = DesignContractInput.model_validate_json(
        artifacts.input_path.read_bytes()
    )
    assert (
        current.input_digest
        == design_contract_loop_module.design_contract_input_digest(current_input)
    )


@pytest.mark.parametrize("later_stage", ["ready", "needs-fix", "closed", "quantified-closed"])
def test_design_publication_recovers_after_later_published_loop(
    initialized_project_dir, monkeypatch, later_stage
):
    import base64
    from dataclasses import replace

    from tests.integration import test_stage_quantified_pipeline as stage

    root = initialized_project_dir
    if later_stage == "quantified-closed":
        stage._ready_project(root)
    options, artifacts, _, _, pending = _interrupted_publication_case(
        root, monkeypatch, initial=True, target="archive"
    )
    original = pending.read_bytes()
    later = replace(options, loop_id="dc-second")
    if later_stage == "quantified-closed":
        later = replace(
            later, decision_mode="adaptive-quantified",
            decision_capability=stage.CAPABILITY,
        )
    tasks = root / options.work_item / "tasks.md"
    original_tasks = tasks.read_bytes()
    if later_stage == "needs-fix":
        tasks.write_bytes(original_tasks.replace(b"FR-DEMO-001", b"FR-UNKNOWN-001"))
    if later_stage == "quantified-closed":
        published_status = stage._payload(stage._cli(
            root, "loop", "design-contract", "check", "--wi", later.work_item,
            "--loop-id", later.loop_id, "--decision-mode", later.decision_mode,
            "--decision-capability", later.decision_capability, "--json",
        ))["status"]
    else:
        published_status = check_design_contract_loop(later).status
    assert published_status == ("needs_fix" if later_stage == "needs-fix" else "ready")
    if later_stage == "needs-fix":
        tasks.write_bytes(original_tasks)
    later_artifacts = design_contract_loop_module.design_contract_artifacts(
        root, later.loop_id
    )
    initial_run = later_artifacts.loop_run_path.read_bytes()
    if later_stage == "quantified-closed":
        # 仅参数化既有测试驱动的目标，begin、判断和关闭仍走原生入口。
        with monkeypatch.context() as target:
            target.setattr(stage, "LOOP", later.loop_id)
            target.setattr(stage, "WORK_ITEM", later.work_item)
            stage.stage_selected(root, "design-contract", start=False)
            stage.stage_apply(root, "design-contract", {
                "operation": "seal-for-review", "request_id": "seal"
            })
            reviewed = stage._payload(stage._cli(
                root, "loop", "review", "--type", "design-contract",
                "--loop-id", later.loop_id, "--json",
            ))
            assert stage.actual_record(root, "design-contract", reviewed)["status"] == "passed"
            closed = stage._cli(
                root, "loop", "design-contract", "close", "--loop-id", later.loop_id,
                "--expect-review-digest", reviewed["input_digest"], "--yes", "--json",
            )
            assert closed.returncode == 0, closed.stdout + closed.stderr
    elif later_stage == "closed":
        assert close_design_contract_loop(DesignContractCloseOptions(
            root=root, loop_id=later.loop_id, yes=True,
        )).closed
    if later_stage in {"closed", "quantified-closed"}:
        assert later_artifacts.loop_run_path.read_bytes() != initial_run
    later_bytes = {
        path: path.read_bytes()
        for path in later_artifacts.loop_dir.rglob("*") if path.is_file()
    }
    later_pointer = artifacts.pointer_path.read_bytes()
    recovery = design_contract_loop_module._recover_design_publication
    observed = []

    def observe_recovery(recovery_root, recovery_artifacts):
        recovery(recovery_root, recovery_artifacts)
        observed.append(artifacts.pointer_path.read_bytes())
        assert {path: path.read_bytes() for path in later_bytes} == later_bytes

    with monkeypatch.context() as observed_recovery:
        observed_recovery.setattr(
            design_contract_loop_module, "_recover_design_publication", observe_recovery
        )
        result = check_design_contract_loop(options)
    assert result.status == "ready", result
    assert observed == [later_pointer]
    assert not pending.exists()
    archives = artifacts.loop_dir / "design-check-publications"
    assert (archives / (hashlib.sha256(original).hexdigest() + ".json")).read_bytes() == original
    assert json.loads(artifacts.pointer_path.read_bytes())["loop_id"] == options.loop_id
    assert any(
        (old := json.loads(path.read_bytes())["entries"][artifacts.pointer_path.name]["old"])
        and base64.b64decode(old["base64"]) == later_pointer
        for path in archives.glob("*.json")
    )
    resolve_review_input(
        root=root, loop_type="design-contract", loop_id=options.loop_id,
        review_round_number=1,
    )
    assert close_design_contract_loop(DesignContractCloseOptions(
        root=root, loop_id=options.loop_id, yes=True,
    )).closed
    assert design_contract_loop_module.read_loop_run(
        artifacts.loop_run_path, root=root
    ).status == "closed"
    assert artifacts.close_path.is_file()
    assert {path: path.read_bytes() for path in later_bytes} == later_bytes


@pytest.mark.parametrize("damage", [
    "missing-archive", "damaged-archive", "missing-input", "run-identity",
    "same-loop", "noncanonical-pointer", "pending-publication",
])
def test_design_publication_rejects_unproven_later_pointer(
    tmp_path, monkeypatch, damage
):
    from dataclasses import replace

    options, artifacts, _, _, pending = _interrupted_publication_case(
        tmp_path, monkeypatch, initial=True, target="archive"
    )
    later = replace(options, loop_id="dc-second")
    if damage == "pending-publication":
        write = LoopArtifactStore.write_bytes_artifact

        def interrupt_later_archive(self, path, content, **kwargs):
            if (
                path.parent.name == "design-check-publications"
                and path.parent.parent.name == later.loop_id
            ):
                raise OSError("later publication archive interrupted")
            return write(self, path, content, **kwargs)

        with monkeypatch.context() as interrupted:
            interrupted.setattr(LoopArtifactStore, "write_bytes_artifact", interrupt_later_archive)
            assert check_design_contract_loop(later).status == "blocked"
    else:
        assert check_design_contract_loop(later).status == "ready"
    later_artifacts = design_contract_loop_module.design_contract_artifacts(
        tmp_path, later.loop_id
    )
    if damage in {"missing-archive", "damaged-archive"}:
        archive = next((later_artifacts.loop_dir / "design-check-publications").glob("*.json"))
    if damage == "missing-archive":
        archive.unlink()
    elif damage == "damaged-archive":
        archive.write_bytes(archive.read_bytes() + b" ")
    elif damage == "missing-input":
        later_artifacts.input_path.unlink()
    elif damage == "run-identity":
        run = json.loads(later_artifacts.loop_run_path.read_bytes())
        run["input_digest"] = "sha256:" + "0" * 64
        later_artifacts.loop_run_path.write_text(json.dumps(run), encoding="utf-8")
    elif damage in {"same-loop", "noncanonical-pointer"}:
        pointer = json.loads(artifacts.pointer_path.read_bytes())
        if damage == "same-loop":
            pointer["loop_id"] = options.loop_id
            pointer["loop_run_path"] = str(artifacts.loop_run_path.relative_to(tmp_path))
            pointer["created_at"] = "2030-01-01T00:00:00Z"
        else:
            pointer["loop_run_path"] = "other/loop-run.json"
        artifacts.pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    before = {
        path: path.read_bytes() for path in (tmp_path / ".ai-sdlc").rglob("*")
        if path.is_file()
    }
    result = check_design_contract_loop(options)
    assert result.status == "blocked", result
    assert "publication" in result.blocker
    if damage == "pending-publication":
        assert "later publication pending" in result.blocker
        assert (later_artifacts.loop_dir / design_contract_loop_module.DESIGN_CHECK_PENDING).is_file()
    assert pending.is_file()
    assert {path: path.read_bytes() for path in before} == before
    assert set(path for path in (tmp_path / ".ai-sdlc").rglob("*") if path.is_file()) == set(before)


def test_design_publication_preserves_pointer_changed_during_recovery(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    options, artifacts, _, _, pending = _interrupted_publication_case(
        tmp_path, monkeypatch, initial=True, target="archive"
    )
    original = pending.read_bytes()
    assert check_design_contract_loop(replace(options, loop_id="dc-second")).status == "ready"
    write = LoopArtifactStore.write_bytes_artifact
    replacement = []

    def publish_third(self, path, content, **kwargs):
        result = write(self, path, content, **kwargs)
        if path.parent == artifacts.loop_dir / "design-check-publications":
            third = check_design_contract_loop(replace(options, loop_id="dc-third"))
            assert third.status == "ready", third
            replacement.append(artifacts.pointer_path.read_bytes())
        return result

    with monkeypatch.context() as interrupted:
        interrupted.setattr(LoopArtifactStore, "write_bytes_artifact", publish_third)
        result = check_design_contract_loop(options)
    assert result.status == "blocked", result
    assert "publication" in result.blocker
    assert len(replacement) == 1
    assert artifacts.pointer_path.read_bytes() == replacement[0]
    assert pending.read_bytes() == original
    assert check_design_contract_loop(options).status == "ready"
    assert not pending.exists()


def test_design_publication_equal_commit_bytes_choose_exact_old_state(
    tmp_path, monkeypatch
):
    options, artifacts, paths, before, pending = _interrupted_publication_case(
        tmp_path, monkeypatch
    )
    journal = json.loads(pending.read_bytes())
    for name in ("loop-run.json", "design-contract-input.json"):
        journal["entries"][name]["new"] = journal["entries"][name]["old"]
        paths[name].write_bytes(before[name])
    pending.write_text(json.dumps(journal), encoding="utf-8")
    with design_contract_loop_module._stage_write_guard(
        tmp_path, "design-contract", options.loop_id
    ):
        design_contract_loop_module._recover_design_publication(tmp_path, artifacts)
    assert {name: path.read_bytes() for name, path in paths.items()} == before
    assert not pending.exists()


@pytest.mark.parametrize("intervening_publication", [False, True])
def test_quantified_design_recovery_preserves_r1_costs_and_completes_original_r2_close(
    initialized_project_dir, monkeypatch, intervening_publication
):
    from tests.integration.test_stage_quantified_pipeline import (
        CAPABILITY,
        LOOP,
        WORK_ITEM,
        _cli,
        _payload,
        actual_record,
        stage_apply,
        stage_selected,
    )

    root = initialized_project_dir
    stage_selected(root, "design-contract", repair_fact=True)
    stage_apply(
        root, "design-contract", {"operation": "seal-for-review", "request_id": "seal"}
    )
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "design-contract",
            "--loop-id",
            LOOP,
            "--json",
        )
    )
    assert (
        actual_record(root, "design-contract", reviewed, status="UNKNOWN")["status"]
        == "needs_fix"
    )
    directory = root / ".ai-sdlc/loops/design-contract" / LOOP
    history = {
        name: (directory / name).read_bytes()
        for name in ("decision-context.json", "review-outcome-round-1.json")
    }
    old_run = LoopRun.model_validate_json((directory / "loop-run.json").read_bytes())
    (root / "repair-fact.md").write_text("R1 后补充实际处理说明。\n", encoding="utf-8")
    plan = root / WORK_ITEM / "plan.md"
    plan.write_text(plan.read_text() + "\n补充原范围内的处理说明。\n", encoding="utf-8")
    options = DesignContractCheckOptions(
        root=root,
        work_item=WORK_ITEM,
        loop_id=LOOP,
        decision_mode="adaptive-quantified",
        decision_capability=CAPABILITY,
    )
    original = LoopArtifactStore.write_bytes_artifact

    def interrupt(self, path, content, **kwargs):
        if (
            path.parent.name == "design-check-publications"
            if intervening_publication else path.name == "loop-run.json"
        ):
            raise OSError("quantified design publication interrupted")
        return original(self, path, content, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(LoopArtifactStore, "write_bytes_artifact", interrupt)
        failed = check_design_contract_loop(options)
        assert failed.status == "blocked"
    if intervening_publication:
        later = check_design_contract_loop(DesignContractCheckOptions(
            root=root, work_item=WORK_ITEM, loop_id="dc-intervening",
        ))
        assert later.status == "ready", later
    command = shlex.split(
        failed.next_action.removeprefix("Run ").removesuffix(
            " to recover this publication."
        )
    )
    assert "adaptive-quantified" in command and CAPABILITY in command
    assert _payload(_cli(root, *command[1:], "--json"))["status"] == "ready"
    assert {name: (directory / name).read_bytes() for name in history} == history
    current_run = LoopRun.model_validate_json(
        (directory / "loop-run.json").read_bytes()
    )
    assert current_run.current_round == 2
    assert current_run.created_at == old_run.created_at
    second = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "design-contract",
            "--loop-id",
            LOOP,
            "--json",
        )
    )
    assert second["round_number"] == 2
    assert actual_record(root, "design-contract", second)["status"] == "passed"
    close = _cli(
        root,
        "loop",
        "design-contract",
        "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        second["input_digest"],
        "--yes",
        "--json",
    )
    assert close.returncode == 0, close.stdout + close.stderr
    assert (directory / "review-outcome-round-1.json").read_bytes() == history[
        "review-outcome-round-1.json"
    ]
    assert not (directory / "review-outcome-round-3.json").exists()


@pytest.mark.parametrize(
    "pending_id,blocked",
    [
        (None, False),
        ("design-upstream", True),
        ("impl-current", False),
        ("empty-upstream", False),
    ],
)
def test_design_publication_guard_uses_implementation_upstream_identity(
    tmp_path, pending_id, blocked
):
    from ai_sdlc.core.design_contract_store import (
        DESIGN_CHECK_PENDING,
        read_verification_contract,
    )
    from ai_sdlc.core.implementation_models import ImplementationInput

    impl = ImplementationInput(
        loop_id="impl-current",
        work_item_id="demo",
        work_item_path="specs/demo",
        spec_path="specs/demo/spec.md",
        plan_path="specs/demo/plan.md",
        tasks_path="specs/demo/tasks.md",
        design_contract_loop_id=""
        if pending_id == "empty-upstream"
        else "design-upstream",
    )
    if pending_id and pending_id != "empty-upstream":
        pending = (
            design_contract_loop_module.design_contract_artifacts(
                tmp_path, pending_id
            ).loop_dir
            / DESIGN_CHECK_PENDING
        )
        pending.parent.mkdir(parents=True)
        pending.write_text("{}", encoding="utf-8")
    if blocked:
        with pytest.raises(ValueError, match="publication-pending"):
            read_verification_contract(tmp_path, impl)
    else:
        assert read_verification_contract(tmp_path, impl) == (None, {})


def test_quantified_design_next_keeps_options_when_pending_creation_fails(
    tmp_path, monkeypatch
):
    _write_work_item(tmp_path)
    options = DesignContractCheckOptions(
        root=tmp_path,
        work_item="specs/demo-contract",
        loop_id="dc-quantified-first-write",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    original = LoopArtifactStore.write_bytes_artifact

    def interrupt(self, path, content, **kwargs):
        if path.name == design_contract_loop_module.DESIGN_CHECK_PENDING:
            raise OSError("pending not written")
        return original(self, path, content, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(LoopArtifactStore, "write_bytes_artifact", interrupt)
        result = check_design_contract_loop(options)
    assert result.status == "blocked"
    assert "publication-write-failed" in result.blocker
    assert "--decision-mode adaptive-quantified" in result.next_action
    assert "--decision-capability stage-simulation-v1" in result.next_action
    assert check_design_contract_loop(options).status == "ready"


@pytest.mark.parametrize(
    "artifact_name",
    ["design-contract-input.json", "loop-run.json"],
)
def test_check_design_contract_loop_rejects_symlinked_previous_artifact(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )
    loop_id = "dc-previous-artifact-symlink"
    first = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert first.status == "needs_fix"

    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "Cover contract docs.\n- **验证**：",
            "Cover FR-DEMO-001 and SC-DEMO-001.\n"
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
        ),
        encoding="utf-8",
    )
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    artifact = loop_dir / artifact_name
    backing = loop_dir / f"backing-{artifact_name}"
    backing.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(backing.name)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert result.status == "blocked"
    assert "previous design check artifacts are unavailable" in result.blocker


@pytest.mark.parametrize("replacement_kind", ["broken_symlink", "directory"])
def test_check_design_contract_loop_rejects_invalid_previous_loop_run(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )
    loop_id = "dc-invalid-previous-loop-run"
    first = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert first.status == "needs_fix"

    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "Cover contract docs.\n- **验证**：",
            "Cover FR-DEMO-001 and SC-DEMO-001.\n"
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
        ),
        encoding="utf-8",
    )
    loop_run = (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id / "loop-run.json"
    )
    loop_run.unlink()
    if replacement_kind == "broken_symlink":
        loop_run.symlink_to("missing-loop-run.json")
    else:
        loop_run.mkdir()

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert result.status == "blocked"
    assert "previous design check artifacts are unavailable" in result.blocker


def test_close_design_contract_loop_rejects_symlinked_loop_run(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-close-loop-run-symlink"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"

    loop_run = (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id / "loop-run.json"
    )
    backing = loop_run.with_name("backing-loop-run.json")
    backing.write_bytes(loop_run.read_bytes())
    loop_run.unlink()
    loop_run.symlink_to(backing.name)

    closed = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id=loop_id,
            yes=True,
        )
    )
    assert closed.status == "blocked"
    assert closed.closed is False
    assert "uses a symlink" in closed.blocker


@pytest.mark.parametrize(
    "replacement_kind",
    ["valid_symlink", "broken_symlink", "directory"],
)
def test_closed_design_contract_recheck_rejects_untrusted_close_artifact(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-closed-untrusted-close"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"
    closed = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )
    assert closed.closed is True
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    backing = loop_dir / "backing-design-contract-close.json"
    if replacement_kind == "valid_symlink":
        backing.write_bytes(close_path.read_bytes())
    close_path.unlink()
    if replacement_kind == "directory":
        close_path.mkdir()
    else:
        target = backing.name if replacement_kind == "valid_symlink" else "missing.json"
        close_path.symlink_to(target)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )

    assert result.status == "blocked"
    assert "closed design-contract artifact is unavailable" in result.blocker
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_closed_design_contract_recheck_rejects_fifo_close_artifact(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-closed-fifo-close"
    assert (
        check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-contract",
                loop_id=loop_id,
            )
        ).status
        == "ready"
    )
    assert close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    ).closed
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    close_path.unlink()
    os.mkfifo(close_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )

    assert result.status == "blocked"
    assert "closed design-contract artifact is unavailable" in result.blocker
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_current_closed_recheck_rejects_untrusted_close_artifact(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    loop_id = "dc-current-closed-untrusted-close"
    assert (
        check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-contract",
                loop_id=loop_id,
            )
        ).status
        == "ready"
    )
    assert close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    ).closed
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    close_path.unlink()
    close_path.symlink_to("missing-close.json")

    result = check_design_contract_loop(
        DesignContractCheckOptions(root=tmp_path, work_item=str(work_item))
    )

    assert result.status == "blocked"
    assert "closed design-contract artifact is unavailable" in result.blocker
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_check_design_contract_loop_dry_run_does_not_write(tmp_path: Path) -> None:
    _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-dry-run",
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.loop_status == "created"
    assert result.design_contract is not None
    assert result.design_contract.status == "created"
    assert result.design_contract.work_item_id == "demo-contract"
    assert result.design_contract.coverage_matrix_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-dry-run/coverage-matrix.json"
    )
    assert result.design_contract.report_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-dry-run/design-contract-report.json"
    )
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-dry-run"
    ).exists()
    assert any(artifact.kind == "loop-run" for artifact in result.artifacts)


def test_check_design_contract_loop_blocks_missing_current_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, with_frozen_requirement=False)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-missing-current-requirement",
        )
    )

    assert result.status == "blocked"
    assert "frozen current requirement loop is required" in result.blocker
    assert "No current requirement loop exists" in result.blocker
    assert result.next_action == "Run ai-sdlc loop requirement start."
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-missing-current-requirement"
    ).exists()


def test_check_design_contract_loop_uses_current_frozen_requirement_by_default(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, requirement_loop_id="req-default-frozen")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-current-requirement",
        )
    )

    assert result.status == "ready"
    input_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-current-requirement"
            / "design-contract-input.json"
        ).read_text(encoding="utf-8")
    )
    assert input_payload["requirement_loop_id"] == "req-default-frozen"


def test_check_design_contract_loop_blocks_unfrozen_current_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, with_frozen_requirement=False)
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-current-unfrozen",
            idea="Demo users need a design contract.",
            acceptance=("Design contract can be checked.",),
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-unfrozen-current-requirement",
        )
    )

    assert result.status == "blocked"
    assert result.blocker == (
        "Requirement loop req-current-unfrozen must be frozen before "
        "design-contract check."
    )
    assert result.next_action == (
        "Run ai-sdlc loop review --type requirement --loop-id req-current-unfrozen."
    )


def test_check_design_contract_loop_blocks_missing_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-missing",
            loop_id="dc-missing-requirement",
        )
    )

    assert result.status == "blocked"
    assert "must exist and be frozen" in result.blocker
    assert result.next_action == "Run ai-sdlc loop requirement start."
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-missing-requirement"
    ).exists()


def test_check_design_contract_loop_blocks_unfrozen_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="需要设计合同前置验证",
            acceptance=("需求可被冻结",),
            loop_id="req-unfrozen",
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-unfrozen",
            loop_id="dc-unfrozen-requirement",
        )
    )

    assert result.status == "blocked"
    assert result.blocker == (
        "Requirement loop req-unfrozen must be frozen before design-contract check."
    )
    assert (
        result.next_action
        == "Run ai-sdlc loop review --type requirement --loop-id req-unfrozen."
    )


def test_check_design_contract_loop_accepts_frozen_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    start_result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="需要设计合同前置验证",
            acceptance=("需求可被冻结",),
            loop_id="req-frozen",
        )
    )
    assert start_result.status == "ready"
    freeze_result = freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, loop_id="req-frozen", yes=True)
    )
    assert freeze_result.status == "ready"
    assert freeze_result.frozen is True

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-frozen",
            loop_id="dc-frozen-requirement",
        )
    )

    assert result.status == "ready"
    input_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-frozen-requirement"
            / "design-contract-input.json"
        ).read_text(encoding="utf-8")
    )
    assert input_payload["requirement_loop_id"] == "req-frozen"


def test_check_design_contract_loop_blocks_mismatched_requirement_work_item(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, with_frozen_requirement=False)
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-other-work-item",
            idea="Other work item needs a design contract.",
            acceptance=("Other work item can be checked.",),
            work_item_id="other-contract",
        )
    )
    freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, loop_id="req-other-work-item", yes=True)
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-other-work-item",
            loop_id="dc-mismatched-requirement",
        )
    )

    assert result.status == "blocked"
    assert result.blocker == (
        "Requirement loop req-other-work-item belongs to work item other-contract, "
        "but design-contract work item is demo-contract."
    )
    assert "--work-item-id demo-contract" in result.next_action


def test_check_design_contract_loop_reports_missing_coverage(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, include_task_refs=False, verification_value="")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-needs-fix",
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count >= 2
    assert "Fix design-contract blockers" in result.next_action

    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-needs-fix"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "missing_coverage" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_infers_generated_task_coverage(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, include_task_refs=False)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-inferred-coverage",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-inferred-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"covered"}
    assert {
        item["source_id"]: item["covered_by"] for item in report["coverage_items"]
    } == {
        "FR-DEMO-001": ["T11"],
        "SC-DEMO-001": ["T11"],
    }


def test_check_design_contract_loop_ignores_non_task_coverage_refs(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
        tasks_intro_extra="\n".join(
            [
                "## Deferred notes",
                "",
                "FR-DEMO-001 and SC-DEMO-001 are mentioned outside executable tasks.",
                "",
                "```markdown",
                "FR-DEMO-001 SC-DEMO-001",
                "```",
                "",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-non-task-coverage",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-non-task-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"missing"}
    assert all(item["covered_by"] == [] for item in report["coverage_items"])
    assert {finding["source_id"] for finding in report["findings"]} >= {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }


def test_check_design_contract_loop_ignores_trailing_non_task_coverage_refs(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
        tasks_tail_extra="\n".join(
            [
                "",
                "## Coverage matrix",
                "",
                "- FR-DEMO-001",
                "- SC-DEMO-001",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-trailing-coverage",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-trailing-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"missing"}
    assert all(item["covered_by"] == [] for item in report["coverage_items"])


def test_check_design_contract_loop_reports_placeholders(tmp_path: Path) -> None:
    _write_work_item(tmp_path, placeholder=True)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-placeholder",
        )
    )

    assert result.status == "needs_fix"
    assert result.blocker_count >= 1
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-placeholder"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "placeholder" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_accepts_filled_feature_spec_title(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, spec_title="# 功能规格：Frontend Program Demo")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-filled-feature-title",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_accepts_direct_formal_as_product_term(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        spec_title="# 功能规格：Direct Formal Work Item",
        spec_intro_extra="本功能延续 direct-formal work item 入口。",
        plan_extra="direct-formal 是本次合同覆盖的正常产品术语。",
        tasks_intro_extra="direct-formal 相关任务必须仍可进入合同检查。",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-direct-formal-term",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_accepts_english_plan_sections(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    (work_item / "plan.md").write_text(
        "\n".join(
            [
                "# Implementation Plan",
                "",
                "## Technical Context",
                "Python runtime.",
                "## Phase Plan",
                "Phase 1.",
                "## Verification",
                "Run pytest.",
                "## Rollback",
                "Revert the commit.",
            ]
        ),
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-english-plan",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_reports_unrendered_feature_spec_title(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, spec_title="# 功能规格：{{ project_name }}")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-template-feature-title",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-template-feature-title"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "placeholder" in {finding["code"] for finding in report["findings"]}


@pytest.mark.parametrize(
    "status_line",
    [
        "**状态**: 草稿",
        "**Status**: Draft",
    ],
)
def test_check_design_contract_loop_blocks_draft_status_variants(
    tmp_path: Path,
    status_line: str,
) -> None:
    _write_work_item(tmp_path, spec_status_line=status_line)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-draft-status",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-draft-status"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "draft_spec" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_ignores_example_contract_ids(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        spec_intro_extra="\n".join(
            [
                "## 用户故事与示例",
                "",
                "**独立测试**：构造 `FR-EXAMPLE-001` 和 `SC-EXAMPLE-001`。",
                "",
                "```markdown",
                "- **FR-CODE-001**：代码块中的编号不能成为合同项。",
                "```",
                "",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-ignore-examples",
        )
    )

    assert result.status == "ready"
    assert result.coverage_count == 2
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-ignore-examples"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["source_id"] for item in report["coverage_items"]} == {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }


def test_check_design_contract_loop_treats_exit_criteria_as_contract_section(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, success_heading="## Exit Criteria")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-exit-criteria",
        )
    )

    assert result.status == "ready"
    assert result.coverage_count == 2
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-exit-criteria"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["source_id"] for item in report["coverage_items"]} == {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }


def test_check_design_contract_loop_blocks_unparseable_task_sections(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, task_heading="### 工作 1.1 Check contract")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-task-sections",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-task-sections"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_section_gap" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_accepts_generated_chinese_task_sections(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, task_heading="### 任务 1.1 Check contract")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-chinese-task-section",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_accepts_english_task_labels(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        acceptance_label="- **Acceptance Criteria**",
        verification_label="- **Verification**",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-english-task-labels",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_ignores_p2_task_detail_gaps(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        extra_task_sections="\n".join(
            [
                "",
                "### Task 2.1 Deferred polish",
                "",
                "- **任务编号**：T12",
                "- **优先级**：P2",
                "- Backlog note without acceptance or verification details.",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-p2-task-gap",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-p2-task-gap"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_acceptance_gap" not in {
        finding["code"] for finding in report["findings"]
    }
    assert "task_verification_gap" not in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_ignores_p2_task_contract_coverage(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
        extra_task_sections="\n".join(
            [
                "",
                "### Task 2.1 Deferred coverage note",
                "",
                "- **任务编号**：T12",
                "- **优先级**：P2",
                "- Deferred backlog mentions FR-DEMO-001 and SC-DEMO-001.",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-p2-coverage",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-p2-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"missing"}
    assert all(item["covered_by"] == [] for item in report["coverage_items"])
    assert "missing_coverage" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_requires_verification_command(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, verification_value="")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-empty-verification",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-empty-verification"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_verification_gap" in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_accepts_canonical_verify_label(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        verification_label="- verify",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-canonical-verify-label",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-canonical-verify-label"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_verification_gap" not in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_checks_plan_scope_drift(tmp_path: Path) -> None:
    _write_work_item(tmp_path, plan_extra="Touch implementation_loop.py.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-plan-drift",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-plan-drift"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    scope_findings = [
        finding for finding in report["findings"] if finding["code"] == "scope_drift"
    ]
    assert scope_findings
    assert scope_findings[0]["path"] == "specs/demo-contract/plan.md"


def test_check_design_contract_loop_detects_case_variant_scope_drift(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, plan_extra="Touch IMPLEMENTATION_LOOP.PY.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-case-variant-drift",
        )
    )

    assert result.status == "needs_fix"


def test_check_design_contract_loop_checks_local_review_scope_drift(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, plan_extra="Run ai-sdlc pr-review start.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-local-review-drift",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-local-review-drift"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    scope_findings = [
        finding for finding in report["findings"] if finding["code"] == "scope_drift"
    ]
    assert scope_findings
    assert "ai-sdlc pr-review" in scope_findings[0]["message"]


def test_check_design_contract_loop_checks_frontend_command_scope_drift(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, plan_extra="Run ai-sdlc loop frontend-evidence check.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-frontend-command-drift",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-frontend-command-drift"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    scope_findings = [
        finding for finding in report["findings"] if finding["code"] == "scope_drift"
    ]
    assert scope_findings
    assert "ai-sdlc loop frontend-evidence" in scope_findings[0]["message"]


def test_check_design_contract_loop_rejects_scope_inferred_only_from_work_item_path(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        relative_path="specs/implementation-loop-runtime",
        plan_extra="Run ai-sdlc loop implementation check and touch implementation_loop.py.",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/implementation-loop-runtime",
            loop_id="dc-active-scope",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-active-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_drift" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_allows_scope_authorized_by_frozen_requirement(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        requirement_scope_families=(
            "implementation",
            "frontend-evidence",
            "pr-review",
        ),
        plan_extra="\n".join(
            [
                "Touch implementation_loop.py.",
                "Touch frontend_evidence_loop.py.",
                "Touch pr_review_service.py.",
            ]
        ),
    )
    spec_path = work_item / "spec.md"
    spec_path.write_text(
        "---\n"
        "design_scope_families:\n"
        "  - implementation\n"
        "  - frontend-evidence\n"
        "  - pr-review\n"
        "---\n"
        f"{spec_path.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-declared-scope",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-declared-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_drift" not in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_uses_the_validated_requirement_scope_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(
        tmp_path,
        requirement_scope_families=("implementation",),
    )
    original_read = LoopArtifactStore.read_json_artifact
    intake_reads = 0

    def counted_read(store: LoopArtifactStore, path: Path) -> object:
        nonlocal intake_reads
        if path.name == "requirement-intake.json":
            intake_reads += 1
        return original_read(store, path)

    monkeypatch.setattr(LoopArtifactStore, "read_json_artifact", counted_read)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-single-authority-snapshot",
        )
    )

    assert result.status == "ready"
    assert intake_reads == 1


def test_check_design_contract_loop_rejects_scope_self_authorized_by_spec(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        plan_extra="Touch implementation_loop.py.",
    )
    spec_path = work_item / "spec.md"
    spec_path.write_text(
        "---\n"
        "design_scope_families:\n"
        "  - implementation\n"
        "---\n"
        f"{spec_path.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-self-authorized-scope",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-self-authorized-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_authority_missing" in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_blocks_mutated_frozen_requirement(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        requirement_scope_families=("implementation",),
    )
    intake_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-current"
        / "requirement-intake.json"
    )
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    intake["design_scope_families"].append("pr-review")
    intake_path.write_text(json.dumps(intake), encoding="utf-8")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-mutated-scope-authority",
        )
    )

    assert result.status == "blocked"
    assert "changed after freeze" in result.blocker


def test_close_design_contract_loop_blocks_changed_frozen_requirement_scope(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        requirement_scope_families=("implementation",),
    )
    check = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-refrozen-scope-authority",
        )
    )
    assert check.status == "ready"
    requirement_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-current"
    intake_path = requirement_dir / "requirement-intake.json"
    intake_payload = json.loads(intake_path.read_text(encoding="utf-8"))
    intake_payload["design_scope_families"].append("pr-review")
    intake = RequirementIntake.model_validate(intake_payload)
    intake_path.write_text(
        json.dumps(intake.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    freeze_path = requirement_dir / "requirement-freeze.json"
    freeze_payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze_payload["intake_digest"] = _requirement_intake_digest(intake)
    freeze_path.write_text(json.dumps(freeze_payload, indent=2), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-refrozen-scope-authority",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "Frozen requirement scope changed" in result.blocker


def test_close_design_contract_loop_blocks_changed_checked_input(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-cleared-scope-authority",
        )
    )
    assert check.status == "ready"
    input_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-cleared-scope-authority"
        / "design-contract-input.json"
    )
    input_payload = json.loads(input_path.read_text(encoding="utf-8"))
    input_payload["authorized_scope_families"] = ["implementation"]
    input_payload["scope_authority_ref"] = ""
    input_payload["scope_authority_digest"] = ""
    input_path.write_text(json.dumps(input_payload, indent=2), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-cleared-scope-authority",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "input changed after check" in result.blocker


def test_close_design_contract_loop_blocks_swapped_requirement_input(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-swapped-scope-authority",
        )
    )
    assert check.status == "ready"
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-alternate",
            work_item_id="demo-contract",
            idea="Authorize implementation design scope.",
            acceptance=("Design contract can be checked.",),
            design_scope_families=("implementation",),
        )
    )
    frozen = freeze_requirement_loop(
        RequirementFreezeOptions(
            root=tmp_path,
            loop_id="req-alternate",
            yes=True,
        )
    )
    assert frozen.status == "ready"
    alternate_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-alternate"
    alternate_freeze = json.loads(
        (alternate_dir / "requirement-freeze.json").read_text(encoding="utf-8")
    )
    input_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-swapped-scope-authority"
        / "design-contract-input.json"
    )
    input_payload = json.loads(input_path.read_text(encoding="utf-8"))
    input_payload["requirement_loop_id"] = "req-alternate"
    input_payload["authorized_scope_families"] = ["implementation"]
    input_payload["scope_authority_ref"] = (
        ".ai-sdlc/loops/requirement/req-alternate/requirement-intake.json"
    )
    input_payload["scope_authority_digest"] = alternate_freeze["intake_digest"]
    input_path.write_text(json.dumps(input_payload, indent=2), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-swapped-scope-authority",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "input changed after check" in result.blocker


def test_check_design_contract_loop_rejects_scope_mentioned_only_as_a_non_goal(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        spec_intro_extra=(
            "Non-goal: never touch implementation_loop.py, "
            "frontend_evidence_loop.py, or pr_review_service.py."
        ),
        plan_extra="Touch implementation_loop.py.",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-non-goal-does-not-authorize-scope",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-non-goal-does-not-authorize-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_drift" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_blocks_non_canonical_work_item_dir(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        relative_path="other/demo-contract",
        with_frozen_requirement=False,
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="other/demo-contract",
            loop_id="dc-non-canonical",
        )
    )

    assert result.status == "blocked"
    assert "canonical specs/<work-item>" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


def test_close_design_contract_loop_writes_close_artifact(tmp_path: Path) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-close",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-close", yes=True)
    )

    assert result.status == "ready"
    assert result.loop_status == "closed"
    assert result.closed is True
    assert result.design_contract is not None
    assert result.design_contract.status == "closed"
    assert result.next_action == "Start implementation loop for demo-contract."
    assert result.next_guidance.safety == "no_action"
    assert result.next_guidance.writes_artifacts is False

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-close"
    assert (loop_dir / "design-contract-close.json").is_file()
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_close_design_contract_loop_rechecks_review_digest_at_final_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-final-review-guard"
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="design-contract",
        loop_id=loop_id,
        review_round_number=1,
    )
    outcome = LoopReviewOutcome(
        loop_id=loop_id,
        loop_type="design-contract",
        round_number=1,
        input_digest=reviewed.input_digest,
        status="completed",
        expert_roles=reviewed.expert_roles,
        findings=[],
        recorded_at="2026-08-17T00:00:00Z",
    )
    (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / loop_id
        / "review-outcome-round-1.json"
    ).write_text(outcome.model_dump_json(indent=2) + "\n", encoding="utf-8")
    original_refresh = design_contract_loop_module._refresh_report_before_close

    def mutate_after_state_validation(*args: object, **kwargs: object) -> object:
        result = original_refresh(*args, **kwargs)
        report_path = (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / loop_id
            / "design-contract-report.md"
        )
        report_path.write_text(
            report_path.read_text(encoding="utf-8") + "\n评审后发生变化。\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(
        design_contract_loop_module,
        "_refresh_report_before_close",
        mutate_after_state_validation,
    )

    with pytest.raises(ReviewInputGuardError, match="review-input-drift"):
        close_design_contract_loop(
            DesignContractCloseOptions(
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
        / "design-contract"
        / loop_id
        / "design-contract-close.json"
    ).exists()


def test_close_design_contract_loop_preserves_unchanged_review_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-unchanged-review-guard"
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="design-contract",
        loop_id=loop_id,
        review_round_number=1,
    )
    outcome = LoopReviewOutcome(
        loop_id=loop_id,
        loop_type="design-contract",
        round_number=1,
        input_digest=reviewed.input_digest,
        status="completed",
        expert_roles=reviewed.expert_roles,
        findings=[],
        recorded_at="2026-08-17T00:00:00Z",
    )
    (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / loop_id
        / "review-outcome-round-1.json"
    ).write_text(outcome.model_dump_json(indent=2) + "\n", encoding="utf-8")
    refresh_persistence: list[bool] = []
    original_refresh = design_contract_loop_module._refresh_report_before_close

    def record_refresh_persistence(*args: object, **kwargs: object) -> object:
        refresh_persistence.append(bool(kwargs["persist"]))
        return original_refresh(*args, **kwargs)

    monkeypatch.setattr(
        design_contract_loop_module,
        "_refresh_report_before_close",
        record_refresh_persistence,
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id=loop_id,
            yes=True,
            expected_review_digest=reviewed.input_digest,
        ),
        review_input_validator=validate_review_input_for_close,
    )

    assert result.status == "ready"
    assert result.loop_status == "closed"
    assert result.closed is True
    assert refresh_persistence == [False]


def test_close_design_contract_loop_recovers_close_written_before_loop_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-close-write-recovery"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"
    original_write = LoopArtifactStore.write_json_artifact
    close_written = False

    def fail_loop_run_write(
        store: LoopArtifactStore,
        path: Path,
        payload: object,
    ) -> Path:
        nonlocal close_written
        if path.name == "design-contract-close.json":
            written = original_write(store, path, payload)
            close_written = True
            return written
        if close_written and path.name == "loop-run.json":
            raise OSError("injected loop-run write failure")
        return original_write(store, path, payload)

    monkeypatch.setattr(
        LoopArtifactStore,
        "write_json_artifact",
        fail_loop_run_write,
    )
    with pytest.raises(OSError, match="injected loop-run write failure"):
        close_design_contract_loop(
            DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
        )

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    assert (loop_dir / "design-contract-close.json").is_file()
    persisted_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert persisted_run["status"] == "needs_review"
    unchanged_artifacts = {
        name: (loop_dir / name).read_bytes()
        for name in (
            "design-contract-input.json",
            "coverage-matrix.json",
            "design-contract-report.json",
        )
    }

    monkeypatch.setattr(
        LoopArtifactStore,
        "write_json_artifact",
        original_write,
    )
    recovered = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )

    assert recovered.status == "ready", recovered.blocker
    assert recovered.closed is True
    assert recovered.loop_status == "closed"
    persisted_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert persisted_run["status"] == "closed"
    assert unchanged_artifacts == {
        name: (loop_dir / name).read_bytes() for name in unchanged_artifacts
    }


def test_close_design_contract_loop_revalidates_docs_before_partial_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-recovery-revalidates-docs"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"
    original_write = LoopArtifactStore.write_json_artifact
    close_written = False

    def fail_loop_run_write(
        store: LoopArtifactStore,
        path: Path,
        payload: object,
    ) -> Path:
        nonlocal close_written
        if path.name == "design-contract-close.json":
            written = original_write(store, path, payload)
            close_written = True
            return written
        if close_written and path.name == "loop-run.json":
            raise OSError("injected loop-run write failure")
        return original_write(store, path, payload)

    monkeypatch.setattr(LoopArtifactStore, "write_json_artifact", fail_loop_run_write)
    with pytest.raises(OSError, match="injected loop-run write failure"):
        close_design_contract_loop(
            DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
        )

    tasks_path = tmp_path / "specs" / "demo-contract" / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
            "- **验证**：",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(LoopArtifactStore, "write_json_artifact", original_write)
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    original_unlink = Path.unlink
    removed_concurrently = False

    def remove_close_before_recovery_unlink(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        nonlocal removed_concurrently
        if path == close_path and not removed_concurrently:
            removed_concurrently = True
            original_unlink(path)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", remove_close_before_recovery_unlink)

    recovered = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )

    assert recovered.status == "needs_fix"
    assert recovered.closed is False
    assert removed_concurrently is True
    assert "design-contract-close" not in {
        artifact.kind for artifact in recovered.artifacts
    }
    assert all(
        not evidence.endswith("design-contract-close.json")
        for evidence in recovered.next_guidance.evidence
    )
    persisted_run = json.loads((loop_dir / "loop-run.json").read_text("utf-8"))
    persisted_report = json.loads(
        (loop_dir / "design-contract-report.json").read_text(encoding="utf-8")
    )
    assert persisted_run["status"] == "needs_fix"
    assert persisted_report["status"] == "needs_fix"
    assert "task_verification_gap" in {
        finding["code"] for finding in persisted_report["findings"]
    }
    assert not (loop_dir / "design-contract-close.json").exists()

    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- **验证**：\n",
            "- **验证**：uv run pytest tests/unit/test_demo.py -q\n",
        ),
        encoding="utf-8",
    )
    rechecked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert rechecked.status == "ready"
    assert rechecked.loop_status == "needs_review"

    closed = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )
    assert closed.status == "ready"
    assert closed.loop_status == "closed"
    assert closed.closed is True


def test_close_design_contract_loop_revalidates_changed_docs(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-stale-close",
        )
    )
    tasks_path = tmp_path / "specs" / "demo-contract" / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
            "- **验证**：",
        ),
        encoding="utf-8",
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-stale-close", yes=True)
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.closed is False
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-stale-close"
        / "design-contract-close.json"
    ).exists()

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-stale-close"
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    report = json.loads(
        (loop_dir / "design-contract-report.json").read_text(encoding="utf-8")
    )
    assert loop_run["status"] == "needs_fix"
    assert "task_verification_gap" in {
        finding["code"] for finding in report["findings"]
    }


def test_close_design_contract_loop_repeat_close_keeps_implementation_next_action(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-repeat-close",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-repeat-close", yes=True)
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-repeat-close", yes=True)
    )

    assert result.status == "ready"
    assert result.closed is True
    assert result.loop_status == "closed"
    assert result.next_action == "Start implementation loop for demo-contract."
    assert result.next_guidance.safety == "no_action"
    assert result.next_guidance.alternatives == [
        "Start implementation loop for demo-contract."
    ]


def test_check_design_contract_loop_blocks_recheck_of_closed_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-closed-recheck",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-closed-recheck", yes=True)
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-closed-recheck",
        )
    )

    assert result.status == "blocked"
    assert "already closed" in result.blocker
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-closed-recheck"
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_check_design_contract_loop_preserves_closed_current_default_recheck(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path, loop_id=check_result.loop_id, yes=True
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
        )
    )

    assert result.status == "ready"
    assert result.closed is True
    assert result.loop_id == check_result.loop_id
    assert result.loop_status == "closed"
    assert result.next_action == "Start implementation loop for demo-contract."

    pointer = json.loads(
        (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).read_text(encoding="utf-8")
    )
    assert pointer["loop_id"] == check_result.loop_id


def test_check_design_contract_loop_dry_run_after_close_stays_preview(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path, loop_id=check_result.loop_id, yes=True
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-dry-after-close",
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.closed is False
    assert result.loop_status == "created"
    assert result.loop_id == "dc-dry-after-close"
    assert result.next_guidance.writes_artifacts is True

    pointer = json.loads(
        (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).read_text(encoding="utf-8")
    )
    assert pointer["loop_id"] == check_result.loop_id
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-dry-after-close"
    ).exists()


def test_close_design_contract_loop_requires_yes(tmp_path: Path) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-close-needs-yes",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-close-needs-yes")
    )

    assert result.status == "blocked"
    assert "Pass --yes" in result.blocker
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-close-needs-yes"
        / "design-contract-close.json"
    ).exists()


def test_close_design_contract_loop_blocks_unresolved_contract(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, include_task_refs=False, verification_value="")
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-close-blocked",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-close-blocked", yes=True)
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count >= 2


def test_close_design_contract_loop_blocks_non_current_explicit_loop_id(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-old",
        )
    )
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        relative_path="specs/current-contract",
    )
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/current-contract",
            loop_id="dc-current",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-old", yes=True)
    )

    assert result.status == "blocked"
    assert "Only the current design-contract loop can be closed" in result.blocker
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-old"
        / "design-contract-close.json"
    ).exists()


def test_close_design_contract_loop_blocks_pointer_and_run_identity_mismatch(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-identity-target",
        )
    )
    loop_run_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-identity-target"
        / "loop-run.json"
    )
    loop_run = json.loads(loop_run_path.read_text(encoding="utf-8"))
    loop_run["loop_id"] = "dc-identity-other"
    loop_run_path.write_text(json.dumps(loop_run), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-identity-target",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "loop identity" in result.blocker


def test_close_design_contract_loop_blocks_report_work_item_mismatch(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-report-identity",
        )
    )
    report_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-report-identity"
        / "design-contract-report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["work_item_id"] = "other-contract"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-report-identity",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "report identity" in result.blocker


def test_close_design_contract_loop_blocks_symlinked_current_pointer(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    outside.joinpath("loop-run.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "linked-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    pointer_path = tmp_path / CURRENT_DESIGN_CONTRACT_PATH
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "artifact_kind": "current-design-contract-pointer",
                "loop_id": "dc-symlink",
                "loop_run_path": "linked-outside/loop-run.json",
            }
        ),
        encoding="utf-8",
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, yes=True)
    )

    assert result.status == "blocked"
    assert "must stay within project" in result.blocker


def test_close_design_contract_loop_blocks_current_pointer_file_symlink(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-pointer-file-symlink"
    assert (
        check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-contract",
                loop_id=loop_id,
            )
        ).status
        == "ready"
    )
    pointer_path = tmp_path / CURRENT_DESIGN_CONTRACT_PATH
    backing = pointer_path.with_name("backing-current-design-contract.json")
    backing.write_bytes(pointer_path.read_bytes())
    pointer_path.unlink()
    pointer_path.symlink_to(backing.name)

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )

    assert result.status == "blocked"
    assert result.closed is False
    assert "pointer is malformed" in result.blocker


def test_check_design_contract_loop_blocks_unsafe_loop_id(tmp_path: Path) -> None:
    _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="../bad",
        )
    )

    assert result.status == "blocked"
    assert "Invalid design-contract loop id" in result.blocker
    assert not (tmp_path / ".ai-sdlc" / "loops" / "design-contract").exists()


def test_check_design_contract_loop_blocks_missing_work_item(tmp_path: Path) -> None:
    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/missing-contract",
            loop_id="dc-missing",
        )
    )

    assert result.status == "blocked"
    assert "does not exist" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


@pytest.mark.parametrize("doc_name", ("spec.md", "plan.md", "tasks.md"))
def test_check_design_contract_loop_blocks_symlinked_formal_doc(
    tmp_path: Path,
    doc_name: str,
) -> None:
    work_item = _write_work_item(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-{doc_name}"
    outside.write_text(work_item.joinpath(doc_name).read_text("utf-8"), "utf-8")
    work_item.joinpath(doc_name).unlink()
    try:
        work_item.joinpath(doc_name).symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=f"dc-symlinked-{doc_name.removesuffix('.md')}",
        )
    )

    assert result.status == "blocked"
    assert "symlink" in result.blocker.lower()


def test_check_design_contract_loop_uses_checkpoint_feature_spec_dir(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    checkpoint = tmp_path / ".ai-sdlc" / "state" / "checkpoint.yml"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        "\n".join(
            [
                "current_stage: execute",
                "feature:",
                "  id: demo-contract",
                "  spec_dir: specs/demo-contract",
            ]
        ),
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            loop_id="dc-checkpoint-spec-dir",
        )
    )

    assert result.status == "ready"
    assert result.work_item_path == "specs/demo-contract"


def test_check_design_contract_loop_prefers_checkpoint_linked_wi_id(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    checkpoint = tmp_path / ".ai-sdlc" / "state" / "checkpoint.yml"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        "\n".join(
            [
                "current_stage: execute",
                "linked_plan_uri: .cursor/plans/demo.plan.md",
                "linked_wi_id: demo-contract",
            ]
        ),
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            loop_id="dc-checkpoint-linked-wi",
        )
    )

    assert result.status == "ready"
    assert result.work_item_path == "specs/demo-contract"


def _write_work_item(
    root: Path,
    *,
    include_task_refs: bool = True,
    with_frozen_requirement: bool = True,
    requirement_loop_id: str = "req-current",
    requirement_scope_families: tuple[str, ...] = (),
    placeholder: bool = False,
    relative_path: str = "specs/demo-contract",
    task_heading: str = "### Task 1.1 Check contract",
    plan_extra: str = "",
    spec_intro_extra: str = "",
    success_heading: str = "## 成功标准",
    acceptance_label: str = "- **验收标准**",
    verification_label: str = "- **验证**",
    verification_value: str = "uv run pytest tests/unit/test_demo.py -q",
    tasks_intro_extra: str = "",
    tasks_tail_extra: str = "",
    extra_task_sections: str = "",
    spec_status_line: str = "**状态**：已冻结",
    spec_title: str = "# PRD：Demo Contract",
) -> Path:
    work_item = root / relative_path
    work_item.mkdir(parents=True)
    spec_extra = "\nTODO: remove placeholder.\n" if placeholder else ""
    work_item.joinpath("spec.md").write_text(
        "\n".join(
            [
                spec_title,
                "",
                spec_status_line,
                "",
                spec_intro_extra,
                "## 需求",
                "",
                "- **FR-DEMO-001**：系统必须检查合同覆盖。",
                "",
                success_heading,
                "",
                "- **SC-DEMO-001**：合同通过后可以关闭。",
                spec_extra,
            ]
        ),
        encoding="utf-8",
    )
    work_item.joinpath("plan.md").write_text(
        "\n".join(
            [
                "# 实施计划",
                "",
                "## 技术背景",
                "Python runtime.",
                "## 阶段计划",
                "Phase 1.",
                "## 验证策略",
                "Run pytest.",
                "## 回退方式",
                "Revert the commit.",
                plan_extra,
            ]
        ),
        encoding="utf-8",
    )
    refs = "FR-DEMO-001 and SC-DEMO-001" if include_task_refs else "contract docs"
    work_item.joinpath("tasks.md").write_text(
        "\n".join(
            [
                "# 任务分解",
                "",
                tasks_intro_extra,
                task_heading,
                "",
                "- **任务编号**：T11",
                "- **优先级**：P0",
                f"{acceptance_label}：Cover {refs}.",
                f"{verification_label}：{verification_value}",
                extra_task_sections,
                tasks_tail_extra,
            ]
        ),
        encoding="utf-8",
    )
    if with_frozen_requirement:
        _ensure_frozen_requirement_loop(
            root,
            loop_id=requirement_loop_id,
            scope_families=requirement_scope_families,
        )
    return work_item


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _ensure_frozen_requirement_loop(
    root: Path,
    *,
    loop_id: str,
    scope_families: tuple[str, ...] = (),
) -> None:
    freeze_path = (
        root
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / loop_id
        / "requirement-freeze.json"
    )
    if freeze_path.is_file():
        return
    start_result = start_requirement_loop(
        RequirementStartOptions(
            root=root,
            loop_id=loop_id,
            idea="Demo users need a checked design contract.",
            acceptance=("The design contract can be checked before implementation.",),
            design_scope_families=scope_families,
        )
    )
    assert start_result.status == "ready"
    freeze_result = freeze_requirement_loop(
        RequirementFreezeOptions(root=root, loop_id=loop_id, yes=True)
    )
    assert freeze_result.frozen is True
