"""Tests for the bounded Loop-native review outcome contract."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_sdlc.cli.loop_review_cmd import resolve_review_input
from ai_sdlc.cli.main import app
from ai_sdlc.core.loop_review_models import LoopReviewOutcome, ReviewStatusOverlay
from ai_sdlc.core.loop_review_service import (
    LoopReviewPreparation,
    LoopReviewServiceError,
    RecordLoopReviewOptions,
    prepare_loop_review,
    record_loop_review,
    validate_prepared_outcome_for_close,
)
from ai_sdlc.core.review_kernel import ReviewExecution, ReviewFinding


@dataclass(frozen=True)
class LoopFixture:
    root: Path
    loop_id: str
    loop_dir: Path

    def resolve_input(self, round_number: int):
        return resolve_review_input(
            self.root,
            loop_type="requirement",
            loop_id=self.loop_id,
            review_round_number=round_number,
        )

    def prepare(self) -> LoopReviewPreparation:
        return prepare_loop_review(
            self.root,
            loop_type="requirement",
            loop_id=self.loop_id,
            loop_dir=self.loop_dir,
            input_resolver=self.resolve_input,
        )


@pytest.fixture
def loop_fixture(tmp_path: Path) -> LoopFixture:
    loop_id = "requirement-review-contract"
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / loop_id
    loop_dir.mkdir(parents=True)
    (loop_dir / "loop-run.json").write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_type": "requirement",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    pointer = loop_dir.parent / "current-requirement.json"
    pointer.write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_run_path": (
                    f".ai-sdlc/loops/requirement/{loop_id}/loop-run.json"
                ),
            }
        ),
        encoding="utf-8",
    )
    (loop_dir / "requirement-intake.json").write_text("{}", encoding="utf-8")
    (loop_dir / "requirement-brief.md").write_text(
        "Security permission requirement.\n",
        encoding="utf-8",
    )
    (loop_dir / "clarification-questions.md").write_text(
        "No open questions.\n",
        encoding="utf-8",
    )
    (loop_dir / "acceptance-checklist.md").write_text(
        "- Permission is verified.\n",
        encoding="utf-8",
    )
    return LoopFixture(root=tmp_path, loop_id=loop_id, loop_dir=loop_dir)


def _finding(*, role: str = "correctness-and-regression") -> ReviewFinding:
    return ReviewFinding(
        severity="important",
        role=role,
        location="implementation-report.md:10",
        summary="A required regression remains unresolved.",
        recommendation="Add a focused regression before Close.",
    )


def _outcome_payload() -> dict[str, object]:
    return {
        "loop_id": "implementation-1",
        "loop_type": "implementation",
        "round_number": 1,
        "input_digest": "a" * 64,
        "status": "completed",
        "expert_roles": ["correctness-and-regression"],
        "findings": [],
        "failure_kind": "",
        "failure_reason": "",
        "recorded_at": "2026-08-18T00:00:00Z",
    }


def test_b1_failed_outcome_retains_framework_retry_count_without_assessment():
    payload = _outcome_payload()
    payload.update(
        status="failed",
        failure_kind="reviewer-exited",
        failure_reason="Independent reviewer did not complete.",
        infra_retry_count=0,
    )
    outcome = LoopReviewOutcome.model_validate(payload)
    assert outcome.infra_retry_count == 0
    assert outcome.b1 is None
    assert outcome.model_dump()["infra_retry_count"] == 0


def test_legacy_outcome_keeps_original_serialized_fields():
    payload = _outcome_payload()
    assert LoopReviewOutcome.model_validate(payload).model_dump() == payload


@pytest.mark.parametrize(
    "marker",
    [
        "review-continuation.json",
        "review-outcome-round-3.json",
        "review-outcome-round-5.json",
        "implementation-close.json",
    ],
)
@pytest.mark.parametrize("entry", ["prepare", "record"])
def test_retired_continuation_cannot_reenter_normal_rounds(tmp_path, marker, entry):
    loop_id = "retired-implementation"
    loop_dir = tmp_path / ".ai-sdlc/loops/implementation" / loop_id
    loop_dir.mkdir(parents=True)
    original = b'{"status":"failed","reason":"preserved historical receipt"}\n'
    if marker == "implementation-close.json":
        from ai_sdlc.core.implementation_models import ImplementationClose

        original = (
            ImplementationClose(
                loop_id=loop_id,
                report_path=f".ai-sdlc/loops/implementation/{loop_id}/implementation-report.json",
                review_binding={
                    "review_round": 3,
                    "input_digest": "a" * 64,
                    "outcome_sha256": "b" * 64,
                    "continuation_sha256": "c" * 64,
                    "continuation_digest": "d" * 64,
                },
            )
            .model_dump_json()
            .encode("utf-8")
        )
    (loop_dir / marker).write_bytes(original)

    def no_new_review(_round_number):
        pytest.fail("Retired history must be rejected before requesting new input.")

    with pytest.raises(ValueError, match="implementation-continuation-retired"):
        if entry == "prepare":
            prepare_loop_review(
                tmp_path,
                loop_type="implementation",
                loop_id=loop_id,
                loop_dir=loop_dir,
                input_resolver=no_new_review,
            )
        else:
            record_loop_review(
                RecordLoopReviewOptions(
                    root=tmp_path,
                    loop_type="implementation",
                    loop_id=loop_id,
                    expected_digest="a" * 64,
                    result_paths=(),
                ),
                loop_dir=loop_dir,
                input_resolver=no_new_review,
            )
    assert (loop_dir / marker).read_bytes() == original
    assert not (loop_dir / "review-outcome-round-1.json").exists()
    assert not (loop_dir / "review-outcome-round-2.json").exists()
    assert {path.name for path in loop_dir.iterdir()} == {marker}


def _mock_legacy_implementation_status(monkeypatch):
    import ai_sdlc.cli.loop_cmd as command
    from ai_sdlc.core.loop_status import (
        LoopNextActionGuidance,
        LoopStatusResult,
        LoopSummary,
    )

    prior_guidance = LoopNextActionGuidance(
        command="ai-sdlc loop review implementation",
        reason="The unchanged current result needs review.",
        requires_model=True,
        safety="may_call_local_review_agent",
    )
    current = LoopSummary(
        loop_id="retired-implementation",
        loop_type="implementation",
        status="needs_review",
        next_action="Run another Implementation review.",
        next_guidance=prior_guidance,
    )
    monkeypatch.setattr(
        command,
        "get_loop_status",
        lambda *args, **kwargs: LoopStatusResult(
            status="ready", current_loop=current, next_guidance=prior_guidance
        ),
    )
    monkeypatch.setattr(command, "_stage_decision_status", lambda *args: False)
    monkeypatch.setattr(
        command, "_implementation_decision_status", lambda *args: (False, None)
    )
    return command


def test_retired_review_status_preserves_history_and_does_not_offer_another_review(
    tmp_path, monkeypatch
):
    command = _mock_legacy_implementation_status(monkeypatch)

    def retired(*args, **kwargs):
        raise LoopReviewServiceError("implementation-continuation-retired")

    monkeypatch.setattr(command, "prepare_current_loop_review", retired)
    result = command.get_review_aware_loop_status(tmp_path, "implementation")

    assert result.status == "blocked"
    assert result.blocker == "implementation-continuation-retired"
    assert "preserve" in result.next_action.lower()
    assert "unchanged" in result.next_action.lower()
    assert any(
        phrase in result.next_action.lower()
        for phrase in ("cannot be resumed", "do not resume", "no further review")
    )
    assert "repair" not in result.next_action.lower()
    assert not result.next_guidance.command
    assert not result.next_guidance.requires_model
    assert not result.next_guidance.writes_artifacts
    assert result.next_guidance.safety in {"blocked", "no_action"}
    assert result.current_loop is not None
    assert result.current_loop.next_action == result.next_action
    assert result.current_loop.next_guidance == result.next_guidance


@pytest.mark.parametrize("original", [b"{", b"[]", b"\xff"])
def test_malformed_close_status_never_falls_back_to_unreviewed_state(
    tmp_path, monkeypatch, original
):
    from ai_sdlc.core.loop_review_service import (
        reject_retired_implementation_continuation,
    )

    command = _mock_legacy_implementation_status(monkeypatch)
    directory = tmp_path / ".ai-sdlc/loops/implementation/retired-implementation"
    directory.mkdir(parents=True)
    path = directory / "implementation-close.json"
    path.write_bytes(original)

    def inspect_actual_close(root, _loop_type, loop_id):
        reject_retired_implementation_continuation(root, loop_id)
        pytest.fail("Malformed Close must not proceed to an unreviewed state.")

    monkeypatch.setattr(command, "prepare_current_loop_review", inspect_actual_close)
    result = command.get_review_aware_loop_status(tmp_path, "implementation")

    assert result.status == "blocked"
    assert result.blocker == "implementation-close-invalid"
    assert path.read_bytes() == original
    assert {item.name for item in directory.iterdir()} == {path.name}


@pytest.mark.parametrize("binding", [{}, "historical-binding", 0, False])
def test_any_nonnull_legacy_close_binding_is_a_retirement_marker(tmp_path, binding):
    from ai_sdlc.core.loop_review_service import (
        reject_retired_implementation_continuation,
    )

    loop_id = "retired-close-marker"
    directory = tmp_path / ".ai-sdlc/loops/implementation" / loop_id
    directory.mkdir(parents=True)
    path = directory / "implementation-close.json"
    original = json.dumps({"review_binding": binding}).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(
        LoopReviewServiceError, match="implementation-continuation-retired"
    ):
        reject_retired_implementation_continuation(tmp_path, loop_id)
    assert path.read_bytes() == original
    assert {item.name for item in directory.iterdir()} == {path.name}


@pytest.mark.parametrize("explicit_null", [False, True])
def test_normal_close_without_legacy_binding_is_not_retired(tmp_path, explicit_null):
    from ai_sdlc.core.implementation_models import ImplementationClose
    from ai_sdlc.core.loop_review_service import (
        reject_retired_implementation_continuation,
    )

    loop_id = "ordinary-close"
    directory = tmp_path / ".ai-sdlc/loops/implementation" / loop_id
    directory.mkdir(parents=True)
    path = directory / "implementation-close.json"
    payload = ImplementationClose(
        loop_id=loop_id,
        report_path=f".ai-sdlc/loops/implementation/{loop_id}/implementation-report.json",
    ).model_dump(mode="json")
    if explicit_null:
        payload["review_binding"] = None
    original = json.dumps(payload).encode("utf-8")
    path.write_bytes(original)

    reject_retired_implementation_continuation(tmp_path, loop_id)
    assert path.read_bytes() == original
    assert {item.name for item in directory.iterdir()} == {path.name}


@pytest.mark.parametrize("original", [b"{", b"[]", b"null", b'"close"', b"0", b"\xff"])
def test_invalid_close_retirement_probe_fails_closed_with_service_error(
    tmp_path, original
):
    from ai_sdlc.core.loop_review_service import (
        reject_retired_implementation_continuation,
    )

    loop_id = "invalid-close-marker"
    directory = tmp_path / ".ai-sdlc/loops/implementation" / loop_id
    directory.mkdir(parents=True)
    path = directory / "implementation-close.json"
    path.write_bytes(original)

    with pytest.raises(LoopReviewServiceError):
        reject_retired_implementation_continuation(tmp_path, loop_id)
    assert path.read_bytes() == original
    assert {item.name for item in directory.iterdir()} == {path.name}


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_close_retirement_probe_rejects_nonregular_file_before_reading(
    tmp_path, monkeypatch, kind
):
    import os
    import stat

    import ai_sdlc.core.loop_review_service as service

    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("This operating system does not support named FIFO creation.")
    loop_id = "nonregular-close-marker"
    directory = tmp_path / ".ai-sdlc/loops/implementation" / loop_id
    directory.mkdir(parents=True)
    path = directory / "implementation-close.json"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    original_stat = path.lstat()

    def no_byte_read(*args, **kwargs):
        raise AssertionError("Nonregular Close must be rejected before any byte read.")

    monkeypatch.setattr(service, "read_stable_bytes", no_byte_read)
    with pytest.raises(LoopReviewServiceError, match="implementation-close-invalid"):
        service.reject_retired_implementation_continuation(tmp_path, loop_id)
    current_stat = path.lstat()
    assert current_stat.st_ino == original_stat.st_ino
    assert (
        stat.S_ISDIR(current_stat.st_mode)
        if kind == "directory"
        else stat.S_ISFIFO(current_stat.st_mode)
    )
    assert {item.name for item in directory.iterdir()} == {path.name}


def test_retired_continuation_has_no_activation_command():
    result = CliRunner().invoke(app, ["loop", "implementation", "--help"])
    assert result.exit_code == 0, result.output
    assert "continue-review" not in result.output


def test_retired_outcome_fields_remain_readable_without_changing_original_shape(
    tmp_path,
):
    from ai_sdlc.core.loop_review_service import _write_outcome

    payload = _outcome_payload()
    payload.update(
        round_number=3,
        status="failed",
        failure_kind="expert-execution-failed",
        failure_reason="Preserved historical execution failure.",
        continuation_digest="b" * 64,
        continuation_material_manifest={
            "source_files": [],
            "verification_evidence": [],
            "prior_outcomes": [],
        },
        continuation_retry_count=0,
    )
    outcome = LoopReviewOutcome.model_validate(payload)
    assert outcome.model_dump() == payload
    directory = tmp_path / ".ai-sdlc/loops/implementation" / outcome.loop_id
    directory.mkdir(parents=True)
    destination = directory / "review-outcome-round-3.json"
    with pytest.raises(
        LoopReviewServiceError, match="implementation-continuation-retired"
    ):
        _write_outcome(tmp_path, destination, outcome)
    assert not destination.exists()


def test_retired_history_check_cannot_treat_unreadable_directory_as_empty(
    tmp_path, monkeypatch
):
    import os

    from ai_sdlc.core.loop_review_service import (
        reject_retired_implementation_continuation,
    )

    loop_id = "unreadable-implementation-history"
    directory = tmp_path / ".ai-sdlc/loops/implementation" / loop_id
    directory.mkdir(parents=True)
    original_scandir = os.scandir
    original_listdir = os.listdir

    def unreadable_scan(path):
        if Path(path) == directory:
            raise PermissionError("history directory cannot be listed")
        return original_scandir(path)

    def unreadable_list(path):
        if Path(path) == directory:
            raise PermissionError("history directory cannot be listed")
        return original_listdir(path)

    monkeypatch.setattr(os, "scandir", unreadable_scan)
    monkeypatch.setattr(os, "listdir", unreadable_list)
    with pytest.raises(PermissionError, match="cannot be listed"):
        reject_retired_implementation_continuation(tmp_path, loop_id)


@pytest.mark.parametrize("value", [True, 1.0, -1, 2, "1"])
def test_b1_retry_count_rejects_coercion_and_extra_attempts(value):
    payload = _outcome_payload()
    payload.update(
        status="failed",
        failure_kind="reviewer-exited",
        failure_reason="Independent reviewer did not complete.",
        infra_retry_count=value,
    )
    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def _result_paths(
    fixture: LoopFixture,
    prepared: LoopReviewPreparation,
    *,
    severity: str | None = None,
    failed: bool = False,
    roles: list[str] | None = None,
    name_prefix: str = "expert",
) -> tuple[Path, ...]:
    selected = roles or prepared.review_input.expert_roles
    paths: list[Path] = []
    for index, role in enumerate(selected):
        result_path = fixture.root / (
            f"{name_prefix}-{prepared.review_input.round_number}-{index}.json"
        )
        if failed:
            execution = ReviewExecution(
                status="failed",
                roles=[role],
                role_reasons={role: prepared.review_input.expert_reasons[role]},
                failure_kind="reviewer-exited",
                failure_reason="The independent reviewer exited nonzero.",
            )
        else:
            findings = []
            if severity is not None:
                findings.append(
                    ReviewFinding(
                        severity=severity,
                        role=role,
                        location="requirement-brief.md:1",
                        summary="The requirement still has an actionable gap.",
                        recommendation="Revise the requirement before Close.",
                    )
                )
            execution = ReviewExecution(
                status="completed",
                roles=[role],
                role_reasons={role: prepared.review_input.expert_reasons[role]},
                findings=findings,
            )
        result_path.write_text(execution.model_dump_json(), encoding="utf-8")
        paths.append(result_path)
    return tuple(paths)


def _record(
    fixture: LoopFixture,
    prepared: LoopReviewPreparation,
    result_paths: tuple[Path, ...],
):
    return record_loop_review(
        RecordLoopReviewOptions(
            root=fixture.root,
            loop_type="requirement",
            loop_id=fixture.loop_id,
            expected_digest=prepared.review_input.input_digest,
            result_paths=result_paths,
        ),
        loop_dir=fixture.loop_dir,
        input_resolver=fixture.resolve_input,
    )


def _prepare_round_two(fixture: LoopFixture) -> LoopReviewPreparation:
    first = fixture.prepare()
    first_record = _record(
        fixture,
        first,
        _result_paths(fixture, first, severity="important"),
    )
    assert first_record.status == "needs_fix"
    with (fixture.loop_dir / "requirement-brief.md").open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write("Repair after round one.\n")
    second = fixture.prepare()
    assert second.review_input.round_number == 2
    assert second.status == "review_missing"
    return second


def test_loop_review_outcome_accepts_minimal_completed_shape() -> None:
    outcome = LoopReviewOutcome.model_validate(_outcome_payload())

    assert outcome.round_number == 1
    assert outcome.status == "completed"
    assert outcome.expert_roles == ["correctness-and-regression"]


@pytest.mark.parametrize("round_number", [0, 3])
def test_loop_review_outcome_allows_only_two_rounds(round_number: int) -> None:
    payload = _outcome_payload()
    payload["round_number"] = round_number

    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def test_loop_review_outcome_rejects_third_expert() -> None:
    payload = _outcome_payload()
    payload["expert_roles"] = ["one", "two", "three"]

    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def test_loop_review_outcome_rejects_finding_from_unselected_role() -> None:
    payload = _outcome_payload()
    payload["findings"] = [_finding(role="security-and-permissions")]

    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "authorization",
        "certificate",
        "session",
        "quorum",
        "score",
        "policy_digest",
        "approved_by",
    ],
)
def test_loop_review_outcome_rejects_credential_fields(field: str) -> None:
    payload = _outcome_payload()
    payload[field] = "forbidden"

    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def test_loop_review_outcome_requires_failure_details_for_failed_status() -> None:
    payload = _outcome_payload()
    payload["status"] = "failed"

    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def test_loop_review_outcome_rejects_failure_details_when_completed() -> None:
    payload = _outcome_payload()
    payload["failure_kind"] = "provider-failed"
    payload["failure_reason"] = "reviewer exited nonzero"

    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def test_review_status_overlay_is_display_state_not_close_credential() -> None:
    overlay = ReviewStatusOverlay(
        status="needs_fix",
        reason="review-findings-actionable",
        next_action="Resolve the recorded findings and prepare round 2.",
        round_number=1,
    )

    assert set(overlay.model_dump()) == {
        "status",
        "reason",
        "next_action",
        "round_number",
    }


def test_matching_digest_without_outcome_cannot_close(
    loop_fixture: LoopFixture,
) -> None:
    prepared = loop_fixture.prepare()

    assert prepared.status == "review_missing"
    with pytest.raises(LoopReviewServiceError, match="review-result-missing"):
        validate_prepared_outcome_for_close(
            prepared,
            expected_digest=prepared.review_input.input_digest,
        )


def test_failed_outcome_cannot_close(loop_fixture: LoopFixture) -> None:
    prepared = loop_fixture.prepare()
    _record(
        loop_fixture,
        prepared,
        _result_paths(loop_fixture, prepared, failed=True),
    )
    current = loop_fixture.prepare()

    with pytest.raises(LoopReviewServiceError, match="review-execution-failed"):
        validate_prepared_outcome_for_close(
            current,
            expected_digest=current.review_input.input_digest,
        )


@pytest.mark.parametrize("failure_kind", ["reviewer-exited", "provider-policy-blocked"])
def test_mixed_failure_preserves_completed_findings_without_allowing_close(
    loop_fixture: LoopFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    prepared = loop_fixture.prepare()
    completed = _result_paths(
        loop_fixture, prepared, severity="important", name_prefix="completed"
    )
    failed = _result_paths(loop_fixture, prepared, failed=True, name_prefix="failed")
    failure = json.loads(failed[1].read_text(encoding="utf-8"))
    failure["failure_kind"] = failure_kind
    failed[1].write_text(json.dumps(failure), encoding="utf-8")
    assert len(completed) == len(failed) == 2
    recorded = _record(loop_fixture, prepared, (completed[0], failed[1]))
    assert recorded.status == "failed"
    assert recorded.reason == "review-execution-failed"

    current = loop_fixture.prepare()
    outcome = current.current_outcome
    assert outcome is not None
    assert outcome.status == "failed"
    results = outcome.model_dump().get("completed_expert_results", [])
    assert len(results) == 1
    assert results[0]["roles"] == [prepared.review_input.expert_roles[0]]
    assert results[0]["findings"][0]["severity"] == "important"
    monkeypatch.chdir(loop_fixture.root)
    response = CliRunner().invoke(
        app,
        [
            "loop",
            "review",
            "--type",
            "requirement",
            "--loop-id",
            loop_fixture.loop_id,
            "--json",
        ],
    )
    assert response.exit_code == 0, response.output
    payload = json.loads(response.output)
    assert payload["review_status"] == "failed"
    assert payload["review_reason"] == "review-execution-failed"
    assert payload["execution_failure"] == {
        "kind": outcome.failure_kind,
        "detail": outcome.failure_reason,
        "input_digest": outcome.input_digest,
    }
    assert "Do not retry policy refusals" in payload["next_action"]
    assert payload["partial_review"]["uncompleted_roles"] == [
        prepared.review_input.expert_roles[1]
    ]
    assert payload["partial_review"]["known_findings"][0]["severity"] == "important"
    with pytest.raises(LoopReviewServiceError, match="review-execution-failed"):
        validate_prepared_outcome_for_close(
            current, expected_digest=current.review_input.input_digest
        )


def test_technical_failure_semicolon_quote_keeps_original_retry_rules(
    loop_fixture: LoopFixture,
) -> None:
    prepared = loop_fixture.prepare()
    results = _result_paths(loop_fixture, prepared, failed=True)
    failure = json.loads(results[0].read_text(encoding="utf-8"))
    failure["failure_reason"] = (
        "Diagnostic quoted text; provider-policy-blocked: example only."
    )
    results[0].write_text(json.dumps(failure), encoding="utf-8")
    _record(loop_fixture, prepared, results)

    current = loop_fixture.prepare()

    assert current.status == "failed"
    assert current.reason == "review-execution-failed"
    assert current.current_outcome is not None
    assert "provider-policy-blocked:" in current.current_outcome.failure_reason
    assert (
        _record(loop_fixture, current, _result_paths(loop_fixture, current)).status
        == "passed"
    )


@pytest.mark.parametrize(
    "invalid_kind",
    ["completed", "failed-role", "duplicate", "unselected", "all-completed"],
)
def test_partial_results_cannot_claim_missing_roles_completed(
    invalid_kind: str,
) -> None:
    payload = _outcome_payload()
    payload.update(
        status="failed",
        expert_roles=["correctness-and-regression", "security-and-permissions"],
        failure_kind="provider-policy-blocked",
        failure_reason="The other selected review did not complete.",
    )
    result = ReviewExecution(
        status="completed",
        roles=["correctness-and-regression"],
        role_reasons={"correctness-and-regression": "Primary expert."},
        findings=[_finding()],
    ).model_dump()
    payload["completed_expert_results"] = [result]
    if invalid_kind == "completed":
        payload.update(status="completed", failure_kind="", failure_reason="")
    elif invalid_kind == "failed-role":
        result.update(
            status="failed",
            failure_kind="unavailable",
            failure_reason="Not executed.",
            findings=[],
        )
    elif invalid_kind == "duplicate":
        payload["completed_expert_results"] = [result, result]
    elif invalid_kind == "unselected":
        result.update(
            roles=["unselected"],
            role_reasons={"unselected": "Wrong role."},
            findings=[],
        )
    else:
        payload["expert_roles"] = ["correctness-and-regression"]
    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def test_actionable_outcome_cannot_close(loop_fixture: LoopFixture) -> None:
    prepared = loop_fixture.prepare()
    _record(
        loop_fixture,
        prepared,
        _result_paths(loop_fixture, prepared, severity="important"),
    )
    current = loop_fixture.prepare()

    with pytest.raises(LoopReviewServiceError, match="review-findings-actionable"):
        validate_prepared_outcome_for_close(
            current,
            expected_digest=current.review_input.input_digest,
        )


def test_stale_completed_outcome_cannot_close(loop_fixture: LoopFixture) -> None:
    prepared = loop_fixture.prepare()
    _record(loop_fixture, prepared, _result_paths(loop_fixture, prepared))
    with (loop_fixture.loop_dir / "requirement-brief.md").open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write("Unreviewed change.\n")
    current = loop_fixture.prepare()

    with pytest.raises(LoopReviewServiceError, match="review-input-drift"):
        validate_prepared_outcome_for_close(
            current,
            expected_digest=prepared.review_input.input_digest,
        )


def test_wrong_role_outcome_cannot_close(loop_fixture: LoopFixture) -> None:
    prepared = loop_fixture.prepare()
    outcome = LoopReviewOutcome(
        loop_id=loop_fixture.loop_id,
        loop_type="requirement",
        round_number=1,
        input_digest=prepared.review_input.input_digest,
        status="completed",
        expert_roles=[prepared.review_input.expert_roles[0]],
        findings=[],
        recorded_at="2026-08-17T00:00:00Z",
    )
    (loop_fixture.loop_dir / "review-outcome-round-1.json").write_text(
        outcome.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    current = loop_fixture.prepare()

    with pytest.raises(LoopReviewServiceError, match="expert-role-mismatch"):
        validate_prepared_outcome_for_close(
            current,
            expected_digest=current.review_input.input_digest,
        )


def test_record_rejects_missing_selected_expert(loop_fixture: LoopFixture) -> None:
    prepared = loop_fixture.prepare()
    assert len(prepared.review_input.expert_roles) == 2
    paths = _result_paths(
        loop_fixture,
        prepared,
        roles=[prepared.review_input.expert_roles[0]],
    )

    with pytest.raises(LoopReviewServiceError, match="expert-role-mismatch"):
        _record(loop_fixture, prepared, paths)


def test_completed_clean_round_one_cannot_be_recorded_again(
    loop_fixture: LoopFixture,
) -> None:
    prepared = loop_fixture.prepare()
    paths = _result_paths(loop_fixture, prepared)

    first = _record(loop_fixture, prepared, paths)
    assert first.status == "passed"
    with pytest.raises(LoopReviewServiceError, match="review-already-completed"):
        _record(loop_fixture, prepared, paths)


def test_concurrent_completed_round_one_has_exactly_one_winner(
    loop_fixture: LoopFixture,
) -> None:
    prepared = loop_fixture.prepare()
    clean_paths = _result_paths(
        loop_fixture,
        prepared,
        name_prefix="clean-expert",
    )
    actionable_paths = _result_paths(
        loop_fixture,
        prepared,
        severity="important",
        name_prefix="actionable-expert",
    )
    start = threading.Barrier(2)
    replace_guard = threading.Lock()
    first_replace = threading.Event()
    second_replace = threading.Event()
    replacement_count = 0
    successes: list[tuple[str, bytes]] = []
    errors: list[str] = []
    result_guard = threading.Lock()
    outcome = loop_fixture.loop_dir / "review-outcome-round-1.json"
    real_replace = __import__("os").replace

    def synchronized_replace(source: Path, destination: Path) -> None:
        nonlocal replacement_count
        with replace_guard:
            replacement_count += 1
            ordinal = replacement_count
        if ordinal == 1:
            first_replace.set()
            second_replace.wait(timeout=0.5)
        elif ordinal == 2:
            second_replace.set()
        real_replace(source, destination)

    def record(name: str, paths: tuple[Path, ...]) -> None:
        start.wait()
        try:
            _record(loop_fixture, prepared, paths)
        except LoopReviewServiceError as exc:
            with result_guard:
                errors.append(exc.reason)
            return
        with result_guard:
            successes.append((name, outcome.read_bytes()))

    with patch(
        "ai_sdlc.core.loop_review_service.os.replace",
        side_effect=synchronized_replace,
    ):
        clean = threading.Thread(target=record, args=("clean", clean_paths))
        actionable = threading.Thread(
            target=record,
            args=("actionable", actionable_paths),
        )
        clean.start()
        actionable.start()
        clean.join(timeout=5)
        actionable.join(timeout=5)

    assert not clean.is_alive()
    assert not actionable.is_alive()
    assert first_replace.is_set()
    assert replacement_count == 1
    assert len(successes) == 1
    assert len(errors) == 1
    assert errors[0] in {
        "review-already-completed",
        "review-outcome-lock-unavailable",
    }
    assert outcome.read_bytes() == successes[0][1]


@pytest.mark.parametrize("severity", [None, "advisory", "important"])
def test_completed_round_two_cannot_be_recorded_again(
    loop_fixture: LoopFixture,
    severity: str | None,
) -> None:
    prepared = _prepare_round_two(loop_fixture)
    paths = _result_paths(loop_fixture, prepared, severity=severity)

    completed = _record(loop_fixture, prepared, paths)
    assert completed.status in {"passed", "needs_user"}
    with pytest.raises(LoopReviewServiceError, match="review-round-limit"):
        _record(loop_fixture, prepared, paths)


@pytest.mark.parametrize("round_number", [1, 2])
def test_failed_round_can_retry_only_same_digest(
    loop_fixture: LoopFixture,
    round_number: int,
) -> None:
    prepared = (
        loop_fixture.prepare()
        if round_number == 1
        else _prepare_round_two(loop_fixture)
    )
    failed = _record(
        loop_fixture,
        prepared,
        _result_paths(loop_fixture, prepared, failed=True),
    )
    assert failed.status == "failed"

    retry = _record(loop_fixture, prepared, _result_paths(loop_fixture, prepared))
    assert retry.status == "passed"


@pytest.mark.parametrize("round_number", [1, 2])
def test_failed_round_stale_retry_preserves_failed_outcome(
    loop_fixture: LoopFixture,
    round_number: int,
) -> None:
    prepared = (
        loop_fixture.prepare()
        if round_number == 1
        else _prepare_round_two(loop_fixture)
    )
    failed = _record(
        loop_fixture,
        prepared,
        _result_paths(loop_fixture, prepared, failed=True),
    )
    outcome_path = loop_fixture.loop_dir / f"review-outcome-round-{round_number}.json"
    failed_bytes = outcome_path.read_bytes()
    assert failed.status == "failed"

    with (loop_fixture.loop_dir / "requirement-brief.md").open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write("Changed after failed review.\n")
    stale = loop_fixture.prepare()
    with pytest.raises(LoopReviewServiceError, match="review-input-drift"):
        _record(loop_fixture, stale, _result_paths(loop_fixture, stale))
    assert outcome_path.read_bytes() == failed_bytes
