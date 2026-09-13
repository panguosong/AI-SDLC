"""Pure, read-only inputs and values for bounded dynamic expert review."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LoopReviewType = Literal[
    "requirement",
    "design-contract",
    "implementation",
    "frontend-evidence",
    "local-pr-review",
]
ReviewSeverity = Literal["blocker", "important", "advisory"]
ReviewExecutionStatus = Literal["completed", "failed"]
ReviewInputValidator = Callable[..., object]

_ROLE_BRIEF = "Choose one primary expert and at most one cross-risk expert."
_PRIMARY_EXPERTS: dict[LoopReviewType, str] = {
    "requirement": "product-value-and-acceptance",
    "design-contract": "architecture-and-maintainability",
    "implementation": "correctness-and-regression",
    "frontend-evidence": "ux-accessibility-and-evidence",
    "local-pr-review": "cross-stage-delivery",
}
_CROSS_RISK_EXPERTS: tuple[tuple[str, str], ...] = (
    ("security", "security-and-permissions"),
    ("data-integrity", "data-integrity-and-migration"),
    ("concurrency", "concurrency-and-recovery"),
    ("public-api", "api-compatibility"),
    ("frontend", "frontend-integration"),
)
_SEVERITY_RANK: dict[ReviewSeverity, int] = {
    "advisory": 0,
    "important": 1,
    "blocker": 2,
}


class ReviewInput(BaseModel):
    """One immutable view of the substantive result to inspect."""

    model_config = ConfigDict(extra="forbid")

    loop_id: str
    loop_type: LoopReviewType
    round_number: int = Field(ge=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_paths: list[str] = Field(min_length=1)
    upstream_context_paths: list[str] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
    expert_roles: list[str] = Field(min_length=1, max_length=2)
    expert_reasons: dict[str, str]
    role_brief: str = _ROLE_BRIEF

    @field_validator("loop_id", "role_brief")
    @classmethod
    def _require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("review input text is required")
        return text

    @field_validator("artifact_paths", "upstream_context_paths", "risk_signals")
    @classmethod
    def _require_unique_values(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("review input values cannot be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("review input values must be unique")
        return normalized

    @model_validator(mode="after")
    def _paths_cannot_overlap(self) -> ReviewInput:
        if set(self.artifact_paths) & set(self.upstream_context_paths):
            raise ValueError("artifact and upstream paths cannot overlap")
        normalized_roles = [role.strip() for role in self.expert_roles]
        if any(not role for role in normalized_roles):
            raise ValueError("expert role cannot be empty")
        if len(normalized_roles) != len(set(normalized_roles)):
            raise ValueError("expert roles must be unique")
        normalized_reasons = {
            role.strip(): reason.strip() for role, reason in self.expert_reasons.items()
        }
        if set(normalized_reasons) != set(normalized_roles):
            raise ValueError("every selected expert requires exactly one reason")
        if any(not reason for reason in normalized_reasons.values()):
            raise ValueError("expert reason cannot be empty")
        self.expert_roles = normalized_roles
        self.expert_reasons = normalized_reasons
        return self


class ReviewFinding(BaseModel):
    """One actionable observation from an ephemeral expert."""

    model_config = ConfigDict(extra="forbid")

    severity: ReviewSeverity
    role: str
    location: str
    summary: str
    recommendation: str

    @field_validator("role", "location", "summary", "recommendation")
    @classmethod
    def _require_finding_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("review finding text is required")
        return text


class ReviewExecution(BaseModel):
    """In-memory execution status and findings; never a close credential."""

    model_config = ConfigDict(extra="forbid")

    status: ReviewExecutionStatus
    roles: list[str] = Field(default_factory=list, max_length=2)
    role_reasons: dict[str, str] = Field(default_factory=dict)
    findings: list[ReviewFinding] = Field(default_factory=list)
    failure_kind: str = ""
    failure_reason: str = ""

    @model_validator(mode="after")
    def _execution_shape_matches_status(self) -> ReviewExecution:
        normalized_roles = [role.strip() for role in self.roles]
        if any(not role for role in normalized_roles):
            raise ValueError("expert role cannot be empty")
        if len(normalized_roles) != len(set(normalized_roles)):
            raise ValueError("expert roles must be unique")
        self.roles = normalized_roles

        normalized_reasons = {
            role.strip(): reason.strip() for role, reason in self.role_reasons.items()
        }
        if set(normalized_reasons) != set(normalized_roles):
            raise ValueError("every expert role requires exactly one reason")
        if any(not reason for reason in normalized_reasons.values()):
            raise ValueError("expert role reason cannot be empty")
        self.role_reasons = normalized_reasons

        absent_roles = {finding.role for finding in self.findings} - set(
            normalized_roles
        )
        if absent_roles:
            raise ValueError("finding role must be present in the execution")

        if self.status == "completed":
            if not normalized_roles:
                raise ValueError("completed review requires at least one expert")
            if self.failure_kind.strip() or self.failure_reason.strip():
                raise ValueError("completed review cannot carry failure state")
        else:
            if not self.failure_kind.strip() or not self.failure_reason.strip():
                raise ValueError("failed review requires failure kind and reason")
            if self.findings:
                raise ValueError("failed review cannot be treated as findings")
        return self


def build_review_input(
    root: Path,
    *,
    loop_id: str,
    loop_type: LoopReviewType,
    round_number: int,
    artifact_paths: Sequence[str | Path],
    upstream_context_paths: Sequence[str | Path],
    risk_signals: Sequence[str],
    capture_artifact_paths: Sequence[str | Path] = (),
    capture_only_paths: Sequence[str | Path] = (),
    captured_artifacts: MutableMapping[str, bytes] | None = None,
) -> ReviewInput:
    """Read stable regular files and bind their raw bytes to one review input."""

    resolved_root = root.resolve(strict=True)
    capture_paths = {
        _review_relative_path(resolved_root, path)
        for path in (*capture_artifact_paths, *capture_only_paths)
    }
    if capture_paths and captured_artifacts is None:
        raise ValueError("captured_artifacts is required when capture paths are set")
    artifacts = _read_paths(
        resolved_root,
        artifact_paths,
        capture_paths=capture_paths,
        captured_artifacts=captured_artifacts,
    )
    upstream = _read_paths(
        resolved_root,
        upstream_context_paths,
        capture_paths=capture_paths,
        captured_artifacts=captured_artifacts,
    )
    _read_paths(
        resolved_root,
        capture_only_paths,
        capture_paths=capture_paths,
        captured_artifacts=captured_artifacts,
    )
    if captured_artifacts is not None:
        missing_captures = capture_paths - captured_artifacts.keys()
        if missing_captures:
            missing = ", ".join(sorted(missing_captures))
            raise ValueError(f"review capture path is not an input artifact: {missing}")
    all_paths = [path for path, _, _, _ in (*artifacts, *upstream)]
    if len(all_paths) != len(set(all_paths)):
        raise ValueError("review paths must be unique")

    normalized_signals = [signal.strip() for signal in risk_signals]
    if any(not signal for signal in normalized_signals):
        raise ValueError("risk signal cannot be empty")
    if len(normalized_signals) != len(set(normalized_signals)):
        raise ValueError("risk signals must be unique")
    expert_roles, expert_reasons = select_expert_roles(loop_type, normalized_signals)

    digest_payload = {
        "loop_id": loop_id.strip(),
        "loop_type": loop_type,
        "round_number": round_number,
        "artifacts": [
            _digest_record(path, mode, size, digest)
            for path, mode, size, digest in artifacts
        ],
        "upstream": [
            _digest_record(path, mode, size, digest)
            for path, mode, size, digest in upstream
        ],
        "risk_signals": normalized_signals,
        "expert_roles": expert_roles,
        "expert_reasons": expert_reasons,
    }
    encoded = json.dumps(
        digest_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ReviewInput(
        loop_id=loop_id,
        loop_type=loop_type,
        round_number=round_number,
        input_digest=hashlib.sha256(encoded).hexdigest(),
        artifact_paths=[path for path, _, _, _ in artifacts],
        upstream_context_paths=[path for path, _, _, _ in upstream],
        risk_signals=normalized_signals,
        expert_roles=expert_roles,
        expert_reasons=expert_reasons,
    )


def select_expert_roles(
    loop_type: LoopReviewType,
    risk_signals: Sequence[str],
) -> tuple[list[str], dict[str, str]]:
    """Select one primary and at most one deterministic cross-risk expert."""

    primary = _PRIMARY_EXPERTS[loop_type]
    roles = [primary]
    reasons = {primary: f"Primary expert for {loop_type}."}
    normalized_signals = {signal.strip() for signal in risk_signals}
    for signal, expert in _CROSS_RISK_EXPERTS:
        if signal not in normalized_signals or expert == primary:
            continue
        roles.append(expert)
        reasons[expert] = f"Cross-risk expert for {signal}."
        break
    return roles, reasons


def merge_expert_findings(executions: Sequence[ReviewExecution]) -> ReviewExecution:
    """Combine completed expert outputs without interpreting them as a verdict."""

    if not executions:
        raise ValueError("at least one expert execution is required")
    failed = [execution for execution in executions if execution.status == "failed"]
    if failed:
        roles, reasons = _merge_roles(executions)
        return ReviewExecution(
            status="failed",
            roles=roles,
            role_reasons=reasons,
            failure_kind="expert-execution-failed",
            failure_reason="; ".join(
                f"{item.failure_kind}: {item.failure_reason}" for item in failed
            ),
        )

    roles, reasons = _merge_roles(executions)
    findings: list[ReviewFinding] = []
    seen: dict[tuple[str, str, str], int] = {}
    for execution in executions:
        for finding in execution.findings:
            identity = (
                finding.role,
                finding.location,
                finding.summary,
            )
            existing_index = seen.get(identity)
            if existing_index is None:
                seen[identity] = len(findings)
                findings.append(finding)
                continue
            existing = findings[existing_index]
            if _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[existing.severity]:
                findings[existing_index] = existing.model_copy(
                    update={"severity": finding.severity}
                )
    return ReviewExecution(
        status="completed",
        roles=roles,
        role_reasons=reasons,
        findings=findings,
    )


def _read_paths(
    root: Path,
    paths: Sequence[str | Path],
    *,
    capture_paths: set[str],
    captured_artifacts: MutableMapping[str, bytes] | None,
) -> list[tuple[str, int, int, str]]:
    records: list[tuple[str, int, int, str]] = []
    for raw_path in paths:
        candidate = Path(raw_path)
        unresolved = candidate if candidate.is_absolute() else root / candidate
        path = Path(os.path.abspath(unresolved))
        try:
            relative_path = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"review path escapes project: {raw_path}") from exc
        relative = relative_path.as_posix()
        original = _validate_review_path(root, relative_path, raw_path=raw_path)
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"review path is missing: {raw_path}") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"review path escapes project: {raw_path}") from exc

        open_flags = (
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = _open_review_descriptor(root, relative_path, open_flags)
        except OSError as exc:
            _validate_review_path(root, relative_path, raw_path=relative)
            raise ValueError(f"review path changed while opening: {relative}") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"review path is not a regular file: {relative}")
            digest = hashlib.sha256()
            captured_chunks: list[bytes] | None = (
                [] if relative in capture_paths else None
            )
            content_size = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                if captured_chunks is not None:
                    captured_chunks.append(chunk)
                content_size += len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        closed = _validate_review_path(root, relative_path, raw_path=relative)
        if not _file_snapshot_is_stable(
            original,
            opened,
            after,
            closed,
            content_size=content_size,
        ):
            raise ValueError(f"review path changed while reading: {relative}")
        records.append(
            (relative, stat.S_IMODE(opened.st_mode), content_size, digest.hexdigest())
        )
        if captured_chunks is not None and captured_artifacts is not None:
            captured_artifacts[relative] = b"".join(captured_chunks)
    records.sort(key=lambda item: item[0])
    return records


def _review_relative_path(root: Path, raw_path: str | Path) -> str:
    candidate = Path(raw_path)
    unresolved = candidate if candidate.is_absolute() else root / candidate
    path = Path(os.path.abspath(unresolved))
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"review path escapes project: {raw_path}") from exc


def _validate_review_path(
    root: Path,
    relative: Path,
    *,
    raw_path: str | Path,
) -> os.stat_result:
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"review path is missing: {raw_path}") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            raise ValueError(f"review path is not a regular file: {raw_path}")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"review path is not a regular file: {raw_path}")
            return metadata
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"review path is not a regular file: {raw_path}")
    raise ValueError(f"review path is not a regular file: {raw_path}")


def _open_review_descriptor(root: Path, relative: Path, file_flags: int) -> int:
    if (
        os.name == "nt"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        return os.open(root / relative, file_flags)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _file_snapshot_is_stable(
    before: os.stat_result,
    opened: os.stat_result,
    after: os.stat_result,
    closed: os.stat_result,
    *,
    content_size: int,
) -> bool:
    def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            stat.S_IMODE(item.st_mode),
        )

    identities = {identity(item) for item in (before, opened, after, closed)}
    return len(identities) == 1 and content_size == opened.st_size


def _digest_record(
    path: str,
    mode: int,
    size: int,
    digest: str,
) -> dict[str, object]:
    return {
        "path": path,
        "mode": mode,
        "size": size,
        "sha256": digest,
    }


def _merge_roles(
    executions: Sequence[ReviewExecution],
) -> tuple[list[str], dict[str, str]]:
    roles: list[str] = []
    reasons: dict[str, str] = {}
    for execution in executions:
        for role in execution.roles:
            if role not in roles:
                roles.append(role)
                reasons[role] = execution.role_reasons[role]
    if len(roles) > 2:
        raise ValueError("merged review cannot contain more than two roles")
    return roles, reasons


def revalidate_review_input_at_transition(
    root: Path,
    *,
    loop_type: LoopReviewType,
    loop_id: str,
    expected_digest: str,
    validator: ReviewInputValidator | None,
) -> None:
    """Recheck the reviewed input immediately before a close transition."""

    digest = expected_digest.strip()
    if not digest:
        return
    if validator is None:
        raise ValueError("A review input validator is required for a guarded close.")
    validator(
        root,
        loop_type=loop_type,
        loop_id=loop_id,
        expected_digest=digest,
    )


__all__ = [
    "ReviewExecution",
    "ReviewFinding",
    "ReviewInput",
    "ReviewInputValidator",
    "build_review_input",
    "merge_expert_findings",
    "revalidate_review_input_at_transition",
    "select_expert_roles",
]
