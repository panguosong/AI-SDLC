"""需求 R1 修复依据的追加入口；不改历史判断、不新增质量评审轮次。"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, cast

import typer

from ai_sdlc.cli.loop_review_cmd import (
    _emit,
    _review_snapshot_payload,
    _safe_identifier,
    resolve_b1_review_snapshot,
    resolve_review_directory,
    resolve_review_input,
)
from ai_sdlc.core.loop_review_service import (
    LoopReviewServiceError,
    RecordLoopReviewOptions,
)
from ai_sdlc.core.review_kernel import LoopReviewType
from ai_sdlc.core.stable_file_read import read_stable_bytes
from ai_sdlc.utils.helpers import find_project_root


def _repair_arguments(
    loop_type: str, loop_id: str, evidence_paths: list[Path]
) -> tuple[Path, dict[str, Any]]:
    """统一使用原生当前实例解析，不能借请求参数选择历史或替代实例。"""
    root = find_project_root()
    if root is None:
        raise ValueError("Project is not initialized; .ai-sdlc is missing.")
    loop_id = _safe_identifier(loop_id)
    directory = resolve_review_directory(root, loop_type, loop_id)
    return root, {
        "loop_dir": directory,
        "evidence_paths": tuple(evidence_paths),
        "input_resolver": lambda number: resolve_review_input(
            root, loop_type=loop_type, loop_id=loop_id, review_round_number=number
        ),
        "b1_snapshot_resolver": lambda number: resolve_b1_review_snapshot(
            root, loop_id, number, loop_type=loop_type
        ),
    }


def loop_review_repair_prepare(
    loop_type: str = typer.Option(..., "--type", help="Only requirement is supported."),
    loop_id: str = typer.Option(..., "--loop-id", help="Original blocked Loop id."),
    evidence_paths: list[Path] = typer.Option(
        ..., "--evidence", help="New project-local repair basis; repeat for each file."
    ),
    expect_digest: str | None = typer.Option(
        None, "--expect-digest", help="Bind the previously prepared repair basis."
    ),
    read_path: str | None = typer.Option(
        None,
        "--read-path",
        help="Read one new evidence snapshot; requires --expect-digest.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Prepare a read-only readiness supplement for the remaining original R2."""
    from ai_sdlc.core.loop_repair_readiness import (
        RepairReadinessExpertResult,
        prepare_repair_readiness,
    )

    try:
        root, kwargs = _repair_arguments(loop_type, loop_id, evidence_paths)
        prepared = prepare_repair_readiness(
            root, loop_type=loop_type, loop_id=loop_id, **kwargs
        )
        if expect_digest is not None and expect_digest != prepared.prepare_digest:
            raise LoopReviewServiceError("repair-readiness-input-drift")
        payload = prepared.model_dump(mode="json")
        if read_path is not None:
            if expect_digest is None:
                raise LoopReviewServiceError("repair-readiness-read-digest-required")
            if read_path not in prepared.evidence_manifest:
                raise LoopReviewServiceError("repair-readiness-evidence-path-invalid")
            content = read_stable_bytes(root, root / read_path)
            # 返回同次捕获字节，并在输出前确认全体绑定没有在读取期间漂移。
            repeated = prepare_repair_readiness(
                root, loop_type=loop_type, loop_id=loop_id, **kwargs
            )
            if (
                repeated.prepare_digest != prepared.prepare_digest
                or hashlib.sha256(content).hexdigest()
                != prepared.evidence_manifest[read_path]
            ):
                raise LoopReviewServiceError("repair-readiness-input-drift")
            payload["review_snapshot"] = _review_snapshot_payload(read_path, content)
        else:
            payload["expert_result_schema"] = (
                RepairReadinessExpertResult.model_json_schema()
            )
            payload["expert_result_requirements"] = [
                "Keep new supplementary files under .ai-sdlc/reviews so adding repair evidence does not mutate the frozen business source. They remain immutable dependencies after recording.",
                "Use one fresh independent read-only context for each original expert role. The author must not supply readiness PASS judgments.",
                "Read the original R1 through loop review with its first_input_digest, and each new evidence file through this command with --expect-digest and --read-path.",
                "Assess only authorization, facts and verification for the required repair. Do not alter obligation results, findings, scores, the contract, or the review round count.",
                "Authorization describes the host author's evidenced scope, not the expert's read-only execution mode. A policy refusal or explicit denied authority cannot be overridden.",
                "Facts and verification describe a concrete repair and how it will be checked at this stage; do not require an unimplemented product to have already passed runtime acceptance.",
                "All three dimensions must be PASS with evidence before the existing repair and R2 can proceed. Missing evidence remains UNKNOWN; this command does not close the Loop.",
            ]
        payload["status"] = "ready-for-readiness-review"
        _emit(payload, json_output=json_output)
    except LoopReviewServiceError as exc:
        _emit(exc.payload(), json_output=json_output)
        raise typer.Exit(1) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _emit(
            {
                "status": "blocked",
                "reason": "repair-readiness-input-invalid",
                "detail": str(exc),
            },
            json_output=json_output,
        )
        raise typer.Exit(1) from exc


def loop_review_repair_record(
    loop_type: str = typer.Option(..., "--type", help="Only requirement is supported."),
    loop_id: str = typer.Option(..., "--loop-id", help="Original blocked Loop id."),
    evidence_paths: list[Path] = typer.Option(
        ..., "--evidence", help="Exact new evidence files used by prepare."
    ),
    expect_digest: str = typer.Option(
        ..., "--expect-digest", help="Prepared repair basis digest."
    ),
    result_paths: list[Path] = typer.Option(
        ..., "--result", help="One independent readiness judgment per original role."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Append verified repair readiness while preserving the complete original R1."""
    from ai_sdlc.core.loop_repair_readiness import record_repair_readiness

    try:
        root, kwargs = _repair_arguments(loop_type, loop_id, evidence_paths)
        supplement = record_repair_readiness(
            RecordLoopReviewOptions(
                root=root,
                loop_type=cast(LoopReviewType, loop_type),
                loop_id=loop_id,
                expected_digest=expect_digest,
                result_paths=tuple(result_paths),
            ),
            **kwargs,
        )
        payload = supplement.model_dump(mode="json")
        payload.update(
            status="repair-readiness-recorded",
            next_action=(
                "Repair the original requirement within the frozen scope, then run the original round 2 and freeze. "
                "The original R1 findings and quality result remain unchanged."
            ),
        )
        _emit(payload, json_output=json_output)
    except LoopReviewServiceError as exc:
        _emit(exc.payload(), json_output=json_output)
        raise typer.Exit(1) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _emit(
            {
                "status": "blocked",
                "reason": "repair-readiness-result-invalid",
                "detail": str(exc),
            },
            json_output=json_output,
        )
        raise typer.Exit(1) from exc
