"""Tests for the minimal, read-only dynamic expert review kernel."""

from __future__ import annotations

import os
import tracemalloc
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_sdlc.core.review_kernel import (
    ReviewExecution,
    ReviewFinding,
    ReviewInput,
    build_review_input,
    merge_expert_findings,
    select_expert_roles,
)


def _finding(*, role: str = "API compatibility reviewer") -> ReviewFinding:
    return ReviewFinding(
        severity="important",
        role=role,
        location="spec.md:42",
        summary="The response contract removes a required field.",
        recommendation="Restore the field or update the accepted contract.",
    )


def test_review_models_expose_only_ephemeral_review_values() -> None:
    review_input = ReviewInput(
        loop_id="loop-1",
        loop_type="requirement",
        round_number=1,
        input_digest="a" * 64,
        artifact_paths=[".ai-sdlc/loops/requirement/loop-1/requirement-brief.md"],
        upstream_context_paths=[],
        risk_signals=["public-api"],
        expert_roles=[
            "product-value-and-acceptance",
            "api-compatibility",
        ],
        expert_reasons={
            "product-value-and-acceptance": "Primary expert for requirement.",
            "api-compatibility": "Cross-risk expert for public-api.",
        },
        role_brief="Choose one primary expert and at most one cross-risk expert.",
    )
    execution = ReviewExecution(
        status="completed",
        roles=["API compatibility reviewer"],
        role_reasons={
            "API compatibility reviewer": "The result changes a public schema."
        },
        findings=[],
    )

    assert review_input.input_digest == "a" * 64
    assert execution.status == "completed"
    assert "verdict" not in execution.model_dump()
    assert "passed" not in execution.model_dump()
    assert "closed" not in execution.model_dump()


@pytest.mark.parametrize(
    ("loop_type", "expected"),
    [
        ("requirement", "product-value-and-acceptance"),
        ("design-contract", "architecture-and-maintainability"),
        ("implementation", "correctness-and-regression"),
        ("frontend-evidence", "ux-accessibility-and-evidence"),
        ("local-pr-review", "cross-stage-delivery"),
    ],
)
def test_select_expert_roles_uses_exact_primary_for_each_loop(
    loop_type: str,
    expected: str,
) -> None:
    roles, reasons = select_expert_roles(loop_type, [])

    assert roles == [expected]
    assert set(reasons) == {expected}
    assert reasons[expected]


def test_select_expert_roles_adds_only_highest_priority_cross_risk() -> None:
    roles, reasons = select_expert_roles(
        "implementation",
        ["frontend", "public-api", "security", "data-integrity"],
    )

    assert roles == ["correctness-and-regression", "security-and-permissions"]
    assert set(reasons) == set(roles)


def test_select_expert_roles_never_duplicates_primary_or_adds_general_role() -> None:
    roles, reasons = select_expert_roles(
        "implementation",
        ["general-correctness", "correctness"],
    )

    assert roles == ["correctness-and-regression"]
    assert set(reasons) == set(roles)


def test_build_review_input_binds_selected_experts(tmp_path: Path) -> None:
    artifact = tmp_path / "implementation-report.md"
    artifact.write_text("Security-sensitive implementation.\n", encoding="utf-8")

    review_input = build_review_input(
        tmp_path,
        loop_id="implementation-1",
        loop_type="implementation",
        round_number=1,
        artifact_paths=[artifact],
        upstream_context_paths=[],
        risk_signals=["security"],
    )

    assert review_input.expert_roles == [
        "correctness-and-regression",
        "security-and-permissions",
    ]
    assert set(review_input.expert_reasons) == set(review_input.expert_roles)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "completed",
            "roles": ["one", "two", "three"],
            "role_reasons": {"one": "a", "two": "b", "three": "c"},
        },
        {
            "status": "completed",
            "roles": ["one", "one"],
            "role_reasons": {"one": "a"},
        },
        {"status": "completed", "roles": [], "role_reasons": {}},
        {
            "status": "completed",
            "roles": ["one"],
            "role_reasons": {"one": "a"},
            "findings": [_finding(role="absent")],
        },
        {
            "status": "failed",
            "roles": ["one"],
            "role_reasons": {"one": "a"},
        },
    ],
)
def test_review_execution_rejects_invalid_ephemeral_state(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ReviewExecution.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "input_digest",
        "verdict",
        "passed",
        "closed",
        "certificate",
        "session_id",
        "quorum",
        "score",
    ],
)
def test_review_execution_rejects_authority_and_persistence_fields(field: str) -> None:
    payload: dict[str, object] = {
        "status": "completed",
        "roles": ["one"],
        "role_reasons": {"one": "reason"},
        field: "forbidden",
    }

    with pytest.raises(ValidationError):
        ReviewExecution.model_validate(payload)


def test_build_review_input_is_stable_read_only_and_detects_drift(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "spec.md"
    context = tmp_path / "acceptance.md"
    artifact.write_text("contract v1\n", encoding="utf-8")
    context.write_text("acceptance v1\n", encoding="utf-8")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    first = build_review_input(
        tmp_path,
        loop_id="loop-1",
        loop_type="design-contract",
        round_number=2,
        artifact_paths=[artifact],
        upstream_context_paths=[context],
        risk_signals=["public-api"],
    )
    second = build_review_input(
        tmp_path,
        loop_id="loop-1",
        loop_type="design-contract",
        round_number=2,
        artifact_paths=[artifact],
        upstream_context_paths=[context],
        risk_signals=["public-api"],
    )

    assert first == second
    assert first.artifact_paths == ["spec.md"]
    assert first.upstream_context_paths == ["acceptance.md"]
    assert len(first.input_digest) == 64
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before

    artifact.write_text("contract v2\n", encoding="utf-8")
    changed = build_review_input(
        tmp_path,
        loop_id="loop-1",
        loop_type="design-contract",
        round_number=2,
        artifact_paths=[artifact],
        upstream_context_paths=[context],
        risk_signals=["public-api"],
    )
    assert changed.input_digest != first.input_digest


def test_build_review_input_streams_large_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "trace.bin"
    chunk = b"x" * (1024 * 1024)
    artifact_size = 12 * len(chunk)
    with artifact.open("wb") as handle:
        for _ in range(12):
            handle.write(chunk)

    tracemalloc.start()
    try:
        build_review_input(
            tmp_path,
            loop_id="loop-large",
            loop_type="frontend-evidence",
            round_number=1,
            artifact_paths=[artifact],
            upstream_context_paths=[],
            risk_signals=[],
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < artifact_size


def test_build_review_input_captures_identity_without_digesting_mutable_state(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "result.md"
    mutable_state = tmp_path / "loop-run.json"
    artifact.write_text("reviewed result\n", encoding="utf-8")
    mutable_state.write_bytes(b'{"status":"running"}\n')
    captured: dict[str, bytes] = {}

    first = build_review_input(
        tmp_path,
        loop_id="loop-identity-capture",
        loop_type="requirement",
        round_number=1,
        artifact_paths=[artifact],
        upstream_context_paths=[],
        risk_signals=[],
        capture_only_paths=[mutable_state],
        captured_artifacts=captured,
    )
    mutable_state.write_bytes(b'{"status":"passed"}\n')
    second = build_review_input(
        tmp_path,
        loop_id="loop-identity-capture",
        loop_type="requirement",
        round_number=1,
        artifact_paths=[artifact],
        upstream_context_paths=[],
        risk_signals=[],
    )

    assert captured["loop-run.json"] == b'{"status":"running"}\n'
    assert "loop-run.json" not in first.artifact_paths
    assert first.input_digest == second.input_digest


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable modes are not available")
def test_build_review_input_digest_binds_posix_file_mode(tmp_path: Path) -> None:
    artifact = tmp_path / "hook.sh"
    artifact.write_bytes(b"#!/bin/sh\nexit 0\n")
    artifact.chmod(0o644)

    before = build_review_input(
        tmp_path,
        loop_id="loop-1",
        loop_type="implementation",
        round_number=1,
        artifact_paths=[artifact],
        upstream_context_paths=[],
        risk_signals=[],
    )
    artifact.chmod(0o755)
    after = build_review_input(
        tmp_path,
        loop_id="loop-1",
        loop_type="implementation",
        round_number=1,
        artifact_paths=[artifact],
        upstream_context_paths=[],
        risk_signals=[],
    )

    assert after.input_digest != before.input_digest


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable modes are not available")
def test_build_review_input_rejects_posix_mode_changes_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "hook.sh"
    artifact.write_bytes(b"#!/bin/sh\nexit 0\n")
    artifact.chmod(0o644)
    original_read = os.read
    changed = False

    def chmod_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, size)
        if content and not changed:
            artifact.chmod(0o755)
            changed = True
        return content

    monkeypatch.setattr("ai_sdlc.core.review_kernel.os.read", chmod_after_first_read)

    with pytest.raises(ValueError, match="review path changed while reading"):
        build_review_input(
            tmp_path,
            loop_id="loop-1",
            loop_type="implementation",
            round_number=1,
            artifact_paths=[artifact],
            upstream_context_paths=[],
            risk_signals=[],
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable")
def test_build_review_input_rejects_repository_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target content\n", encoding="utf-8")
    artifact = tmp_path / "artifact.txt"
    artifact.symlink_to(target.name)

    with pytest.raises(ValueError, match="review path is not a regular file"):
        build_review_input(
            tmp_path,
            loop_id="loop-1",
            loop_type="implementation",
            round_number=1,
            artifact_paths=[artifact],
            upstream_context_paths=[],
            risk_signals=[],
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable")
def test_build_review_input_rejects_symlink_replacement_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("replacement content\n", encoding="utf-8")
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("reviewed content\n", encoding="utf-8")
    original_resolve = Path.resolve
    replaced = False

    def replace_before_resolve(path: Path, strict: bool = False) -> Path:
        nonlocal replaced
        if not replaced and path == artifact:
            artifact.unlink()
            artifact.symlink_to(target.name)
            replaced = True
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", replace_before_resolve)

    with pytest.raises(ValueError, match="not a regular file|changed while opening"):
        build_review_input(
            tmp_path,
            loop_id="loop-1",
            loop_type="implementation",
            round_number=1,
            artifact_paths=[artifact],
            upstream_context_paths=[],
            risk_signals=[],
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable")
def test_build_review_input_rejects_ancestor_symlink_retarget_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    artifact = scoped / "spec.md"
    artifact.write_text("same reviewed bytes\n", encoding="utf-8")
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    os.link(artifact, alternate / artifact.name)
    saved = tmp_path / "scoped-original"
    original_open = os.open
    swapped = False

    def retarget_ancestor_before_file_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path).name == artifact.name:
            scoped.rename(saved)
            scoped.symlink_to(alternate.name, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return original_open(path, flags)
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(
        "ai_sdlc.core.review_kernel.os.open",
        retarget_ancestor_before_file_open,
    )

    with pytest.raises(ValueError, match="not a regular file|changed while reading"):
        build_review_input(
            tmp_path,
            loop_id="loop-1",
            loop_type="implementation",
            round_number=1,
            artifact_paths=[artifact],
            upstream_context_paths=[],
            risk_signals=[],
        )


def test_build_review_input_reads_windows_files_in_binary_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "spec.md"
    artifact.write_bytes(b"contract v1\r\n")
    binary_flag = 0x8000
    original_open = os.open

    def windows_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if not flags & binary_flag:
            raise AssertionError("Windows low-level reads must request binary mode")
        forwarded_flags = flags if os.name == "nt" else flags & ~binary_flag
        if dir_fd is None:
            return original_open(path, forwarded_flags)
        return original_open(path, forwarded_flags, dir_fd=dir_fd)

    monkeypatch.setattr(
        "ai_sdlc.core.review_kernel.os.O_BINARY", binary_flag, raising=False
    )
    monkeypatch.setattr("ai_sdlc.core.review_kernel.os.open", windows_open)

    review_input = build_review_input(
        tmp_path,
        loop_id="loop-1",
        loop_type="design-contract",
        round_number=1,
        artifact_paths=[artifact],
        upstream_context_paths=[],
        risk_signals=[],
    )

    assert review_input.artifact_paths == ["spec.md"]


@pytest.mark.parametrize("unsafe", ["missing.md", "../outside.md"])
def test_build_review_input_rejects_missing_or_escaping_path(
    tmp_path: Path,
    unsafe: str,
) -> None:
    with pytest.raises(ValueError):
        build_review_input(
            tmp_path,
            loop_id="loop-1",
            loop_type="implementation",
            round_number=1,
            artifact_paths=[unsafe],
            upstream_context_paths=[],
            risk_signals=[],
        )


def test_merge_expert_findings_deduplicates_without_deciding_close() -> None:
    finding = _finding()
    reworded = finding.model_copy(
        update={"recommendation": "Keep the existing response field."}
    )
    merged = merge_expert_findings(
        [
            ReviewExecution(
                status="completed",
                roles=["API compatibility reviewer"],
                role_reasons={"API compatibility reviewer": "public schema"},
                findings=[reworded],
            ),
            ReviewExecution(
                status="completed",
                roles=["API compatibility reviewer"],
                role_reasons={"API compatibility reviewer": "public schema"},
                findings=[finding],
            ),
        ]
    )

    assert merged.status == "completed"
    assert merged.findings == [reworded]
    assert set(merged.model_dump()) == {
        "status",
        "roles",
        "role_reasons",
        "findings",
        "failure_kind",
        "failure_reason",
    }


def test_merge_expert_findings_deduplicates_across_severities() -> None:
    important = _finding()
    advisory = important.model_copy(update={"severity": "advisory"})
    merged = merge_expert_findings(
        [
            ReviewExecution(
                status="completed",
                roles=["API compatibility reviewer"],
                role_reasons={"API compatibility reviewer": "public schema"},
                findings=[advisory],
            ),
            ReviewExecution(
                status="completed",
                roles=["API compatibility reviewer"],
                role_reasons={"API compatibility reviewer": "public schema"},
                findings=[important],
            ),
        ]
    )

    assert len(merged.findings) == 1
    assert merged.findings[0].severity == "important"
