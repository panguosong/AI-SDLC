"""Persistence helpers for design-contract loop artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from ai_sdlc.core.design_contract_models import (
    CURRENT_DESIGN_CONTRACT_PATH,
    DesignContractArtifactRef,
    DesignContractClose,
    DesignContractInput,
    DesignContractReport,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRun, LoopType, utc_now_iso
from ai_sdlc.core.stable_file_read import read_stable_bytes, read_stable_text
from ai_sdlc.utils.helpers import AI_SDLC_DIR

_SAFE_EXPLICIT_LOOP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def design_contract_input_digest(contract_input: DesignContractInput) -> str:
    """Hash the substantive design input without its creation timestamp."""

    payload = contract_input.model_dump(mode="json", exclude={"created_at"})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class DesignContractArtifacts:
    """Resolved design-contract artifact paths for one loop id."""

    loop_dir: Path
    loop_run_path: Path
    input_path: Path
    coverage_matrix_path: Path
    report_json_path: Path
    report_md_path: Path
    close_path: Path
    pointer_path: Path

    def refs(
        self,
        root: Path,
        *,
        include_close: bool = False,
    ) -> list[DesignContractArtifactRef]:
        paths = (
            ("loop-run", self.loop_run_path),
            ("design-contract-input", self.input_path),
            ("coverage-matrix", self.coverage_matrix_path),
            ("design-contract-report-json", self.report_json_path),
            ("design-contract-report-md", self.report_md_path),
            ("current-design-contract-pointer", self.pointer_path),
        )
        refs = [artifact_ref(root, kind, path) for kind, path in paths]
        if include_close:
            refs.append(artifact_ref(root, "design-contract-close", self.close_path))
        return refs


def build_contract_input(
    *,
    root: Path,
    loop_id: str,
    work_item_dir: Path,
    requirement_loop_id: str,
) -> DesignContractInput:
    """Build a persisted input model from resolved paths."""

    spec_path = work_item_dir / "spec.md"
    plan_path = work_item_dir / "plan.md"
    tasks_path = work_item_dir / "tasks.md"
    return DesignContractInput(
        loop_id=loop_id,
        work_item_id=work_item_dir.name,
        work_item_path=repo_relative_path(root, work_item_dir),
        spec_path=repo_relative_path(root, spec_path),
        spec_digest=_document_digest(read_stable_bytes(root, spec_path)),
        plan_path=repo_relative_path(root, plan_path),
        plan_digest=_document_digest(read_stable_bytes(root, plan_path)),
        tasks_path=repo_relative_path(root, tasks_path),
        tasks_digest=_document_digest(read_stable_bytes(root, tasks_path)),
        requirement_loop_id=requirement_loop_id.strip(),
    )


def _document_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def resolve_loop_id(loop_id: str) -> str:
    """Resolve or generate a safe design-contract loop id."""

    text = loop_id.strip()
    if text:
        return validate_explicit_loop_id(text)
    stamp = utc_now_iso().replace(":", "").replace("-", "").replace("T", "-")
    return f"design-contract-{stamp.lower().removesuffix('z')}-{uuid4().hex[:8]}"


def validate_explicit_loop_id(loop_id: str) -> str:
    """Validate an explicit design-contract loop id for shell-safe rendering."""

    if not _SAFE_EXPLICIT_LOOP_ID.fullmatch(loop_id):
        raise ValueError(
            "explicit loop id may contain only letters, digits, hyphen, and "
            "underscore, and must start with a letter or digit"
        )
    return loop_id


def resolve_work_item_dir(root: Path, work_item: str) -> tuple[Path, str]:
    """Resolve a user-supplied or linked work item path."""

    value = work_item.strip() or _current_work_item_path(root)
    if not value:
        return root / "specs", "Pass --wi specs/<work-item> or link a current work item."
    try:
        path = _resolve_repo_relative_path(root, value)
    except ValueError:
        return root / "specs", f"Work item path must stay within project: {value}"
    if path.is_file() and path.name in {"spec.md", "plan.md", "tasks.md"}:
        path = path.parent
    canonical_blocker = _canonical_work_item_blocker(root, path, value)
    if canonical_blocker:
        return path, canonical_blocker
    if not path.exists():
        return path, f"Work item path does not exist: {value}"
    if not path.is_dir():
        return path, f"Work item path must be a directory or formal doc path: {value}"
    return path, ""


def design_contract_artifacts(root: Path, loop_id: str) -> DesignContractArtifacts:
    """Resolve artifact paths for one design-contract loop id."""

    store = LoopArtifactStore(root)
    loop_dir = store.loop_run_dir(loop_id, loop_type=LoopType.DESIGN_CONTRACT.value)
    return DesignContractArtifacts(
        loop_dir=loop_dir,
        loop_run_path=loop_dir / "loop-run.json",
        input_path=loop_dir / "design-contract-input.json",
        coverage_matrix_path=loop_dir / "coverage-matrix.json",
        report_json_path=loop_dir / "design-contract-report.json",
        report_md_path=loop_dir / "design-contract-report.md",
        close_path=loop_dir / "design-contract-close.json",
        pointer_path=root / CURRENT_DESIGN_CONTRACT_PATH,
    )


def resolve_design_contract_loop_run_path(
    root: Path,
    loop_id: str,
) -> tuple[Path, str]:
    """Resolve a design-contract loop-run path with the stable public signature."""

    path, _expected_loop_id, blocker = _resolve_design_contract_loop_run_identity(
        root,
        loop_id,
    )
    return path, blocker


def _resolve_design_contract_loop_run_identity(
    root: Path,
    loop_id: str,
) -> tuple[Path, str, str]:
    """Resolve a loop-run path together with its trusted expected identity."""

    text = loop_id.strip()
    if text:
        try:
            safe_loop_id = validate_explicit_loop_id(text)
        except ValueError as exc:
            return (
                root / CURRENT_DESIGN_CONTRACT_PATH,
                "",
                f"Invalid design-contract loop id: {exc}",
            )
        artifacts = design_contract_artifacts(root, safe_loop_id)
        current_path, current_loop_id, blocker = _current_design_contract_loop_run_path(
            root
        )
        if blocker:
            return current_path, safe_loop_id, blocker
        if (
            current_loop_id != safe_loop_id
            or current_path.resolve(strict=False)
            != artifacts.loop_run_path.resolve(strict=False)
        ):
            return (
                artifacts.loop_run_path,
                safe_loop_id,
                "Only the current design-contract loop can be closed.",
            )
        return artifacts.loop_run_path, safe_loop_id, ""
    return _current_design_contract_loop_run_path(root)


def _current_design_contract_loop_run_path(root: Path) -> tuple[Path, str, str]:
    pointer_path = root / CURRENT_DESIGN_CONTRACT_PATH
    try:
        payload = LoopArtifactStore(root).read_json_artifact(pointer_path, stable=True)
    except FileNotFoundError:
        return pointer_path, "", "No current design-contract loop exists."
    except (OSError, ValueError) as exc:
        return pointer_path, "", f"Current design-contract pointer is malformed: {exc}"
    loop_id = payload.get("loop_id")
    if not isinstance(loop_id, str) or not loop_id.strip():
        return pointer_path, "", "Current design-contract pointer is missing loop_id."
    try:
        safe_loop_id = validate_explicit_loop_id(loop_id.strip())
    except ValueError as exc:
        return (
            pointer_path,
            "",
            f"Current design-contract pointer loop identity is invalid: {exc}",
        )
    path_text = payload.get("loop_run_path")
    if not isinstance(path_text, str) or not path_text.strip():
        return pointer_path, "", "Current design-contract pointer is missing loop_run_path."
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        return pointer_path, "", "Current design-contract pointer path must be project-relative."
    canonical_root = root.resolve(strict=False)
    candidate = canonical_root / path
    try:
        candidate.resolve(strict=False).relative_to(canonical_root)
    except ValueError:
        return pointer_path, "", "Current design-contract pointer path must stay within project."
    canonical = design_contract_artifacts(canonical_root, safe_loop_id).loop_run_path
    if candidate != canonical:
        return (
            candidate,
            safe_loop_id,
            "Current design-contract pointer identity does not match its loop-run path.",
        )
    return candidate, safe_loop_id, ""


def read_loop_run(path: Path, *, root: Path | None = None) -> LoopRun:
    """Read and validate a design-contract loop-run artifact."""

    try:
        content = (
            read_stable_text(root, path, encoding="utf-8")
            if root is not None
            else path.read_text(encoding="utf-8")
        )
        payload = json.loads(content)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Design-contract loop-run.json is not readable: {exc}") from exc
    try:
        loop_run = LoopRun.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Design-contract loop-run.json is invalid: {exc}") from exc
    if loop_run.loop_type != LoopType.DESIGN_CONTRACT:
        raise ValueError("Design-contract close target is not a design-contract loop.")
    return loop_run


def _design_contract_loop_identity_issue(
    root: Path,
    loop_run_path: Path,
    expected_loop_id: str,
    loop_run: LoopRun,
) -> str:
    canonical = design_contract_artifacts(
        root,
        expected_loop_id,
    ).loop_run_path.resolve(strict=False)
    if loop_run_path.resolve(strict=False) != canonical:
        return "Design-contract loop identity path is not canonical."
    if loop_run.loop_id != expected_loop_id:
        return "Design-contract loop identity does not match the confirmed target."
    return ""


def read_report(path: Path) -> DesignContractReport:
    """Read and validate a design-contract report artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"design-contract-report.json is not readable: {exc}") from exc
    try:
        return DesignContractReport.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"design-contract-report.json is invalid: {exc}") from exc


def _read_close(path: Path) -> DesignContractClose:
    """Read and validate a design-contract close artifact."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"design-contract-close.json is not readable: {exc}") from exc
    try:
        return DesignContractClose.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"design-contract-close.json is invalid: {exc}") from exc


def artifact_ref(root: Path, kind: str, path: Path) -> DesignContractArtifactRef:
    """Return a command-facing artifact reference."""

    return DesignContractArtifactRef(
        kind=kind,
        path=repo_relative_path(root, path),
        exists=path.is_file(),
    )


def repo_relative_path(root: Path, path: Path) -> str:
    """Render a path relative to the repository root when possible."""

    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.as_posix()


def append_unique(values: list[str], value: str) -> list[str]:
    """Append a value while preserving order and avoiding duplicates."""

    if value in values:
        return values
    return [*values, value]


def _current_work_item_path(root: Path) -> str:
    checkpoint = root / AI_SDLC_DIR / "state" / "checkpoint.yml"
    if not checkpoint.is_file():
        return ""
    try:
        payload = yaml.safe_load(checkpoint.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(payload, dict):
        return ""
    plan_uri = payload.get("linked_plan_uri")
    wi_id = payload.get("linked_wi_id")
    if isinstance(plan_uri, str) and plan_uri.strip():
        plan_path = Path(plan_uri)
        plan_parent = plan_path.parent
        if (
            plan_path.name == "plan.md"
            and len(plan_parent.parts) == 2
            and plan_parent.parts[0] == "specs"
        ):
            return plan_parent.as_posix()
    if isinstance(wi_id, str) and wi_id.strip():
        return f"specs/{wi_id}"
    if isinstance(plan_uri, str) and plan_uri.strip():
        return str(Path(plan_uri).parent)
    feature = payload.get("feature")
    if isinstance(feature, dict):
        spec_dir = feature.get("spec_dir")
        if isinstance(spec_dir, str) and spec_dir.strip():
            return spec_dir
    return ""


def _resolve_repo_relative_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)
    resolved.relative_to(root.resolve(strict=False))
    return resolved


def _canonical_work_item_blocker(root: Path, path: Path, original: str) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return f"Work item path must stay within project: {original}"
    if len(relative.parts) != 2 or relative.parts[0] != "specs":
        return f"Work item path must be a canonical specs/<work-item> directory: {original}"
    return ""


__all__ = [
    "DesignContractArtifacts",
    "append_unique",
    "artifact_ref",
    "build_contract_input",
    "design_contract_artifacts",
    "read_loop_run",
    "read_report",
    "repo_relative_path",
    "resolve_design_contract_loop_run_path",
    "resolve_loop_id",
    "resolve_work_item_dir",
    "validate_explicit_loop_id",
]
