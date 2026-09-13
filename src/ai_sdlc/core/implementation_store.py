"""Persistence helpers for implementation loop artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ai_sdlc.core.implementation_models import (
    CURRENT_IMPLEMENTATION_PATH,
    ImplementationArtifactRef,
    ImplementationInput,
    ImplementationProgress,
    ImplementationReport,
    ImplementationTaskItem,
    ImplementationTasks,
    ImplementationVerificationEvidence,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRun, LoopType, utc_now_iso
from ai_sdlc.core.plan_check import parse_markdown_frontmatter
from ai_sdlc.core.stable_file_read import _stable_regular_file_exists, read_stable_bytes
from ai_sdlc.core.state_machine import load_work_item, work_item_path
from ai_sdlc.models.work import WorkType

_SAFE_EXPLICIT_LOOP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_WINDOWS_DEVICE_NAMES = {
    "AUX",
    "CLOCK$",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ImplementationArtifacts:
    """Resolved implementation artifact paths for one loop id."""

    loop_dir: Path
    loop_run_path: Path
    input_path: Path
    tasks_path: Path
    progress_path: Path
    evidence_path: Path
    report_json_path: Path
    report_md_path: Path
    close_path: Path
    pointer_path: Path

    def refs(
        self,
        root: Path,
        *,
        include_close: bool = False,
    ) -> list[ImplementationArtifactRef]:
        paths = (
            ("loop-run", self.loop_run_path),
            ("implementation-input", self.input_path),
            ("implementation-tasks", self.tasks_path),
            ("implementation-progress", self.progress_path),
            ("verification-evidence", self.evidence_path),
            ("implementation-report-json", self.report_json_path),
            ("implementation-report-md", self.report_md_path),
            ("current-implementation-pointer", self.pointer_path),
        )
        refs = [artifact_ref(root, kind, path) for kind, path in paths]
        if include_close:
            refs.append(artifact_ref(root, "implementation-close", self.close_path))
        return refs


def build_implementation_input(
    *,
    root: Path,
    loop_id: str,
    work_item_dir: Path,
    design_contract_loop_id: str,
    design_contract_report_path: str,
    task_items: list[ImplementationTaskItem] | None = None,
    decision_mode: str = "legacy",
    decision_capability: str | None = None,
) -> ImplementationInput:
    """Build a persisted input model from resolved paths."""

    items = list(task_items or [])
    work_type, quality_profiles = _implementation_quality_profile(
        root,
        work_item_dir,
    )
    scope = list(dict.fromkeys(path for item in items for path in item.files))
    acceptance = [value for item in items for value in item.acceptance]
    return ImplementationInput(
        loop_id=loop_id,
        work_item_id=work_item_dir.name,
        work_item_path=repo_relative_path(root, work_item_dir),
        spec_path=repo_relative_path(root, work_item_dir / "spec.md"),
        plan_path=repo_relative_path(root, work_item_dir / "plan.md"),
        tasks_path=repo_relative_path(root, work_item_dir / "tasks.md"),
        design_contract_loop_id=design_contract_loop_id,
        design_contract_report_path=design_contract_report_path,
        work_type=work_type,
        quality_profiles=quality_profiles,
        declared_scope=scope,
        task_scopes={item.task_id: item.files for item in items},
        tasks_digest=implementation_task_items_digest(items),
        acceptance_digest=_stable_digest(acceptance),
        decision_mode=decision_mode,
        decision_capability=(
            decision_capability or "implementation-b1"
            if decision_mode == "adaptive-quantified"
            else decision_capability
        ),
    )


def _implementation_quality_profile(
    root: Path,
    work_item_dir: Path,
) -> tuple[WorkType, list[str]]:
    work_item_id = work_item_dir.name
    path = work_item_path(root, work_item_id)
    if path.is_file():
        work_type = load_work_item(root, work_item_id).work_type
    else:
        spec_path = work_item_dir / "spec.md"
        if not spec_path.is_file():
            return WorkType.UNCERTAIN, []
        frontmatter, _ = parse_markdown_frontmatter(spec_path)
        raw_work_type = frontmatter.get("work_type")
        try:
            work_type = (
                WorkType(str(raw_work_type))
                if raw_work_type is not None
                else WorkType.UNCERTAIN
            )
        except ValueError as exc:
            raise ValueError("formal work_type metadata is invalid") from exc
    return work_type, []


def _stable_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def implementation_task_items_digest(items: list[ImplementationTaskItem]) -> str:
    """Hash the task content frozen into one Implementation input."""

    return _stable_digest([item.model_dump(mode="json") for item in items])


def implementation_input_digest(impl_input: ImplementationInput) -> str:
    """Hash stable implementation input content without timestamp provenance."""

    return _stable_digest(_without_provenance(impl_input.model_dump(mode="json")))


def validate_implementation_lifecycle(root: Path, current: ImplementationInput) -> None:
    """同工作项的冻结合同只允许一个 B1；换 ID 或模式不产生新预算。"""

    parent = implementation_artifacts(root, current.loop_id).loop_dir.parent
    if not parent.exists():
        return
    try:
        for directory in sorted(parent.iterdir()):
            if directory.name == current.loop_id or not (
                directory.is_dir() or directory.is_symlink()
            ):
                continue
            previous = _lifecycle_peer_input(root, directory, current.work_item_id)
            if previous is None or (
                previous.work_item_id != current.work_item_id
                and previous.work_item_path != current.work_item_path
            ):
                continue
            if previous.decision_mode == current.decision_mode == "legacy":
                continue
            run = LoopRun.model_validate_json(
                read_stable_bytes(root, directory / "loop-run.json")
            )
            if (
                previous.loop_id != directory.name
                or previous.work_item_path != current.work_item_path
                or run.loop_id != previous.loop_id
                or run.loop_type != LoopType.IMPLEMENTATION
                or run.work_item_id != previous.work_item_id
                or run.decision_mode != previous.decision_mode
                or run.decision_capability != previous.decision_capability
                or run.input_digest != implementation_input_digest(previous)
            ):
                raise ValueError("persisted Implementation identity does not match")
            if (
                previous.design_contract_loop_id == current.design_contract_loop_id
                or _lifecycle_contract(root, previous)
                == _lifecycle_contract(root, current)
            ):
                raise ValueError(
                    f"decision-lifecycle-conflict: existing Implementation {previous.loop_id}"
                )
    except (OSError, ValueError) as exc:
        raise ValueError(f"decision-lifecycle-unavailable: {exc}") from exc


def _lifecycle_peer_input(
    root: Path, directory: Path, work_item_id: str
) -> ImplementationInput | None:
    path = directory / "implementation-input.json"
    if _stable_regular_file_exists(root, path):
        return ImplementationInput.model_validate_json(read_stable_bytes(root, path))
    run_path = directory / "loop-run.json"
    if _stable_regular_file_exists(root, run_path):
        run = LoopRun.model_validate_json(read_stable_bytes(root, run_path))
        if run.work_item_id == work_item_id:
            raise ValueError(f"Implementation input is missing: {directory.name}")
    return None


def _lifecycle_contract(root: Path, impl_input: ImplementationInput) -> tuple:
    from ai_sdlc.core.design_contract_models import DesignContractInput
    from ai_sdlc.core.design_contract_store import (
        design_contract_artifacts,
        design_contract_input_digest,
    )

    artifacts = design_contract_artifacts(root, impl_input.design_contract_loop_id)
    contract = DesignContractInput.model_validate_json(
        read_stable_bytes(root, artifacts.input_path)
    )
    run = LoopRun.model_validate_json(read_stable_bytes(root, artifacts.loop_run_path))
    if (
        contract.loop_id != impl_input.design_contract_loop_id
        or contract.work_item_id != impl_input.work_item_id
        or contract.work_item_path != impl_input.work_item_path
        or run.loop_id != contract.loop_id
        or run.work_item_id != contract.work_item_id
        or run.loop_type != LoopType.DESIGN_CONTRACT
        or run.status != "closed"
        or run.input_digest != design_contract_input_digest(contract)
    ):
        raise ValueError("frozen Design identity does not match")
    # 比较历史冻结内容，不用当前文档覆盖旧摘要；只改上游 ID 也不能刷新次数。
    return (
        contract.spec_digest,
        contract.plan_digest,
        contract.tasks_digest,
        tuple(sorted(contract.authorized_scope_families)),
        contract.scope_authority_digest,
    )


def _without_provenance(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_provenance(item)
            for key, item in value.items()
            if key not in {"created_at", "ai_sdlc_version"}
        }
    if isinstance(value, list):
        return [_without_provenance(item) for item in value]
    return value


def resolve_loop_id(loop_id: str) -> str:
    """Resolve or generate a safe implementation loop id."""

    text = loop_id.strip()
    if text:
        return validate_explicit_loop_id(text)
    stamp = utc_now_iso().replace(":", "").replace("-", "").replace("T", "-")
    return f"implementation-{stamp.lower().removesuffix('z')}-{uuid4().hex[:8]}"


def validate_explicit_loop_id(loop_id: str) -> str:
    """Validate an explicit implementation loop id for shell-safe rendering."""

    if (
        not _SAFE_EXPLICIT_LOOP_ID.fullmatch(loop_id)
        or loop_id.upper() in _WINDOWS_DEVICE_NAMES
    ):
        raise ValueError(
            "explicit loop id may contain only letters, digits, hyphen, and "
            "underscore, must start with a letter or digit, and must be a "
            "portable file name"
        )
    return loop_id


def implementation_artifacts(root: Path, loop_id: str) -> ImplementationArtifacts:
    """Resolve artifact paths for one implementation loop id."""

    store = LoopArtifactStore(root)
    loop_dir = store.loop_run_dir(loop_id, loop_type=LoopType.IMPLEMENTATION.value)
    return ImplementationArtifacts(
        loop_dir=loop_dir,
        loop_run_path=loop_dir / "loop-run.json",
        input_path=loop_dir / "implementation-input.json",
        tasks_path=loop_dir / "implementation-tasks.json",
        progress_path=loop_dir / "implementation-progress.json",
        evidence_path=loop_dir / "verification-evidence.json",
        report_json_path=loop_dir / "implementation-report.json",
        report_md_path=loop_dir / "implementation-report.md",
        close_path=loop_dir / "implementation-close.json",
        pointer_path=root / CURRENT_IMPLEMENTATION_PATH,
    )


def resolve_implementation_loop_run_path(
    root: Path,
    loop_id: str,
) -> tuple[Path, str]:
    """Resolve an explicit or current implementation loop-run path."""

    text = loop_id.strip()
    if text:
        try:
            safe_loop_id = validate_explicit_loop_id(text)
        except ValueError as exc:
            return (
                root / CURRENT_IMPLEMENTATION_PATH,
                f"Invalid implementation loop id: {exc}",
            )
        return implementation_artifacts(root, safe_loop_id).loop_run_path, ""
    return _current_implementation_loop_run_path(root)


def read_loop_run(path: Path) -> LoopRun:
    """Read and validate an implementation loop-run artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"Implementation loop-run.json is not readable: {exc}"
        ) from exc
    try:
        loop_run = LoopRun.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Implementation loop-run.json is invalid: {exc}") from exc
    if loop_run.loop_type != LoopType.IMPLEMENTATION:
        raise ValueError("Implementation target is not an implementation loop.")
    return loop_run


def read_input(path: Path) -> ImplementationInput:
    """Read and validate implementation input artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"implementation-input.json is not readable: {exc}") from exc
    try:
        return ImplementationInput.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"implementation-input.json is invalid: {exc}") from exc


def read_tasks(path: Path) -> ImplementationTasks:
    """Read and validate implementation tasks artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"implementation-tasks.json is not readable: {exc}") from exc
    try:
        return ImplementationTasks.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"implementation-tasks.json is invalid: {exc}") from exc


def read_progress(path: Path) -> ImplementationProgress:
    """Read and validate implementation progress artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"implementation-progress.json is not readable: {exc}"
        ) from exc
    try:
        return ImplementationProgress.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"implementation-progress.json is invalid: {exc}") from exc


def read_evidence(path: Path) -> ImplementationVerificationEvidence:
    """Read and validate verification evidence artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"verification-evidence.json is not readable: {exc}") from exc
    try:
        return ImplementationVerificationEvidence.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"verification-evidence.json is invalid: {exc}") from exc


def read_report(path: Path) -> ImplementationReport:
    """Read and validate implementation report artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"implementation-report.json is not readable: {exc}") from exc
    try:
        return ImplementationReport.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"implementation-report.json is invalid: {exc}") from exc


def artifact_ref(root: Path, kind: str, path: Path) -> ImplementationArtifactRef:
    """Return a command-facing artifact reference."""

    return ImplementationArtifactRef(
        kind=kind,
        path=repo_relative_path(root, path),
        exists=path.is_file(),
    )


def repo_relative_path(root: Path, path: Path) -> str:
    """Render a path relative to the repository root when possible."""

    try:
        return (
            path.resolve(strict=False)
            .relative_to(root.resolve(strict=False))
            .as_posix()
        )
    except ValueError:
        return path.as_posix()


def append_unique(values: list[str], value: str) -> list[str]:
    """Append a value while preserving order and avoiding duplicates."""

    if value in values:
        return values
    return [*values, value]


def _current_implementation_loop_run_path(root: Path) -> tuple[Path, str]:
    pointer_path = root / CURRENT_IMPLEMENTATION_PATH
    if not pointer_path.is_file():
        return pointer_path, "No current implementation loop exists."
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return pointer_path, f"Current implementation pointer is malformed: {exc}"
    if not isinstance(payload, dict):
        return (
            pointer_path,
            "Current implementation pointer is malformed: root must be an object.",
        )
    loop_id = payload.get("loop_id")
    if not isinstance(loop_id, str) or not loop_id.strip():
        return pointer_path, "Current implementation pointer is missing loop_id."
    path_text = payload.get("loop_run_path")
    if not isinstance(path_text, str) or not path_text.strip():
        return pointer_path, "Current implementation pointer is missing loop_run_path."
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        return (
            pointer_path,
            "Current implementation pointer path must be project-relative.",
        )
    candidate = (root / path).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve(strict=False))
    except ValueError:
        return (
            pointer_path,
            "Current implementation pointer path must stay within project.",
        )
    return candidate, ""


__all__ = [
    "ImplementationArtifacts",
    "append_unique",
    "artifact_ref",
    "build_implementation_input",
    "implementation_artifacts",
    "implementation_input_digest",
    "implementation_task_items_digest",
    "read_evidence",
    "read_input",
    "read_loop_run",
    "read_progress",
    "read_report",
    "read_tasks",
    "repo_relative_path",
    "resolve_implementation_loop_run_path",
    "resolve_loop_id",
    "validate_explicit_loop_id",
]
