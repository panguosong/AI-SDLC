"""Local adversarial PR review data models."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_sdlc.core.loop_models import (
    LoopArtifactModel,
    LoopStatus,
    LoopType,
    utc_now_iso,
)
from ai_sdlc.core.quality_command import (
    QualityCommandResult,
    controlled_raw_original,
    validate_controlled_receipts,
)


class ReviewVerdict(StrEnum):
    """Reviewer or close verdict values."""

    CLEAN = "clean"
    CHANGES_REQUIRED = "changes_required"
    BLOCKED = "blocked"
    FULLY_CLEAN = "fully_clean"
    RISK_ACCEPTED = "risk_accepted"


class FindingSeverity(StrEnum):
    """Stable review finding severity values."""

    BLOCKER = "BLOCKER"
    REQUIRED = "REQUIRED"
    ADVISORY = "ADVISORY"


class FindingResolutionStatus(StrEnum):
    """Stable finding resolution values."""

    UNRESOLVED = "unresolved"
    FIXED = "fixed"
    WAIVED = "waived"
    NOT_APPLICABLE = "not_applicable"


class ProviderIsolationStatus(StrEnum):
    """How strongly the reviewer runner is isolated from implementation context."""

    ISOLATED_PROCESS = "isolated_process"
    ISOLATED_SESSION = "isolated_session"
    NOT_PROVEN = "not_proven"


class ProviderLaunchStatus(StrEnum):
    """区分未启动证明、已启动调用及不含启动证明的旧记录。"""

    UNKNOWN = "unknown"
    NEVER_STARTED = "never_started"
    STARTED = "started"


class ProviderMode(StrEnum):
    """Where provider execution is initiated."""

    LOCAL_AGENT = "local_agent"
    MOCK = "mock"
    CUSTOM_LOCAL_COMMAND = "custom_local_command"


class ModelResolutionStatus(StrEnum):
    """Whether a model selector was resolved to an executable model."""

    RESOLVED = "resolved"
    NEEDS_USER = "needs_user"
    BLOCKED = "blocked"


class ModelResolutionSource(StrEnum):
    """Source used to resolve a model selector."""

    EXPLICIT_CLI = "explicit_cli"
    PROJECT_POLICY = "project_policy"
    PROVIDER_CONFIG = "provider_config"
    CURRENT_AGENT = "current_agent"
    MOCK_FIXTURE = "mock_fixture"


class DiffSourceKind(StrEnum):
    """Supported review input source kinds."""

    LOCAL_GIT_RANGE = "local-git-range"
    LOCAL_STAGED = "local-staged"
    LOCAL_UNSTAGED = "local-unstaged"
    PATCH = "patch"
    SCM_PR = "scm-pr"
    CUSTOM = "custom"


class SourceAccessStatus(StrEnum):
    """Whether a review source could be resolved safely."""

    RESOLVED = "resolved"
    NEEDS_USER = "needs_user"
    BLOCKED = "blocked"


class DiffSourceDescriptor(BaseModel):
    """Embedded descriptor for the exact review input source."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    source_kind: DiffSourceKind = DiffSourceKind.LOCAL_GIT_RANGE
    adapter_id: str = "local-git-range"
    source_id: str = ""
    repo_root: str = ""
    base_ref: str = ""
    head_ref: str = ""
    base_commit: str = ""
    head_commit: str = ""
    staged_tree_oid: str = ""
    patch_file: str = ""
    patch_hash: str = ""
    scm_host_type: str = ""
    access_status: SourceAccessStatus = SourceAccessStatus.RESOLVED
    source_metadata: dict[str, str | bool | int | float] = Field(default_factory=dict)

    @field_validator("adapter_id")
    @classmethod
    def _require_adapter_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source adapter_id is required")
        return value


class SourceAdapterResolution(LoopArtifactModel):
    """Persisted source adapter resolution artifact."""

    artifact_kind: str = "source-resolution"
    source_kind: DiffSourceKind = DiffSourceKind.LOCAL_GIT_RANGE
    adapter_id: str = "local-git-range"
    source_id: str = ""
    repo_root: str = ""
    base_ref: str = ""
    head_ref: str = ""
    base_commit: str = ""
    head_commit: str = ""
    staged_tree_oid: str = ""
    patch_file: str = ""
    patch_hash: str = ""
    scm_host_type: str = ""
    access_status: SourceAccessStatus = SourceAccessStatus.NEEDS_USER
    requires_user_choice: bool = False
    unavailable_reason: str = ""
    blocker: str = ""
    next_command: str = ""
    source_metadata: dict[str, str | bool | int | float] = Field(default_factory=dict)

    @field_validator("adapter_id")
    @classmethod
    def _require_source_resolution_adapter(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source adapter_id is required")
        return value

    @model_validator(mode="after")
    def _source_resolution_status_requires_context(self) -> SourceAdapterResolution:
        if self.access_status == SourceAccessStatus.RESOLVED:
            if not self.repo_root.strip():
                raise ValueError("resolved source requires repo_root")
            if self.source_kind == DiffSourceKind.LOCAL_GIT_RANGE and (
                not self.base_ref.strip()
                or not self.head_ref.strip()
                or not self.base_commit.strip()
                or not self.head_commit.strip()
            ):
                raise ValueError(
                    "resolved local-git-range source requires refs and commits"
                )
            if self.source_kind == DiffSourceKind.LOCAL_STAGED and not re.fullmatch(
                r"[0-9a-f]{40,64}", self.staged_tree_oid
            ):
                raise ValueError(
                    "resolved local-staged source requires staged_tree_oid"
                )
        elif not (self.blocker.strip() or self.unavailable_reason.strip()):
            raise ValueError("unresolved source requires blocker or unavailable_reason")
        return self

    def to_descriptor(self) -> DiffSourceDescriptor:
        """Return the embeddable source descriptor for review-pack.json."""

        return DiffSourceDescriptor(
            source_kind=self.source_kind,
            adapter_id=self.adapter_id,
            source_id=self.source_id,
            repo_root=self.repo_root,
            base_ref=self.base_ref,
            head_ref=self.head_ref,
            base_commit=self.base_commit,
            head_commit=self.head_commit,
            staged_tree_oid=self.staged_tree_oid,
            patch_file=self.patch_file,
            patch_hash=self.patch_hash,
            scm_host_type=self.scm_host_type,
            access_status=self.access_status,
            source_metadata=dict(self.source_metadata),
        )


class ModelResolution(LoopArtifactModel):
    """Resolved model contract for a local review provider run."""

    artifact_kind: str = "model-resolution"
    provider_id: str
    provider_mode: ProviderMode = ProviderMode.LOCAL_AGENT
    model_selector: str = "current"
    resolved_model: str = ""
    resolution_source: ModelResolutionSource | None = None
    status: ModelResolutionStatus = ModelResolutionStatus.NEEDS_USER
    code_egress: bool = False
    unavailable_reason: str = ""
    blocker: str = ""

    @field_validator("provider_id", "model_selector")
    @classmethod
    def _require_model_resolution_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model resolution field is required")
        return value

    @model_validator(mode="after")
    def _resolved_models_require_resolution_details(self) -> ModelResolution:
        if self.status == ModelResolutionStatus.RESOLVED:
            if not self.resolved_model.strip():
                raise ValueError("resolved model resolution requires resolved_model")
            if self.resolution_source is None:
                raise ValueError("resolved model resolution requires resolution_source")
        if self.status != ModelResolutionStatus.RESOLVED:
            if not self.blocker.strip():
                raise ValueError("unresolved model resolution requires blocker")
            if not self.unavailable_reason.strip():
                self.unavailable_reason = self.blocker
        return self


class RepairScopeDependency(BaseModel):
    """本次修复新增的精确普通文件，不接受目录或路径模式。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _canonical_file(cls, value: str) -> str:
        if (
            any(part in {"", ".", ".."} for part in value.split("/"))
            or any(char in value for char in "\\:*?[]")
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("repair scope requires a canonical relative file")
        return value

    @field_validator("finding_id", "reason")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("repair scope text is required")
        return value






class RepairScopeInput(BaseModel):
    """对原 REQUIRED 修复依赖的精确确认；不证明操作者身份或语义正确性。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1"] = "1"
    artifact_kind: Literal["pr-repair-scope-input"] = "pr-repair-scope-input"
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    review_id: str = Field(min_length=1)
    loop_id: str = Field(min_length=1)
    head_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    review_pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_round: int = Field(ge=1)
    staged_tree_oid: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    dependencies: list[RepairScopeDependency] = Field(default_factory=list)
    workspace_adoption: dict[str, object] | None = None

    @model_validator(mode="after")
    def _unique_paths(self) -> RepairScopeInput:
        if self.workspace_adoption is not None:
            raise ValueError("workspace adoption is unsupported")
        if not self.dependencies:
            raise ValueError("repair dependencies are required")
        paths = [item.path for item in self.dependencies]
        if len(set(paths)) != len(paths):
            raise ValueError("repair scope dependency paths must be unique")
        return self


class FindingResolution(LoopArtifactModel):
    """Resolution record for one review finding."""

    artifact_kind: str = "finding-resolution"
    finding_id: str
    status: FindingResolutionStatus = FindingResolutionStatus.UNRESOLVED
    reason: str = ""
    operator: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    resolved_at: str = ""

    @model_validator(mode="after")
    def _resolved_status_requires_audit_metadata(self) -> FindingResolution:
        if self.status == FindingResolutionStatus.FIXED and (
            not any(ref.strip() for ref in self.evidence_refs)
            or not self.operator.strip()
            or not self.resolved_at.strip()
        ):
            raise ValueError(
                "fixed findings require evidence_refs, operator, and resolved_at"
            )
        if self.status == FindingResolutionStatus.WAIVED and (
            not self.reason.strip()
            or not self.operator.strip()
            or not self.resolved_at.strip()
        ):
            raise ValueError(
                "waived findings require reason, operator, and resolved_at"
            )
        if self.status == FindingResolutionStatus.NOT_APPLICABLE and (
            not self.reason.strip()
            or not self.operator.strip()
            or not self.resolved_at.strip()
        ):
            raise ValueError(
                "not_applicable findings require reason, operator, and resolved_at"
            )
        return self


class ReviewFinding(LoopArtifactModel):
    """One structured adversarial review finding."""

    artifact_kind: str = "review-finding"
    id: str
    severity: FindingSeverity
    file: str
    line: int | None = Field(default=None, ge=1)
    claim: str
    evidence: str
    risk: str
    suggested_fix: str
    confidence: float = Field(ge=0.0, le=1.0)
    resolution: FindingResolutionStatus = FindingResolutionStatus.UNRESOLVED

    @field_validator("id", "file", "claim", "evidence", "risk", "suggested_fix")
    @classmethod
    def _require_non_empty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field is required")
        return value


class ReviewFindings(LoopArtifactModel):
    """Structured findings artifact emitted by a local review provider."""

    artifact_kind: str = "review-findings"
    review_id: str
    loop_id: str
    review_pack_path: str
    provider_id: str
    model_selector: str = "current"
    resolved_model: str
    verdict: ReviewVerdict = ReviewVerdict.CLEAN
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocker: str = ""

    @field_validator(
        "review_id",
        "loop_id",
        "review_pack_path",
        "provider_id",
        "model_selector",
        "resolved_model",
    )
    @classmethod
    def _require_findings_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review findings field is required")
        return value

    @model_validator(mode="after")
    def _verdict_must_match_findings(self) -> ReviewFindings:
        if self.verdict == ReviewVerdict.CHANGES_REQUIRED and not self.findings:
            raise ValueError("changes_required findings require at least one finding")
        if self.verdict == ReviewVerdict.BLOCKED and not self.blocker.strip():
            raise ValueError("blocked findings require blocker")
        if self.verdict == ReviewVerdict.CLEAN and any(
            finding.severity in {FindingSeverity.BLOCKER, FindingSeverity.REQUIRED}
            for finding in self.findings
        ):
            raise ValueError(
                "clean findings cannot include blocker or required findings"
            )
        finding_ids = [finding.id for finding in self.findings]
        duplicate_ids = sorted(
            finding_id
            for finding_id in set(finding_ids)
            if finding_ids.count(finding_id) > 1
        )
        if duplicate_ids:
            raise ValueError(
                "duplicate review finding ids: " + ", ".join(duplicate_ids)
            )
        return self


class ReviewPack(LoopArtifactModel):
    """Mechanically generated input package for a local review agent."""

    artifact_kind: str = "review-pack"
    review_id: str
    loop_id: str
    diff_source: DiffSourceDescriptor = Field(default_factory=DiffSourceDescriptor)
    source_adapter: str = "local-git-range"
    source_access_status: SourceAccessStatus = SourceAccessStatus.RESOLVED
    source_resolution_path: str = ""
    source_resolution_digest: str = ""
    repo_root: str
    base_ref: str
    head_ref: str
    base_commit: str
    head_commit: str
    staged_tree_oid: str = ""
    changed_files: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    diff_path: str = ""
    diff_digest: str = ""
    diff_coverage: dict[str, int | float | str] = Field(default_factory=dict)
    work_item_refs: list[str] = Field(default_factory=list)
    test_results_refs: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)
    policy_profile_id: str = "default"
    policy_decisions: dict[str, str | bool | int | float] = Field(default_factory=dict)
    model_selector: str = "current"
    resolved_model: str = ""
    model_resolution_status: ModelResolutionStatus = ModelResolutionStatus.NEEDS_USER
    model_resolution_source: ModelResolutionSource | None = None
    model_unavailable_reason: str = ""
    provider_mode: ProviderMode = ProviderMode.LOCAL_AGENT
    code_egress: bool = False
    redaction_report_path: str = ""
    reviewer_allowlist: list[str] = Field(default_factory=list)
    workspace_adoption_ref: dict[str, str] | None = Field(default=None, exclude_if=lambda value: value is None)
    rejected_feedback_ref: dict[str, str] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _require_commit_scope(self) -> ReviewPack:
        if self.rejected_feedback_ref is not None or self.workspace_adoption_ref is not None:
            raise ValueError("historical provider recovery references are unsupported")
        required = {
            "review_id": self.review_id,
            "loop_id": self.loop_id,
            "source_adapter": self.source_adapter,
            "repo_root": self.repo_root,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "model_selector": self.model_selector,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"missing review pack scope fields: {', '.join(missing)}")
        if self.model_resolution_status == ModelResolutionStatus.RESOLVED and (
            not self.resolved_model.strip() or self.model_resolution_source is None
        ):
            raise ValueError(
                "resolved review pack model state requires resolved_model and source"
            )
        if self.source_access_status != SourceAccessStatus.RESOLVED:
            raise ValueError("review pack requires a resolved diff source")
        if (
            self.diff_source.source_kind == DiffSourceKind.LOCAL_STAGED
            and self.staged_tree_oid != self.diff_source.staged_tree_oid
        ):
            raise ValueError("review pack staged tree does not match diff source")
        return self


class ProviderWorkspaceCheck(BaseModel):
    """原调用的工作区检查证据；只约束 reviewer 实际改过的边界。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot_mode: Literal["bounded-metadata-v1", "complete-metadata-v1"] = Field(
        default="bounded-metadata-v1", exclude_if=lambda value: value == "bounded-metadata-v1"
    )
    snapshot_max_entries: int = Field(default=4096, strict=True, exclude_if=lambda value: value == 4096)
    workspace_adoption_ref: dict[str, str] | None = Field(default=None, exclude_if=lambda value: value is None)
    status: Literal["unchanged", "mutated", "unproven"]
    review_pack_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_values: dict[str, str | None] = Field(default_factory=dict)
    host_artifact_mutations: list[str] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_workspace_check(self) -> ProviderWorkspaceCheck:
        if (self.snapshot_mode != "bounded-metadata-v1" or self.snapshot_max_entries != 4096
                or self.workspace_adoption_ref is not None):
            raise ValueError("workspace adoption and complete recovery snapshots are unsupported")
        for key in [*self.original_values, *self.host_artifact_mutations]:
            if key in {"<git:HEAD>", "<git:INDEX>"}:
                if key in self.host_artifact_mutations:
                    raise ValueError("host artifact must be a relative file path")
                continue
            if (
                not key
                or key.startswith("/")
                or "\\" in key
                or ":" in key
                or any(part in {"", ".", ".."} for part in key.rstrip("/").split("/"))
            ):
                raise ValueError("workspace boundary must be a canonical relative path")
        changed = bool(self.original_values or self.host_artifact_mutations)
        if self.status == "unchanged" and changed:
            raise ValueError("unchanged workspace cannot contain mutation boundaries")
        if self.status == "mutated" and not changed:
            raise ValueError("mutated workspace requires an original boundary")
        if self.status == "unproven":
            if not self.reason or not self.reason.strip():
                raise ValueError("unproven workspace requires a reason")
        elif self.reason is not None:
            raise ValueError("only unproven workspace may contain a reason")
        if len(set(self.host_artifact_mutations)) != len(self.host_artifact_mutations):
            raise ValueError("host artifact mutations must be unique")
        return self


class ProviderCompletionProof(BaseModel):
    """原调用的进程原件随 invocation 归档；不为旧缺证调用补写证明。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    ownership_nonce: str = Field(min_length=1)
    originals: dict[str, str]
    sha256: dict[str, str]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_completion_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("provider-completion-schema-version-invalid")
        return value

    def verified_receipts(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        names = {"process.json", "raw-result.json", "cleanup.json"}
        present = set(self.originals)
        if (
            "cleanup.json" not in present
            or not present <= names
            or set(self.sha256) != present
        ):
            raise ValueError("provider-completion-original-set-incomplete")
        documents = {}
        for name in sorted(present):
            content = self.originals[name].encode("utf-8")
            if hashlib.sha256(content).hexdigest() != self.sha256[name]:
                raise ValueError("provider-completion-original-drift")
            document = json.loads(content)
            if not isinstance(document, dict):
                raise ValueError("provider-completion-original-not-object")
            documents[name] = document
        # 未启动没有 process 原件；保留缺席事实，不生成空文件冒充原始回执。
        process = documents.get("process.json", {})
        cleanup = documents["cleanup.json"]
        raw = controlled_raw_original(
            documents.get("raw-result.json", {}), cleanup,
            raw_present="raw-result.json" in documents,
        )
        validate_controlled_receipts(
            process, raw, cleanup, ownership_nonce=self.ownership_nonce
        )
        return process, raw, cleanup

    def require_complete(self) -> dict[str, object]:
        _, raw, cleanup = self.verified_receipts()
        if cleanup["status"] != "complete":
            raise ValueError("provider-owned-process-cleanup-incomplete")
        return raw


class ProviderExecutionFailure(BaseModel):
    """已启动调用的异常事实与诊断输出；不提供评审判断。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    exception_type: str = Field(min_length=1)
    message: str
    findings_status: Literal["absent", "present", "unavailable"]
    findings_sha256: str | None = Field(default=None, exclude_if=lambda value: value is None)
    findings_error: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _require_diagnostic_identity(self) -> ProviderExecutionFailure:
        if not self.exception_type.strip():
            raise ValueError("provider execution exception type is required")
        if self.findings_status == "present":
            if (self.findings_sha256 is None
                    or re.fullmatch(r"[0-9a-f]{64}", self.findings_sha256) is None
                    or self.findings_error is not None):
                raise ValueError("present provider diagnostics require their original digest")
        elif self.findings_status == "absent":
            if self.findings_sha256 is not None or self.findings_error is not None:
                raise ValueError("absent provider diagnostics cannot claim original bytes")
        elif (self.findings_sha256 is not None or not self.findings_error
              or not self.findings_error.strip()):
            raise ValueError("unavailable provider diagnostics require an actual read error")
        return self


class ProviderRunnerInvocation(LoopArtifactModel):
    """Persistent audit record for one reviewer provider invocation."""

    artifact_kind: str = "provider-runner-invocation"
    provider_id: str
    provider_mode: ProviderMode = ProviderMode.LOCAL_AGENT
    model_selector: str = "current"
    resolved_model: str
    model_resolution_source: ModelResolutionSource
    code_egress: bool = False
    command: str
    argv: list[str] = Field(default_factory=list)
    cwd: str
    input_path: str
    output_path: str
    allowlist: list[str] = Field(default_factory=list)
    isolation_status: ProviderIsolationStatus = ProviderIsolationStatus.NOT_PROVEN
    launch_status: ProviderLaunchStatus = ProviderLaunchStatus.UNKNOWN
    completion_proof: ProviderCompletionProof | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    exit_code: int | None = None
    started_at: str = Field(default_factory=utc_now_iso)
    completed_at: str = ""
    preflight_incomplete: bool = Field(default=False, exclude_if=lambda value: value is False)
    execution_failure: ProviderExecutionFailure | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    status: LoopStatus = LoopStatus.CREATED
    workspace_check: ProviderWorkspaceCheck | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _require_preflight_failure(self) -> ProviderRunnerInvocation:
        if self.execution_failure is not None and (
            self.launch_status != ProviderLaunchStatus.STARTED
            or self.status != LoopStatus.BLOCKED or self.preflight_incomplete
        ):
            raise ValueError("provider execution failure requires a started blocked invocation")
        if self.preflight_incomplete and (
            self.launch_status != ProviderLaunchStatus.NEVER_STARTED
            or self.isolation_status != ProviderIsolationStatus.NOT_PROVEN
            or self.exit_code is not None or self.completion_proof is not None
            or self.status != LoopStatus.BLOCKED or not self.completed_at
            or self.started_at != self.completed_at
            or self.workspace_check is None or self.workspace_check.status != "unproven"
            or self.workspace_check.original_values or self.workspace_check.host_artifact_mutations
        ):
            raise ValueError("incomplete preflight cannot claim execution or a proven workspace")
        return self

    @field_validator(
        "provider_id",
        "model_selector",
        "resolved_model",
        "command",
        "cwd",
        "input_path",
        "output_path",
    )
    @classmethod
    def _require_runner_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider runner field is required")
        return value


class ReviewRun(LoopArtifactModel):
    """Persisted state for one local adversarial PR review run."""

    artifact_kind: str = "review-run"
    review_id: str
    loop_id: str
    loop_type: LoopType = LoopType.LOCAL_PR_REVIEW
    status: LoopStatus = LoopStatus.CREATED
    provider_id: str = ""
    provider_mode: ProviderMode = ProviderMode.LOCAL_AGENT
    model_selector: str = "current"
    resolved_model: str = ""
    model_resolution_status: ModelResolutionStatus = ModelResolutionStatus.NEEDS_USER
    model_resolution_source: ModelResolutionSource | None = None
    code_egress: bool = False
    code_egress_confirmed: bool = False
    diff_source: DiffSourceDescriptor = Field(default_factory=DiffSourceDescriptor)
    source_adapter: str = "local-git-range"
    source_access_status: SourceAccessStatus = SourceAccessStatus.RESOLVED
    source_resolution_path: str = ""
    base_ref: str = ""
    head_ref: str = ""
    base_commit: str = ""
    head_commit: str = ""
    staged_tree_oid: str = ""
    delivery_commit: str = ""
    delivery_parent_commit: str = ""
    provider_command: list[str] = Field(default_factory=list)
    review_pack_path: str = ""
    review_pack_digest: str = ""
    findings_path: str = ""
    findings_digest: str = ""
    resolution_path: str = ""
    final_report_path: str = ""
    final_report_digest: str = ""
    verdict: ReviewVerdict | None = None
    unresolved_blockers: int = 0
    unresolved_required: int = 0
    unresolved_advisory: int = 0
    next_action: str = ""
    updated_at: str = Field(default_factory=utc_now_iso)
    decision_mode: Literal["legacy", "adaptive-quantified"] = Field(
        default="legacy", exclude_if=lambda value: value == "legacy"
    )
    decision_capability: Literal["stage-simulation-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    decision_staged_tree_oid: str = Field(
        default="", exclude_if=lambda value: not value
    )
    decision_started_at_ms: int | None = Field(
        default=None, strict=True, ge=0, exclude_if=lambda value: value is None
    )
    decision_begin_pending_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _decision_identity(self) -> ReviewRun:
        if self.decision_mode == "legacy":
            if (
                self.decision_capability is not None
                or self.decision_staged_tree_oid
                or self.decision_started_at_ms is not None
                or self.decision_begin_pending_digest is not None
            ):
                raise ValueError("pr-decision-identity-invalid")
        elif (
            self.decision_capability != "stage-simulation-v1"
            or self.loop_type != LoopType.LOCAL_PR_REVIEW
            or self.diff_source.source_kind != DiffSourceKind.LOCAL_STAGED
            or not re.fullmatch(r"[0-9a-f]{40,64}", self.decision_staged_tree_oid)
            or not re.fullmatch(r"[0-9a-f]{40,64}", self.staged_tree_oid)
        ):
            raise ValueError("pr-decision-requires-current-staged-tree")
        if (
            self.decision_begin_pending_digest is not None
            and self.decision_started_at_ms is None
        ):
            raise ValueError("pr-decision-pending-begin-requires-start-marker")
        return self

    @field_validator(
        "unresolved_blockers",
        "unresolved_required",
        "unresolved_advisory",
    )
    @classmethod
    def _counts_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("unresolved finding counts cannot be negative")
        return value

    @model_validator(mode="after")
    def _resolved_review_runs_require_model(self) -> ReviewRun:
        if self.model_resolution_status == ModelResolutionStatus.RESOLVED and (
            not self.resolved_model.strip() or self.model_resolution_source is None
        ):
            raise ValueError(
                "resolved review run model state requires resolved_model and source"
            )
        return self


class PRReviewVerificationEvidence(LoopArtifactModel):
    """Local PR 可执行验证结果及其 reviewed staged tree 绑定。"""

    artifact_kind: str = "review-verification-evidence"
    review_id: str
    loop_id: str
    staged_tree_oid: str = ""
    entries: list[str] = Field(default_factory=list)
    results: list[QualityCommandResult] = Field(default_factory=list)

    @field_validator("review_id", "loop_id")
    @classmethod
    def _require_evidence_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("review verification identity is required")
        return normalized


__all__ = [
    "FindingResolution",
    "FindingResolutionStatus",
    "FindingSeverity",
    "DiffSourceDescriptor",
    "DiffSourceKind",
    "ModelResolution",
    "ModelResolutionSource",
    "ModelResolutionStatus",
    "ProviderIsolationStatus",
    "ProviderLaunchStatus",
    "ProviderMode",
    "ProviderRunnerInvocation",
    "ProviderWorkspaceCheck",
    "ReviewFinding",
    "ReviewFindings",
    "ReviewPack",
    "PRReviewVerificationEvidence",
    "ReviewRun",
    "ReviewVerdict",
    "SourceAccessStatus",
    "SourceAdapterResolution",
]
