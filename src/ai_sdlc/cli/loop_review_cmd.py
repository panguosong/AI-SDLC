"""Read-only CLI mapping from existing Loop results to review inputs."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
import re
import subprocess
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer

from ai_sdlc.core.frontend_visual_baseline import (
    validate_frontend_visual_baseline_identity,
)
from ai_sdlc.core.loop_decision_service import validate_implementation_source_boundary
from ai_sdlc.core.loop_review_service import (
    LoopReviewPreparation,
    LoopReviewServiceError,
    RecordLoopReviewOptions,
    outcome_path,
    prepare_loop_review,
    record_loop_review,
    reject_retired_implementation_continuation,
    validate_prepared_outcome_for_close,
)
from ai_sdlc.core.pr_review_models import PRReviewVerificationEvidence
from ai_sdlc.core.review_kernel import LoopReviewType, ReviewInput, build_review_input
from ai_sdlc.core.source_snapshot import SourceSnapshotOptions, build_source_snapshot
from ai_sdlc.core.stable_file_read import consume_stable_chunks, read_stable_text
from ai_sdlc.utils.helpers import find_project_root

if TYPE_CHECKING:
    from ai_sdlc.core.loop_decision_models import DecisionContext
    from ai_sdlc.core.loop_decision_service import B1ReviewSnapshot
    from ai_sdlc.core.loop_simulation_context import SimulationContext

_STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "requirement": (
        "requirement-intake.json",
        "requirement-brief.md",
        "clarification-questions.md",
        "acceptance-checklist.md",
    ),
    "design-contract": (
        "design-contract-input.json",
        "design-contract-report.json",
        "design-contract-report.md",
    ),
    "implementation": (
        "implementation-input.json",
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ),
    "frontend-evidence": (
        "frontend-evidence-input.json",
        "frontend-evidence-snapshot.json",
        "frontend-evidence-report.json",
        "frontend-evidence-report.md",
    ),
}
_STAGE_CLOSE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "requirement": ("requirement-intake.json",),
    "design-contract": (
        "design-contract-input.json",
        "design-contract-report.json",
    ),
    "implementation": (
        "implementation-input.json",
        "implementation-report.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ),
    "frontend-evidence": (
        "frontend-evidence-input.json",
        "frontend-evidence-snapshot.json",
        "frontend-evidence-report.json",
    ),
}
_STAGE_PREDECESSORS: dict[str, tuple[str, str, str]] = {
    "design-contract": (
        "design-contract-input.json",
        "requirement_loop_id",
        "requirement",
    ),
    "implementation": (
        "implementation-input.json",
        "design_contract_loop_id",
        "design-contract",
    ),
    "frontend-evidence": (
        "frontend-evidence-input.json",
        "implementation_loop_id",
        "implementation",
    ),
}
_STAGE_POINTER_NAMES = {
    "requirement": "current-requirement.json",
    "design-contract": "current-design-contract.json",
    "implementation": "current-implementation.json",
    "frontend-evidence": "current-frontend-evidence.json",
}
_LOCAL_REQUIRED = ("review-pack.json", "findings.json")
_LOCAL_OPTIONAL = ("resolution.yaml", "verification-evidence.json")
_CURRENT_LOCAL_REVIEW = Path(".ai-sdlc") / "reviews" / "pr" / "current-review.json"
_RISK_TERMS: dict[str, tuple[str, ...]] = {
    "public-api": ("public api", "public-api", "schema", "contract"),
    "security": ("security", "authorization", "permission", "secret"),
    "data-integrity": ("database", "migration", "transaction", "data loss"),
    "concurrency": ("concurrency", "parallel", "race", "lock"),
    "frontend": ("frontend", "browser", "accessibility", "ui", "ux"),
}
_RISK_SCAN_OVERLAP = (
    max(len(term) for terms in _RISK_TERMS.values() for term in terms) + 2
)
_TEXT_RISK_SUFFIXES = {
    ".bat",
    ".cfg",
    ".cmd",
    ".css",
    ".csv",
    ".diff",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".patch",
    ".ps1",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
_BINARY_RISK_SUFFIXES = {
    ".avi",
    ".bmp",
    ".db",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp4",
    ".otf",
    ".pdf",
    ".png",
    ".sqlite",
    ".tar",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}
_GIT_ROUTING_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_REPLACE_REF_BASE",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
}


class ReviewInputGuardError(ValueError):
    """A close request no longer matches the input selected for review."""

    def __init__(
        self,
        reason: str,
        *,
        detail: str = "",
        expected_digest: str = "",
        actual_digest: str = "",
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "blocked",
            "reason": self.reason,
        }
        if self.detail:
            result["detail"] = self.detail
        if self.expected_digest:
            result["expected_digest"] = self.expected_digest
        if self.actual_digest:
            result["actual_digest"] = self.actual_digest
        return result


def validate_review_input_for_close(
    root: Path,
    *,
    loop_type: str,
    loop_id: str,
    expected_digest: str,
    captured_artifacts: MutableMapping[str, bytes] | None = None,
) -> ReviewInput:
    """Require a current clean expert outcome and capture its reviewed input."""

    expected = expected_digest.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ReviewInputGuardError(
            "review-input-unavailable",
            detail="Expected review input digest must be 64 lowercase hexadecimal characters.",
        )
    try:
        prepared, _ = prepare_current_loop_review(root, loop_type, loop_id)
        review_input = resolve_review_input(
            root,
            loop_type=loop_type,
            loop_id=loop_id,
            review_round_number=prepared.review_input.round_number,
            captured_artifacts=captured_artifacts,
        )
        if review_input.input_digest != prepared.review_input.input_digest:
            raise LoopReviewServiceError(
                "review-input-drift",
                expected_digest=prepared.review_input.input_digest,
                actual_digest=review_input.input_digest,
            )
        fresh, _ = prepare_current_loop_review(root, loop_type, loop_id)
        if (
            fresh.review_input.round_number != prepared.review_input.round_number
            or fresh.review_input.input_digest != review_input.input_digest
        ):
            raise LoopReviewServiceError(
                "review-input-drift",
                expected_digest=prepared.review_input.input_digest,
                actual_digest=fresh.review_input.input_digest,
            )
        validate_prepared_outcome_for_close(fresh, expected_digest=expected)
    except LoopReviewServiceError as exc:
        raise ReviewInputGuardError(
            exc.reason,
            detail=exc.detail,
            expected_digest=exc.expected_digest or expected,
            actual_digest=exc.actual_digest,
        ) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ReviewInputGuardError(
            "review-input-unavailable",
            detail=str(exc),
            expected_digest=expected,
        ) from exc
    return review_input


def loop_review(
    loop_type: str = typer.Option(..., "--type", help="Loop result type."),
    loop_id: str = typer.Option(..., "--loop-id", help="Existing Loop id."),
    expect_digest: str = typer.Option(
        "",
        "--expect-digest",
        help="Fail if the current substantive input no longer matches this digest.",
    ),
    read_path: str = typer.Option(
        "",
        "--read-path",
        help="Return one artifact's bytes from the same digest-bound read.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print JSON output."),
) -> None:
    """Build or recheck one read-only dynamic-expert review input."""

    try:
        root = find_project_root()
        if root is None:
            raise ValueError("Project is not initialized; .ai-sdlc is missing.")
        expected = expect_digest.strip().lower()
        requested_path = read_path.strip()
        if requested_path and not expected:
            raise ValueError("--read-path requires --expect-digest.")
        prepared, _ = prepare_current_loop_review(root, loop_type, loop_id)
        captured_artifacts: dict[str, bytes] | None = {} if requested_path else None
        review_input = prepared.review_input
        if captured_artifacts is not None:
            review_input = resolve_review_input(
                root,
                loop_type=loop_type,
                loop_id=loop_id,
                review_round_number=prepared.review_input.round_number,
                captured_artifacts=captured_artifacts,
                capture_paths=[requested_path],
            )
            if review_input.input_digest != prepared.review_input.input_digest:
                raise LoopReviewServiceError("review-input-drift")
        if expected and expected != review_input.input_digest:
            _emit(
                {
                    "status": "blocked",
                    "reason": "review-input-drift",
                    "expected_digest": expected,
                    "actual_digest": review_input.input_digest,
                },
                json_output=json_output,
            )
            raise typer.Exit(1)
        snapshot = resolve_b1_review_snapshot(
            root,
            loop_id,
            review_input.round_number,
            **({} if loop_type == "implementation" else {"loop_type": loop_type}),
        )
        if snapshot is not None and snapshot.review_input != review_input:
            raise LoopReviewServiceError("review-input-drift")
    except typer.Exit:
        raise
    except LoopReviewServiceError as exc:
        _emit(exc.payload(), json_output=json_output)
        raise typer.Exit(1) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _emit(
            {
                "status": "blocked",
                "reason": "review-input-unavailable",
                "detail": str(exc),
            },
            json_output=json_output,
        )
        raise typer.Exit(1) from exc

    payload = review_input.model_dump(mode="json")
    payload.update(
        {
            "review_status": prepared.status,
            "review_reason": prepared.reason,
            "next_action": prepared.next_action,
        }
    )
    if prepared.current_outcome and prepared.current_outcome.status == "failed":
        outcome = prepared.current_outcome
        payload["execution_failure"] = {
            "input_digest": outcome.input_digest,
            "kind": outcome.failure_kind,
            "detail": outcome.failure_reason,
        }
    if prepared.current_outcome and prepared.current_outcome.completed_expert_results:
        outcome = prepared.current_outcome
        completed_roles = [
            result.roles[0] for result in outcome.completed_expert_results
        ]
        payload["partial_review"] = {
            "input_digest": outcome.input_digest,
            "completed_roles": completed_roles,
            "uncompleted_roles": [
                role for role in outcome.expert_roles if role not in completed_roles
            ],
            "known_findings": [
                finding.model_dump(mode="json")
                for result in outcome.completed_expert_results
                for finding in result.findings
            ],
        }
    if snapshot is not None:
        capability = snapshot.context.capability
        is_b1 = capability == "implementation-b1"
        payload.update(
            {
                "decision_capability": capability,
                "context_digest": snapshot.context.context_digest,
                "selected_route_id": snapshot.context.selection.selected_id,
                "expert_result_format": (
                    "execution-and-b1-assessment"
                    if is_b1
                    else "execution-and-simulation-assessment"
                ),
                "expert_result_guidance": (
                    "Each independent expert returns an envelope with execution "
                    + (
                        "(ReviewExecution) and assessment (B1Assessment). Completed "
                        if is_b1
                        else "(ReviewExecution) and actual assessment. Completed "
                    )
                    + "executions require every frozen obligation and same-snapshot "
                    "evidence; failed executions require assessment=null."
                ),
            }
        )
        if captured_artifacts is None:
            payload.update(_b1_review_protocol_payload(snapshot))
    if captured_artifacts is not None:
        if len(captured_artifacts) != 1:
            raise typer.Exit(1)
        path, content = next(iter(captured_artifacts.items()))
        payload["review_snapshot"] = _review_snapshot_payload(path, content)
        if snapshot is not None and path in snapshot.manifest:
            payload["review_snapshot"]["sha256"] = snapshot.manifest[path]
    _emit(payload, json_output=json_output)


def _b1_review_protocol_payload(snapshot: B1ReviewSnapshot) -> dict[str, object]:
    """只在完整评审入口公开合同；逐文件读取不重复巨大上下文。"""
    from ai_sdlc.core.loop_review_models import B1ExpertResult

    reviewed = snapshot.review_input
    return {
        "expert_result_schema": B1ExpertResult.model_json_schema(),
        "decision_context": snapshot.context.model_dump(mode="json"),
        "evidence_manifest": dict(snapshot.manifest),
        "snapshot_read_command": [
            "ai-sdlc",
            "loop",
            "review",
            "--type",
            reviewed.loop_type,
            "--loop-id",
            reviewed.loop_id,
            "--expect-digest",
            reviewed.input_digest,
            "--read-path",
            "<manifest-path>",
            "--json",
        ],
        "expert_result_requirements": [
            "Start a fresh read-only context for each selected expert_roles entry; "
            "the implementation author must not prefill actual PASS results.",
            "Each execution has exactly one selected role and its expert_reasons; "
            "completed requires assessment, failed requires assessment=null and "
            "failure_kind/failure_reason, with no findings.",
            "Copy input_digest, context_digest and selected_route_id from this "
            "response; results must cover every frozen obligation exactly once.",
            "Read evidence using snapshot_read_command, replacing <manifest-path> "
            "with an evidence_manifest key. Non-UNKNOWN results require local "
            "evidence IDs bound to those paths and SHA256 values, with a locator "
            "and the actual claim proved; a hash or exit zero is not a business proof.",
            "Evidence IDs must exactly cover results and repair_readiness refs. "
            "Non-UNKNOWN repair readiness requires evidence; missing authority, "
            "facts or verification must not become PASS.",
            "Check actual changes follow the selected mechanism and authorized "
            "scope; unauthorized alternate or extra routes need an important or "
            "blocker finding. Do not submit aggregate scores or inferred permissions.",
            "The schema describes structure; review-record also enforces identity, "
            "coverage, same-snapshot evidence and the bounded review lifecycle.",
        ],
    }


def loop_review_record(
    loop_type: str = typer.Option(..., "--type", help="Loop result type."),
    loop_id: str = typer.Option(..., "--loop-id", help="Existing Loop id."),
    expect_digest: str = typer.Option(
        ...,
        "--expect-digest",
        help="Digest returned by the current review input.",
    ),
    result_paths: list[Path] = typer.Option(
        ...,
        "--result",
        help=(
            "One single-role result per selected expert: legacy ReviewExecution or "
            "quantified {execution, assessment} envelope."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Print JSON output."),
) -> None:
    """Record one bounded independent-expert review round."""

    try:
        root = find_project_root()
        if root is None:
            raise ValueError("Project is not initialized; .ai-sdlc is missing.")
        prepared, loop_dir = prepare_current_loop_review(root, loop_type, loop_id)
        overlay = record_loop_review(
            RecordLoopReviewOptions(
                root=root,
                loop_type=cast(LoopReviewType, loop_type),
                loop_id=loop_id,
                expected_digest=expect_digest,
                result_paths=tuple(result_paths),
            ),
            loop_dir=loop_dir,
            input_resolver=lambda round_number: resolve_review_input(
                root,
                loop_type=loop_type,
                loop_id=loop_id,
                review_round_number=round_number,
            ),
            b1_snapshot_resolver=(
                lambda round_number: resolve_b1_review_snapshot(
                    root, loop_id, round_number, loop_type=loop_type
                )
            ),
        )
    except LoopReviewServiceError as exc:
        _emit(exc.payload(), json_output=json_output)
        raise typer.Exit(1) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _emit(
            {
                "status": "blocked",
                "reason": "review-result-invalid",
                "detail": str(exc),
            },
            json_output=json_output,
        )
        raise typer.Exit(1) from exc

    payload = overlay.model_dump(mode="json")
    payload.update(
        {
            "input_digest": prepared.review_input.input_digest,
            "outcome_path": prepared.outcome_path.relative_to(root).as_posix(),
        }
    )
    _emit(payload, json_output=json_output)


def prepare_current_loop_review(
    root: Path,
    loop_type: str,
    loop_id: str,
) -> tuple[LoopReviewPreparation, Path]:
    """Resolve current Loop identity and derive its bounded review state."""

    safe_loop_id = _safe_identifier(loop_id)
    loop_dir = resolve_review_directory(root, loop_type, safe_loop_id)
    prepared = prepare_loop_review(
        root,
        loop_type=cast(LoopReviewType, loop_type),
        loop_id=safe_loop_id,
        loop_dir=loop_dir,
        input_resolver=lambda round_number: resolve_review_input(
            root,
            loop_type=loop_type,
            loop_id=safe_loop_id,
            review_round_number=round_number,
        ),
        b1_snapshot_resolver=(
            lambda round_number: resolve_b1_review_snapshot(
                root, safe_loop_id, round_number, loop_type=loop_type
            )
        ),
    )
    return prepared, loop_dir


def resolve_review_directory(root: Path, loop_type: str, loop_id: str) -> Path:
    """Return the canonical existing directory after validating its current pointer."""

    if loop_type == "local-pr-review":
        loop_dir, _, _ = _find_local_review_dir(root, loop_id)
        return loop_dir
    if loop_type not in _STAGE_ARTIFACTS:
        raise ValueError(f"Unsupported review Loop type: {loop_type}")
    _resolve_current_stage_state(root, loop_type, loop_id)
    return root / ".ai-sdlc" / "loops" / loop_type / loop_id


def resolve_review_input(
    root: Path,
    *,
    loop_type: str,
    loop_id: str,
    review_round_number: int | None = None,
    captured_artifacts: MutableMapping[str, bytes] | None = None,
    capture_paths: Sequence[str | Path] | None = None,
    capture_all: bool = False,
) -> ReviewInput:
    """Resolve existing substantive artifacts without creating a parallel Loop."""

    safe_loop_id = _safe_identifier(loop_id)
    b1_context = None
    upstream_context = []
    if loop_type == "local-pr-review":
        loop_dir, pointer_path, run_path = _find_local_review_dir(root, safe_loop_id)
        review_pack_path = loop_dir / "review-pack.json"
        review_pack_payload = _read_json_object(root, review_pack_path)
        _require_local_review_verification(root, loop_dir, review_pack_payload)
        artifacts = [
            *(loop_dir / name for name in _LOCAL_REQUIRED),
        ]
        artifacts.extend(
            loop_dir / name for name in _LOCAL_OPTIONAL if (loop_dir / name).is_file()
        )
        diff_path = _local_review_diff(root, review_pack_path)
        artifacts.append(diff_path)
        _, b1_context = _read_pr_stage_context(root, run_path)
        if b1_context is not None:
            artifacts.extend(
                [
                    loop_dir / "decision-context.json",
                    *(root / source.path for source in b1_context.sources),
                ]
            )
        risk_signals = [
            *_content_risk_signals(root, artifacts),
            *_local_review_source_risk_signals(root, review_pack_path),
        ]
        round_number = _read_round_number(root, run_path)
        capture_artifact_paths = (
            list(capture_paths)
            if capture_paths is not None
            else (
                [path for path in artifacts if path != diff_path]
                if captured_artifacts is not None
                else []
            )
        )
        capture_only_paths = (
            [pointer_path, run_path]
            if captured_artifacts is not None and capture_paths is None
            else []
        )
    elif loop_type in _STAGE_ARTIFACTS:
        loop_dir = root / ".ai-sdlc" / "loops" / loop_type / safe_loop_id
        _, run_path = _resolve_current_stage_state(
            root,
            loop_type,
            safe_loop_id,
        )
        stage_source_material = _stage_source_material(root, loop_type, loop_dir)
        from ai_sdlc.cli.loop_stage_cmd import read_stage_decision_context

        b1_context = read_stage_decision_context(
            root,
            loop_type,
            safe_loop_id,
            purpose="review",
            round_number=review_round_number or 1,
        )
        if loop_type == "implementation":
            reject_retired_implementation_continuation(root, safe_loop_id)
        if b1_context is not None:
            stage_source_material = _unique_paths(
                [
                    *stage_source_material,
                    loop_dir / "decision-context.json",
                    *(root / source.path for source in b1_context.sources),
                ]
            )
        artifacts = _unique_paths(
            [
                *(loop_dir / name for name in _STAGE_ARTIFACTS[loop_type]),
                *stage_source_material,
            ]
        )
        upstream_context = _exclude_paths(
            _stage_upstream_context(root, loop_type, loop_dir),
            excluded=artifacts,
        )
        if b1_context is not None and b1_context.capability == "stage-simulation-v1":
            from ai_sdlc.core.loop_stage_decision_service import (
                validate_stage_source_boundary,
            )

            validate_stage_source_boundary(
                root, [root / source.path for source in b1_context.sources]
            )
        elif b1_context is not None:
            _require_b1_material_boundary(
                root,
                [*artifacts, *upstream_context],
            )
        risk_signals = _content_risk_signals(
            root,
            [*artifacts, *upstream_context],
        )
        round_number = _read_round_number(root, run_path)
        capture_artifact_paths = (
            list(capture_paths)
            if capture_paths is not None
            else (
                [
                    *(loop_dir / name for name in _STAGE_CLOSE_ARTIFACTS[loop_type]),
                    *(stage_source_material if loop_type == "design-contract" else []),
                ]
                if captured_artifacts is not None
                else []
            )
        )
        capture_only_paths = (
            [run_path]
            if captured_artifacts is not None and capture_paths is None
            else []
        )
        if (
            b1_context is not None
            and captured_artifacts is not None
            and capture_paths is None
        ):
            capture_artifact_paths.append(loop_dir / "decision-context.json")
    else:
        raise ValueError(f"Unsupported review Loop type: {loop_type}")

    if review_round_number is not None:
        if review_round_number not in {1, 2}:
            raise ValueError("Review round number must be 1 or 2.")
        round_number = review_round_number

    if capture_all:
        if captured_artifacts is None:
            raise ValueError("capture_all requires captured_artifacts")
        capture_artifact_paths = [
            *artifacts,
            *(upstream_context if loop_type != "local-pr-review" else []),
        ]
        capture_only_paths = [run_path]
    # 量化模式以生成摘要的同次读取校验身份；不在读取后另开文件拼证据。
    target_captures = captured_artifacts
    if b1_context is not None:
        target_captures = {}
    reviewed = build_review_input(
        root,
        loop_id=safe_loop_id,
        loop_type=cast(LoopReviewType, loop_type),
        round_number=round_number,
        artifact_paths=artifacts,
        upstream_context_paths=upstream_context
        if loop_type != "local-pr-review"
        else [],
        risk_signals=risk_signals,
        capture_artifact_paths=(
            [*artifacts, *upstream_context, *capture_artifact_paths]
            if b1_context is not None
            else capture_artifact_paths
        ),
        capture_only_paths=(
            _unique_paths([run_path, *capture_only_paths])
            if b1_context is not None
            else capture_only_paths
        ),
        captured_artifacts=target_captures,
    )
    if b1_context is not None:
        assert target_captures is not None
        context = _captured_quantified_context(
            root, loop_type, loop_dir, target_captures
        )
        if context != b1_context:
            raise ValueError("decision-context-drift")
        material = {*reviewed.artifact_paths, *reviewed.upstream_context_paths}
        if any(source.path not in material for source in context.sources):
            raise ValueError("decision-source-missing-from-snapshot")
        if captured_artifacts is not None:
            requested = [*capture_artifact_paths, *capture_only_paths]
            for path in requested:
                relative = (
                    _lexical_path(
                        Path(path) if Path(path).is_absolute() else root / path
                    )
                    .relative_to(root.resolve())
                    .as_posix()
                )
                captured_artifacts[relative] = target_captures[relative]
    return reviewed


def resolve_b1_review_snapshot(
    root: Path, loop_id: str, round_number: int, *, loop_type: str = "implementation"
) -> B1ReviewSnapshot | None:
    """共用同次读取的量化合同与实际材料；旧模式不增加协议字段。"""
    from ai_sdlc.core.loop_decision_service import B1ReviewSnapshot

    safe_loop_id = _safe_identifier(loop_id)
    if loop_type == "local-pr-review":
        return _pr_stage_review_snapshot(root, safe_loop_id, round_number)
    from ai_sdlc.cli.loop_stage_cmd import (
        read_stage_decision_context,
        stage_review_snapshot,
    )

    context = read_stage_decision_context(
        root, loop_type, safe_loop_id, purpose="review", round_number=round_number
    )
    if context is None:
        return None
    if context.capability == "stage-simulation-v1":
        return stage_review_snapshot(root, loop_type, safe_loop_id, round_number)
    loop_dir = root / ".ai-sdlc" / "loops" / "implementation" / safe_loop_id
    captured: dict[str, bytes] = {}
    reviewed = resolve_review_input(
        root,
        loop_type="implementation",
        loop_id=safe_loop_id,
        review_round_number=round_number,
        captured_artifacts=captured,
        capture_all=True,
    )
    context = _captured_implementation_decision_context(root, loop_dir, captured)
    return B1ReviewSnapshot(
        review_input=reviewed,
        context=context,
        manifest={
            path: hashlib.sha256(captured[path]).hexdigest()
            for path in (*reviewed.artifact_paths, *reviewed.upstream_context_paths)
        },
    )


def _captured_quantified_context(root, stage, directory, captured):
    if stage == "local-pr-review":
        from ai_sdlc.core.loop_stage_decision_service import (
            parse_stage_simulation_context,
        )
        from ai_sdlc.core.pr_review_decision import validate_captured_pr_review_context
        from ai_sdlc.core.pr_review_models import ReviewRun

        run_path = directory / "review-run.json"
        return validate_captured_pr_review_context(
            ReviewRun.model_validate_json(
                captured[run_path.relative_to(root).as_posix()]
            ),
            parse_stage_simulation_context(
                captured[
                    (directory / "decision-context.json").relative_to(root).as_posix()
                ]
            ),
        )
    run = json.loads(
        captured[(directory / "loop-run.json").relative_to(root).as_posix()]
    )
    if run.get("decision_capability") == "stage-simulation-v1":
        from ai_sdlc.cli.loop_stage_cmd import captured_stage_decision_context

        return captured_stage_decision_context(root, stage, directory, captured)
    return _captured_implementation_decision_context(root, directory, captured)


def _pr_stage_review_snapshot(root, loop_id, round_number):
    import time

    from ai_sdlc.core.loop_stage_decision_service import StageReviewSnapshot
    from ai_sdlc.core.pr_review_decision import pr_review_source_digest
    from ai_sdlc.core.pr_review_models import ReviewRun

    directory, _, run_path = _find_local_review_dir(root, loop_id)
    run, context = _read_pr_stage_context(root, run_path)
    if context is None:
        return None
    before = pr_review_source_digest(root, run, context.sources)
    captured = {}
    reviewed = resolve_review_input(
        root,
        loop_type="local-pr-review",
        loop_id=loop_id,
        review_round_number=round_number,
        captured_artifacts=captured,
        capture_all=True,
    )
    parsed = _captured_quantified_context(root, "local-pr-review", directory, captured)
    fresh = ReviewRun.model_validate(_read_json_object(root, run_path))
    if (
        fresh != run
        or parsed != context
        or pr_review_source_digest(root, fresh, parsed.sources) != before
    ):
        raise ValueError("review-input-drift")
    return StageReviewSnapshot(
        review_input=reviewed,
        context=parsed,
        manifest={
            path: hashlib.sha256(captured[path]).hexdigest()
            for path in reviewed.artifact_paths
        },
        source_digest=before,
        observed_at_ms=time.time_ns() // 1_000_000,
        context_path=(directory / "decision-context.json").relative_to(root).as_posix(),
    )


def _read_pr_stage_context(root, run_path):
    from ai_sdlc.core.pr_review_decision import validate_pr_review_context
    from ai_sdlc.core.pr_review_models import ReviewRun

    raw = _read_json_object(root, run_path)
    if raw.get("decision_mode", "legacy") == "legacy":
        if (
            raw.get("decision_capability")
            or (run_path.parent / "decision-context.json").exists()
        ):
            raise ValueError("decision-context-conflicts-with-legacy")
        return None, None
    run = ReviewRun.model_validate(raw)
    return run, validate_pr_review_context(root, run, purpose="review")


def _read_implementation_decision_context(
    root: Path, loop_dir: Path
) -> DecisionContext | SimulationContext | None:
    from ai_sdlc.core.implementation_models import ImplementationInput
    from ai_sdlc.core.loop_decision_service import validate_implementation_context
    from ai_sdlc.core.loop_models import LoopRun

    run = _read_json_object(root, loop_dir / "loop-run.json")
    impl_input = _read_json_object(root, loop_dir / "implementation-input.json")
    return validate_implementation_context(
        root,
        LoopRun.model_validate(run),
        ImplementationInput.model_validate(impl_input),
        purpose="review",
    )


def _captured_implementation_decision_context(
    root: Path, loop_dir: Path, captured: MutableMapping[str, bytes]
) -> DecisionContext | SimulationContext:
    from ai_sdlc.core.implementation_models import ImplementationInput
    from ai_sdlc.core.loop_decision_service import (
        parse_implementation_context,
        validate_captured_implementation_context,
    )
    from ai_sdlc.core.loop_models import LoopRun

    def content(name: str) -> bytes:
        return captured[(loop_dir / name).relative_to(root).as_posix()]

    return validate_captured_implementation_context(
        LoopRun.model_validate_json(content("loop-run.json")),
        ImplementationInput.model_validate_json(content("implementation-input.json")),
        parse_implementation_context(content("decision-context.json")),
    )


def _require_b1_material_boundary(root: Path, paths: Sequence[Path]) -> None:

    validate_implementation_source_boundary(root, paths)


def _require_local_review_verification(
    root: Path,
    loop_dir: Path,
    review_pack: dict[str, object],
) -> None:
    diff_source = review_pack.get("diff_source", {})
    if not isinstance(diff_source, dict):
        raise ValueError("Review pack diff_source is invalid.")
    if diff_source.get("source_kind") != "local-staged":
        return
    evidence_path = loop_dir / "verification-evidence.json"
    if not evidence_path.is_file():
        raise ValueError("Executable Local PR verification evidence is missing.")
    try:
        evidence = PRReviewVerificationEvidence.model_validate(
            _read_json_object(root, evidence_path)
        )
    except ValueError as exc:
        raise ValueError("Local PR verification evidence is invalid.") from exc
    expected_tree = review_pack.get("staged_tree_oid", "")
    if (
        not isinstance(expected_tree, str)
        or not expected_tree
        or evidence.staged_tree_oid != expected_tree
    ):
        raise ValueError("Local PR verification evidence is bound to another tree.")
    if evidence.entries or not any(
        result.successful and result.source_digest_before == result.source_digest_after
        for result in evidence.results
    ):
        raise ValueError("Local PR requires successful executable verification.")


def _review_snapshot_payload(path: str, content: bytes) -> dict[str, str]:
    try:
        rendered = content.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": path,
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }
    return {
        "path": path,
        "encoding": "utf-8",
        "content": rendered,
    }


def _stage_upstream_context(
    root: Path,
    loop_type: str,
    loop_dir: Path,
    *,
    visited: frozenset[tuple[str, str]] = frozenset(),
) -> list[Path]:
    predecessor = _STAGE_PREDECESSORS.get(loop_type)
    if predecessor is None:
        return []
    input_name, id_field, predecessor_type = predecessor
    input_path = loop_dir / input_name
    try:
        payload = json.loads(read_stable_text(root, input_path, encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Loop input is unreadable: {input_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Loop input root must be an object: {input_path}")
    raw_loop_id = payload.get(id_field, "")
    if not isinstance(raw_loop_id, str):
        raise ValueError(f"Loop predecessor id is invalid: {input_path}")
    predecessor_id = raw_loop_id.strip()
    if not predecessor_id:
        if loop_type == "design-contract":
            return []
        raise ValueError(f"Loop predecessor id is missing: {input_path}")
    safe_predecessor_id = _safe_identifier(predecessor_id)
    identity = (predecessor_type, safe_predecessor_id)
    if identity in visited:
        raise ValueError(
            f"Loop predecessor cycle detected: {predecessor_type}/{safe_predecessor_id}"
        )
    predecessor_dir = (
        root / ".ai-sdlc" / "loops" / predecessor_type / safe_predecessor_id
    )
    inherited = _stage_upstream_context(
        root,
        predecessor_type,
        predecessor_dir,
        visited=visited | {identity},
    )
    predecessor_artifacts = [
        *(predecessor_dir / name for name in _STAGE_ARTIFACTS[predecessor_type]),
        *_stage_source_material(root, predecessor_type, predecessor_dir),
    ]
    predecessor_input = _read_json_object(
        root, predecessor_dir / _STAGE_ARTIFACTS[predecessor_type][0]
    )
    if predecessor_input.get("decision_capability") == "stage-simulation-v1":
        # 上游量化决定及实际关闭属于本阶段依据；不把自身 Close 加入自身评审摘要。
        from ai_sdlc.core.loop_stage_decision_service import (
            parse_stage_simulation_context,
        )
        from ai_sdlc.core.stable_file_read import read_stable_bytes

        context_path = predecessor_dir / "decision-context.json"
        context = parse_stage_simulation_context(read_stable_bytes(root, context_path))
        if (
            context.loop_type != predecessor_type
            or context.loop_id != safe_predecessor_id
            or context.phase != "review_sealed"
        ):
            raise ValueError("upstream-simulation-context-identity-mismatch")
        close_name = (
            "requirement-freeze.json"
            if predecessor_type == "requirement"
            else f"{predecessor_type}-close.json"
        )
        predecessor_artifacts.extend(
            [
                context_path,
                predecessor_dir / close_name,
                *(
                    outcome_path(predecessor_dir, number)
                    for number in (1, 2)
                    if outcome_path(predecessor_dir, number).is_file()
                ),
                *(root / source.path for source in context.sources),
            ]
        )
    if predecessor_type == "implementation":
        reject_retired_implementation_continuation(root, safe_predecessor_id)
    return _unique_paths([*inherited, *predecessor_artifacts])


def _stage_source_material(root: Path, loop_type: str, loop_dir: Path) -> list[Path]:
    if loop_type == "requirement":
        return []
    if loop_type == "design-contract":
        payload = _read_json_object(root, loop_dir / "design-contract-input.json")
        return [
            _repo_path(root, value, field_name)
            for field_name in ("spec_path", "plan_path", "tasks_path")
            if isinstance((value := payload.get(field_name)), str) and value.strip()
        ]
    if loop_type == "implementation":
        payload = _read_json_object(root, loop_dir / "implementation-input.json")
        declared_scope = payload.get("declared_scope", [])
        if not isinstance(declared_scope, list) or not all(
            isinstance(item, str) for item in declared_scope
        ):
            raise ValueError(
                f"Loop declared_scope is invalid: {loop_dir / 'implementation-input.json'}"
            )
        return _unique_paths(
            [
                *_expand_repo_patterns(root, declared_scope),
                *_implementation_evidence_material(root, loop_dir),
            ]
        )
    if loop_type == "frontend-evidence":
        input_payload = _read_json_object(
            root, loop_dir / "frontend-evidence-input.json"
        )
        snapshot_payload = _read_json_object(
            root, loop_dir / "frontend-evidence-snapshot.json"
        )
        referenced: list[Path] = []
        baseline_root = snapshot_payload.get("visual_baseline_root", "")
        if isinstance(baseline_root, str) and baseline_root.strip():
            baseline_identity = {
                "root": baseline_root,
                "image_path": snapshot_payload.get("visual_baseline_image_path", ""),
                "metadata_path": snapshot_payload.get(
                    "visual_baseline_metadata_path", ""
                ),
                "digest": snapshot_payload.get("visual_baseline_digest", ""),
            }
            validated_baseline = validate_frontend_visual_baseline_identity(
                root,
                baseline_identity,
                expected_root=baseline_root,
            )
            referenced.extend(
                _repo_path(root, validated_baseline[field_name], field_name)
                for field_name in ("image_path", "metadata_path")
            )
        source_path = input_payload.get("source_artifact_path", "")
        if isinstance(source_path, str) and source_path.strip():
            referenced.append(_repo_path(root, source_path, "source_artifact_path"))
        records = snapshot_payload.get("artifact_records", [])
        if not isinstance(records, list):
            raise ValueError(
                "Frontend evidence artifact_records must be a list: "
                f"{loop_dir / 'frontend-evidence-snapshot.json'}"
            )
        for record in records:
            if (
                not isinstance(record, dict)
                or record.get("capture_status") != "captured"
            ):
                continue
            artifact_ref = record.get("artifact_ref", "")
            if isinstance(artifact_ref, str) and artifact_ref.strip():
                referenced.append(_repo_path(root, artifact_ref, "artifact_ref"))
        for field_name in ("screenshot_refs", "trace_refs"):
            refs = snapshot_payload.get(field_name, [])
            if not isinstance(refs, list):
                raise ValueError(
                    f"Frontend evidence {field_name} must be a list: "
                    f"{loop_dir / 'frontend-evidence-snapshot.json'}"
                )
            for ref in refs:
                if isinstance(ref, str) and ref.strip():
                    referenced.append(_repo_path(root, ref, field_name))
        return _unique_paths(referenced)
    return []


def _implementation_evidence_material(root: Path, loop_dir: Path) -> list[Path]:
    payload = _read_json_object(root, loop_dir / "verification-evidence.json")
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError(
            "Implementation verification evidence tasks must be a list: "
            f"{loop_dir / 'verification-evidence.json'}"
        )
    referenced: list[Path] = []
    resolved_root = root.resolve()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError(
                "Implementation verification evidence task must be an object: "
                f"{loop_dir / 'verification-evidence.json'}"
            )
        evidence = task.get("evidence", [])
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise ValueError(
                "Implementation verification evidence paths must be strings: "
                f"{loop_dir / 'verification-evidence.json'}"
            )
        for item in evidence:
            text = item.strip()
            if not text:
                continue
            candidate = Path(text)
            unresolved = (
                candidate if candidate.is_absolute() else resolved_root / candidate
            )
            lexical = _lexical_path(unresolved)
            try:
                lexical.relative_to(resolved_root)
            except ValueError:
                continue
            referenced.extend(_expand_review_material(lexical))
    return _unique_paths(referenced)


def _local_review_diff(root: Path, review_pack_path: Path) -> Path:
    payload = _read_json_object(root, review_pack_path)
    diff_path_text = payload.get("diff_path", "")
    diff_digest = payload.get("diff_digest", "")
    if not isinstance(diff_path_text, str) or not diff_path_text.strip():
        raise ValueError(f"Review pack diff_path is missing: {review_pack_path}")
    if not isinstance(diff_digest, str) or not diff_digest.startswith("sha256:"):
        raise ValueError(f"Review pack diff_digest is invalid: {review_pack_path}")
    diff_path = _repo_path(root, diff_path_text, "diff_path")
    try:
        actual = _file_sha256(root, diff_path)
    except OSError as exc:
        raise ValueError(f"Review diff is unreadable: {diff_path}") from exc
    if diff_digest != f"sha256:{actual}":
        raise ValueError(
            f"Review diff digest does not match review-pack.json: {diff_path}"
        )
    return diff_path


def _read_json_object(root: Path, path: Path) -> dict[str, object]:
    try:
        payload = json.loads(read_stable_text(root, path, encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Loop input is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Loop input root must be an object: {path}")
    return payload


def _repo_path(root: Path, value: str, field_name: str) -> Path:
    resolved_root = root.resolve()
    candidate = Path(value)
    unresolved = candidate if candidate.is_absolute() else resolved_root / candidate
    lexical = Path(os.path.abspath(unresolved))
    try:
        lexical.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Loop {field_name} escapes the project: {value}") from exc
    try:
        lexical.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Loop {field_name} escapes the project: {value}") from exc
    return lexical


def _expand_repo_patterns(root: Path, patterns: list[str]) -> list[Path]:
    expanded: list[Path] = []
    for pattern in patterns:
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ValueError(f"Loop declared_scope escapes the project: {pattern}")
        matches = sorted(root.glob(pattern))
        for match in matches:
            resolved = _repo_path(root, str(match), "declared_scope")
            expanded.extend(_expand_review_material(resolved))
    return _unique_paths(expanded)


def _expand_review_material(path: Path) -> list[Path]:
    if path.is_symlink():
        return [path]
    if path.is_dir():
        return [
            nested
            for nested in sorted(path.rglob("*"))
            if nested.is_symlink() or nested.is_file()
        ]
    if path.is_file():
        return [path]
    return []


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: dict[Path, Path] = {}
    for path in paths:
        unique.setdefault(_lexical_path(path), path)
    return list(unique.values())


def _file_sha256(root: Path, path: Path) -> str:
    digest = hashlib.sha256()
    consume_stable_chunks(root, path, digest.update)
    return digest.hexdigest()


def _exclude_paths(paths: list[Path], *, excluded: list[Path]) -> list[Path]:
    excluded_keys = {_lexical_path(path) for path in excluded}
    return [path for path in paths if _lexical_path(path) not in excluded_keys]


def _lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _resolve_current_stage_state(
    root: Path,
    loop_type: str,
    loop_id: str,
) -> tuple[Path, Path]:
    pointer_path = (
        root / ".ai-sdlc" / "loops" / loop_type / _STAGE_POINTER_NAMES[loop_type]
    )
    pointer = _read_json_object(root, pointer_path)
    if pointer.get("loop_id") != loop_id:
        raise ValueError(
            f"Current {loop_type} review does not identify Loop {loop_id}."
        )
    raw_run_path = pointer.get("loop_run_path", "")
    if not isinstance(raw_run_path, str) or not raw_run_path.strip():
        raise ValueError(f"Current {loop_type} Loop run path is missing.")
    run_path = _repo_path(root, raw_run_path, "loop_run_path")
    expected_run_path = (
        root / ".ai-sdlc" / "loops" / loop_type / loop_id / "loop-run.json"
    )
    if _lexical_path(run_path) != _lexical_path(expected_run_path):
        raise ValueError(f"Current {loop_type} Loop run path is not canonical.")
    run = _read_json_object(root, run_path)
    if run.get("loop_id") != loop_id or run.get("loop_type") != loop_type:
        raise ValueError(
            f"Current {loop_type} Loop run does not identify Loop {loop_id}."
        )
    return pointer_path, run_path


def _find_local_review_dir(
    root: Path,
    loop_id: str,
) -> tuple[Path, Path, Path]:
    pointer_path = root / _CURRENT_LOCAL_REVIEW
    pointer = _read_json_object(root, pointer_path)
    if pointer.get("loop_id") != loop_id:
        raise ValueError(f"Current local PR review does not identify Loop {loop_id}.")
    raw_review_id = pointer.get("review_id", "")
    raw_run_path = pointer.get("review_run_path", "")
    if not isinstance(raw_review_id, str) or not raw_review_id.strip():
        raise ValueError("Current local PR review id is missing.")
    if not isinstance(raw_run_path, str) or not raw_run_path.strip():
        raise ValueError("Current local PR review run path is missing.")
    review_id = _safe_identifier(raw_review_id)
    run_path = _repo_path(root, raw_run_path, "review_run_path")
    expected_run_path = (
        root / ".ai-sdlc" / "reviews" / "pr" / review_id / "review-run.json"
    )
    if _lexical_path(run_path) != _lexical_path(expected_run_path):
        raise ValueError("Current local PR review run path is not canonical.")
    run = _read_json_object(root, run_path)
    if run.get("review_id") != review_id or run.get("loop_id") != loop_id:
        raise ValueError(
            f"Current local PR review run does not identify Loop {loop_id}."
        )
    for field_name, filename in (
        ("review_pack_path", "review-pack.json"),
        ("findings_path", "findings.json"),
    ):
        raw_artifact_path = run.get(field_name, "")
        if raw_artifact_path in {None, ""}:
            continue
        if not isinstance(raw_artifact_path, str):
            raise ValueError(f"Current local PR review {field_name} is invalid.")
        artifact_path = _repo_path(root, raw_artifact_path, field_name)
        expected_artifact_path = run_path.parent / filename
        if _lexical_path(artifact_path) != _lexical_path(expected_artifact_path):
            raise ValueError(f"Current local PR review {field_name} is not canonical.")
    return run_path.parent, pointer_path, run_path


def _read_round_number(root: Path, path: Path) -> int:
    try:
        payload = json.loads(read_stable_text(root, path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Loop state is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Loop state root must be an object: {path}")
    value = payload.get("current_round", payload.get("round_number", 1))
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _content_risk_signals(root: Path, paths: list[Path]) -> list[str]:
    detected: set[str] = set()
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"review path is not a regular file: {path}")
        suffix = path.suffix.lower()
        if suffix in _BINARY_RISK_SUFFIXES:
            continue
        try:
            detected.update(
                _stream_text_risk_signals(
                    root,
                    path,
                    strict_text=suffix in _TEXT_RISK_SUFFIXES,
                )
            )
        except OSError as exc:
            raise ValueError(f"Review artifact is unreadable: {path}") from exc
    risks = [risk for risk in _RISK_TERMS if risk in detected]
    return risks or ["general-correctness"]


def _stream_text_risk_signals(
    root: Path,
    path: Path,
    *,
    strict_text: bool,
) -> set[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    detected: set[str] = set()
    tail = ""
    left_truncated = False

    def scan_chunk(chunk: bytes) -> None:
        nonlocal tail, left_truncated
        combined = tail + decoder.decode(chunk).lower()
        detected.update(
            _matching_risk_signals(
                combined,
                eof=False,
                left_truncated=left_truncated,
            )
        )
        left_truncated = left_truncated or len(combined) > _RISK_SCAN_OVERLAP
        tail = combined[-_RISK_SCAN_OVERLAP:]

    try:
        consume_stable_chunks(root, path, scan_chunk)
        combined = tail + decoder.decode(b"", final=True).lower()
    except UnicodeDecodeError as exc:
        if strict_text:
            raise ValueError(
                f"Review text artifact is not strict UTF-8: {path}"
            ) from exc
        return set()
    detected.update(
        _matching_risk_signals(
            combined,
            eof=True,
            left_truncated=left_truncated,
        )
    )
    return detected


def _matching_risk_signals(
    content: str,
    *,
    eof: bool,
    left_truncated: bool,
) -> set[str]:
    detected: set[str] = set()
    for risk, terms in _RISK_TERMS.items():
        for term in terms:
            pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
            for match in re.finditer(pattern, content):
                if left_truncated and match.start() == 0:
                    continue
                if eof or match.end() < len(content):
                    detected.add(risk)
                    break
            if risk in detected:
                break
    return detected


def _local_review_source_risk_signals(root: Path, review_pack_path: Path) -> list[str]:
    payload = _read_json_object(root, review_pack_path)
    diff_source = payload.get("diff_source")
    if diff_source is None:
        return []
    if not isinstance(diff_source, dict):
        raise ValueError(f"Review pack diff_source is invalid: {review_pack_path}")
    source_kind = diff_source.get("source_kind", "")
    if source_kind == "patch":
        patch_file = diff_source.get("patch_file", "")
        head_ref = diff_source.get("head_ref", payload.get("head_ref", "HEAD"))
        if not isinstance(patch_file, str) or not patch_file.strip():
            raise ValueError(f"Review pack patch_file is invalid: {review_pack_path}")
        if not isinstance(head_ref, str) or not head_ref.strip():
            raise ValueError(
                f"Review pack patch head_ref is invalid: {review_pack_path}"
            )
        snapshot = build_source_snapshot(
            SourceSnapshotOptions(
                root=root,
                source_kind=source_kind,
                head_ref=head_ref.strip(),
                patch_file=patch_file.strip(),
            )
        )
        return [
            f"git-selected-source:{source_kind}",
            f"git-selected-head-tip:{snapshot.head_commit}",
            f"git-selected-patch:{snapshot.source_input_digest}",
            f"git-selected-diff:{snapshot.diff_hash}",
        ]
    if source_kind == "local-git-range":
        base_ref = diff_source.get("base_ref", payload.get("base_ref", ""))
        head_ref = diff_source.get("head_ref", payload.get("head_ref", "HEAD"))
        if not isinstance(base_ref, str) or not base_ref.strip():
            raise ValueError(
                f"Review pack local-git-range base_ref is invalid: {review_pack_path}"
            )
        if not isinstance(head_ref, str) or not head_ref.strip():
            raise ValueError(
                f"Review pack local-git-range head_ref is invalid: {review_pack_path}"
            )
        base_ref = base_ref.strip()
        head_ref = head_ref.strip()
        snapshot = build_source_snapshot(
            SourceSnapshotOptions(
                root=root,
                source_kind=source_kind,
                base_ref=base_ref,
                head_ref=head_ref,
            )
        )
        base_tip = (
            _git_bytes(
                root,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{base_ref}^{{commit}}",
            )
            .decode("ascii")
            .strip()
        )
        head_tip = (
            _git_bytes(
                root,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{head_ref}^{{commit}}",
            )
            .decode("ascii")
            .strip()
        )
        return [
            f"git-selected-source:{source_kind}",
            f"git-selected-base-tip:{base_tip}",
            f"git-selected-head-tip:{head_tip}",
            f"git-selected-diff:{snapshot.diff_hash}",
        ]
    if source_kind == "local-staged":
        head_commit = payload.get("head_commit", "")
        staged_tree_oid = payload.get("staged_tree_oid", "")
        diff_hash = diff_source.get("patch_hash", "")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (head_commit, staged_tree_oid, diff_hash)
        ):
            raise ValueError(
                f"Review pack staged source identity is invalid: {review_pack_path}"
            )
        return [
            "git-selected-source:local-staged",
            f"git-selected-head:{head_commit}",
            f"git-selected-tree:{staged_tree_oid}",
            f"git-selected-diff:{diff_hash}",
        ]
    if source_kind != "local-unstaged":
        return []
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=root, source_kind=source_kind)
    )
    return [
        f"git-selected-source:{source_kind}",
        f"git-selected-diff:{snapshot.diff_hash}",
    ]


def _git_bytes(root: Path, *args: str) -> bytes:
    env = {
        key: value for key, value in os.environ.items() if key not in _GIT_ROUTING_ENV
    }
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "--no-replace-objects",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    return result.stdout


def _safe_identifier(value: str) -> str:
    text = value.strip()
    if not text or text in {".", ".."} or any(char in text for char in "/\\:"):
        raise ValueError(f"Unsafe Loop id: {value!r}")
    return text


def _emit(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    for key, value in payload.items():
        typer.echo(f"{key}: {value}")


__all__ = ["loop_review", "resolve_review_input"]
