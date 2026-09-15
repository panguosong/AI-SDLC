"""Persistence helpers for design-contract loop artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
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
DESIGN_CHECK_PENDING = "design-check-publication.pending.json"


def require_design_check_published(loop_dir: Path) -> None:
    """未完成的多文件写入只能由同 Loop 的 check 恢复，不能用于后续判断。"""
    pending = loop_dir / DESIGN_CHECK_PENDING
    if pending.exists() or pending.is_symlink():
        raise ValueError(
            "design-contract-publication-pending: rerun check for the same loop"
        )


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
    decision_mode: str = "legacy",
    decision_capability: str | None = None,
    verification_contract: str = "",
    captured_artifacts: dict[str, bytes] | None = None,
) -> DesignContractInput:
    """Build a persisted input model from resolved paths."""

    spec_path = work_item_dir / "spec.md"
    plan_path = work_item_dir / "plan.md"
    tasks_path = work_item_dir / "tasks.md"
    artifacts = design_contract_artifacts(root, loop_id)
    previous = (
        DesignContractInput.model_validate_json(
            read_stable_bytes(root, artifacts.input_path)
        )
        if artifacts.input_path.is_file()
        else None
    )
    reference = verification_contract or (
        previous.verification_contract_ref if previous is not None else ""
    )
    binding = {}
    content = None
    if reference:
        from ai_sdlc.core.counterexample_models import (
            VERIFICATION_CAPABILITY,
            project_relative_path,
        )

        project_relative_path(reference)
        content = read_stable_bytes(root, root / reference)
        digest = hashlib.sha256(content).hexdigest()
        capture = artifacts.loop_dir / f"verification-contract-{digest}.json"
        binding = {
            "verification_capability": VERIFICATION_CAPABILITY,
            "verification_contract_ref": repo_relative_path(root, capture),
            "verification_contract_digest": digest,
        }
    contract_input = DesignContractInput(
        loop_id=loop_id,
        work_item_id=work_item_dir.name,
        work_item_path=repo_relative_path(root, work_item_dir),
        spec_path=repo_relative_path(root, spec_path),
        spec_digest=_document_digest(read_stable_bytes(root, spec_path)),
        plan_path=repo_relative_path(root, plan_path),
        plan_digest=_document_digest(read_stable_bytes(root, plan_path)),
        tasks_path=repo_relative_path(root, tasks_path),
        tasks_digest=_document_digest(read_stable_bytes(root, tasks_path)),
        requirement_loop_id=requirement_loop_id.strip()
        or (
            previous.requirement_loop_id
            if previous is not None and previous.verification_capability is not None
            else ""
        ),
        decision_mode=decision_mode,
        decision_capability=decision_capability,
        **binding,
    )
    if content is not None:
        staged = {contract_input.verification_contract_ref: content}
        _, material = read_verification_contract(
            root, contract_input, staged_content=staged
        )
        if captured_artifacts is not None:
            captured_artifacts.update(material)
    return contract_input


def verification_binding(stage_input) -> dict[str, str | None]:
    """沿原输入传递能力；不从报告结论推断是否必达。"""
    return {
        name: getattr(stage_input, name)
        for name in (
            "verification_capability",
            "verification_contract_ref",
            "verification_contract_digest",
        )
    }


def read_verification_contract(
    root: Path,
    frozen_input,
    captured_artifacts: Mapping[str, bytes] | None = None,
    *,
    staged_content: Mapping[str, bytes] | None = None,
):
    """复用同一完整合同和原条目核验，返回审查必须捕获的全部内容。"""
    design_id = getattr(frozen_input, "design_contract_loop_id", frozen_input.loop_id)
    if design_id:
        require_design_check_published(
            design_contract_artifacts(root, design_id).loop_dir
        )
    from ai_sdlc.core.counterexample_models import (
        VerificationContract,
        obligation_task_owners,
        project_relative_path,
        validate_contract_sources,
    )
    from ai_sdlc.core.loop_stage_input import validate_verification_identity

    validate_verification_identity(frozen_input)
    if frozen_input.verification_capability is None:
        return None, {}
    expected = repo_relative_path(
        root,
        design_contract_artifacts(root, design_id).loop_dir
        / f"verification-contract-{frozen_input.verification_contract_digest}.json",
    )
    if frozen_input.verification_contract_ref != expected:
        raise ValueError("counterexample-contract-owner-mismatch")
    material: dict[str, bytes] = {}

    def read(path: str) -> bytes:
        project_relative_path(path)
        if path in material:
            return material[path]
        if captured_artifacts is not None:
            if path not in captured_artifacts:
                raise ValueError(f"counterexample-captured-source-missing: {path}")
            content = captured_artifacts[path]
        elif staged_content is not None and path in staged_content:
            content = staged_content[path]
        else:
            content = read_stable_bytes(root, root / path)
        material[path] = content
        return content

    raw = read(expected)
    if hashlib.sha256(raw).hexdigest() != frozen_input.verification_contract_digest:
        raise ValueError("counterexample-contract-digest-mismatch")
    contract = VerificationContract.model_validate_json(raw)
    if contract.work_item_id != frozen_input.work_item_id:
        raise ValueError("counterexample-contract-work-item-mismatch")
    from ai_sdlc.core.design_contract_checks import _TASK_ID, _task_sections

    tasks_content = read(frozen_input.tasks_path)
    task_ids = tuple(
        task_id
        for section in _task_sections(tasks_content.decode("utf-8"))
        if (task_id := next(iter(_TASK_ID.findall(section)), ""))
    )
    if isinstance(frozen_input, DesignContractInput):
        if _document_digest(tasks_content) != frozen_input.tasks_digest:
            raise ValueError("counterexample-original-tasks-digest-mismatch")
    elif getattr(frozen_input, "task_scopes", None):
        if set(task_ids) != set(frozen_input.task_scopes):
            raise ValueError("counterexample-original-task-set-mismatch")
    else:
        # 历史 Implementation 输入可能尚无 task_scopes，必须回到原设计全文确认。
        upstream = DesignContractInput.model_validate_json(
            read(
                repo_relative_path(
                    root, design_contract_artifacts(root, design_id).input_path
                )
            )
        )
        if (
            upstream.tasks_path != frozen_input.tasks_path
            or _document_digest(tasks_content) != upstream.tasks_digest
        ):
            raise ValueError("counterexample-original-tasks-digest-mismatch")
    obligation_task_owners(contract, task_ids=task_ids)
    entries: dict[str, dict[str, bytes]] = {}
    for source in contract.sources:
        content = read(source.path)
        if source.namespace == "task":
            if source.path != frozen_input.tasks_path:
                raise ValueError("counterexample-original-task-path-mismatch")
            entry = _verification_task_entry(content, source.task_id, source.locator)
        else:
            if source.namespace == "spec" and source.path != frozen_input.spec_path:
                raise ValueError("counterexample-original-spec-path-mismatch")
            if source.namespace == "requirement":
                _verification_requirement_source(root, frozen_input, source, read)
            entry = _verification_spec_entry(content, source.locator)
        entries.setdefault(source.path, {})[source.locator] = entry
    budget = read(contract.budget_ref.path)
    if hashlib.sha256(budget).hexdigest() != contract.budget_ref.sha256:
        raise ValueError("counterexample-original-budget-digest-mismatch")
    # 比较时忽略阶段目录大小写，原路径和内容摘要仍按原件绑定。
    budget_path = contract.budget_ref.path.casefold()
    if (
        "/loops/implementation/" in budget_path
        or "/loops/design-contract/" in budget_path
    ):
        raise ValueError("counterexample-future-stage-budget-forbidden")
    validate_contract_sources(contract, material, entries)
    if captured_artifacts is None:
        for path, content in material.items():
            if staged_content is not None and path in staged_content:
                continue
            if read_stable_bytes(root, root / path) != content:
                raise ValueError("counterexample-contract-source-drift")
    return contract, material


def _verification_spec_entry(content: bytes, locator: str) -> bytes:
    """条目仅来自原规范：唯一标题正文或唯一编号需求行。"""
    from ai_sdlc.core.design_contract_checks import (
        _contract_source_text,
        _without_fenced_blocks,
    )

    text = content.decode("utf-8")
    lines = _without_fenced_blocks(text).splitlines()
    headings = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"#{1,6}\s+" + re.escape(locator) + r"\s*", line)
    ]
    if len(headings) == 1:
        heading_level = len(lines[headings[0]].split(maxsplit=1)[0])
        start = headings[0] + 1
        # 子标题属于当前来源条目，只有同级或上级标题结束该条目。
        end = next(
            (
                i
                for i in range(start, len(lines))
                if re.match(rf"^#{{1,{heading_level}}}\s", lines[i])
            ),
            len(lines),
        )
        entry = "\n".join(lines[start:end]).strip("\n").encode("utf-8")
        if not entry.strip():
            raise ValueError("counterexample-original-spec-entry-empty")
        return entry
    pattern = re.compile(
        r"^\s*[-*]\s+(?:\*\*)?" + re.escape(locator) + r"(?:\*\*)?\s*[:：]\s*(.+)$"
    )
    matches = [
        match.group(1)
        for line in _contract_source_text(text).splitlines()
        if (match := pattern.match(line))
    ]
    if len(matches) != 1 or headings:
        raise ValueError("counterexample-original-spec-entry-unavailable-or-duplicate")
    return matches[0].encode("utf-8")


def _verification_task_entry(content: bytes, task_id: str, locator: str) -> bytes:
    from ai_sdlc.core.implementation_loop import (
        _TASK_ID,
        _task_list_after_label,
        _task_sections,
    )

    match = re.fullmatch(re.escape(task_id) + r"/acceptance/([1-9][0-9]*)", locator)
    sections = [
        section
        for section in _task_sections(content.decode("utf-8"))
        if next(iter(_TASK_ID.findall(section)), "") == task_id
    ]
    if match is None or len(sections) != 1:
        raise ValueError("counterexample-original-task-entry-unavailable")
    values = _task_list_after_label(sections[0], "验收标准", "acceptance")
    index = int(match.group(1)) - 1
    if index >= len(values):
        raise ValueError("counterexample-original-task-entry-unavailable")
    return values[index].encode("utf-8")


def _verification_requirement_source(root, frozen_input, source, read):
    from ai_sdlc.core.loop_stage_decision_service import parse_stage_simulation_context
    from ai_sdlc.core.loop_stage_input import stage_input_identity
    from ai_sdlc.core.requirement_loop import (
        RequirementFreeze,
        RequirementIntake,
        _requirement_intake_digest,
    )

    design_id = getattr(frozen_input, "design_contract_loop_id", None)
    if design_id is not None:
        upstream = DesignContractInput.model_validate_json(
            read(
                repo_relative_path(
                    root, design_contract_artifacts(root, design_id).input_path
                )
            )
        )
    else:
        upstream = frozen_input
    if source.loop_id != upstream.requirement_loop_id:
        raise ValueError("counterexample-original-requirement-loop-mismatch")
    directory = Path(".ai-sdlc/loops/requirement") / validate_explicit_loop_id(
        source.loop_id
    )
    context = parse_stage_simulation_context(
        read((directory / "decision-context.json").as_posix())
    )
    intake = RequirementIntake.model_validate_json(
        read((directory / "requirement-intake.json").as_posix())
    )
    run = LoopRun.model_validate_json(read((directory / "loop-run.json").as_posix()))
    freeze = RequirementFreeze.model_validate_json(
        read((directory / "requirement-freeze.json").as_posix())
    )
    if (
        context.loop_id != source.loop_id
        or context.loop_type != "requirement"
        or run.status != "closed"
        or run.loop_id != source.loop_id
        or intake.loop_id != source.loop_id
        or (intake.work_item_id and intake.work_item_id != frozen_input.work_item_id)
        or context.capability != "stage-simulation-v1"
        or context.phase != "review_sealed"
        or run.loop_type != "requirement"
        or run.decision_capability != context.capability
        or intake.decision_capability != context.capability
        or freeze.loop_id != source.loop_id
        or freeze.artifact_kind != "requirement-freeze"
        or freeze.intake_path != (directory / "requirement-intake.json").as_posix()
        or freeze.intake_digest != _requirement_intake_digest(intake)
        or freeze.acceptance_count != len(intake.acceptance_criteria)
        or context.implementation_input_digest
        != stage_input_identity("requirement", intake)
    ):
        raise ValueError("counterexample-original-requirement-identity-mismatch")
    profiles = [
        item for item in context.contracts if item.profile_id == source.profile_id
    ]
    if len(profiles) != 1:
        raise ValueError("counterexample-original-profile-missing")
    profile = profiles[0]
    obligations = [
        item
        for item in profile.goal_contract.obligations
        if item.id == source.obligation_id and item.goal_id == source.goal_id
    ]
    if len(obligations) != 1 or source.goal_id not in {
        item.id for item in profile.goal_contract.goals
    }:
        raise ValueError("counterexample-original-obligation-mismatch")
    criteria = [item for item in profile.criteria if item.id == source.criterion_id]
    if source.criterion_id and (
        len(criteria) != 1
        or source.goal_id not in {share.goal_id for share in criteria[0].goal_shares}
    ):
        raise ValueError("counterexample-original-criterion-mismatch")
    originals = [
        item
        for item in context.sources
        if item.id == obligations[0].source_ref
        and item.path == source.path
        and item.sha256 == source.sha256
    ]
    if len(originals) != 1 or source.path.startswith(".ai-sdlc/loops/"):
        raise ValueError("counterexample-original-business-source-mismatch")


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
        return (
            root / "specs",
            "Pass --wi specs/<work-item> or link a current work item.",
        )
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
        if current_loop_id != safe_loop_id or current_path.resolve(
            strict=False
        ) != artifacts.loop_run_path.resolve(strict=False):
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
        return (
            pointer_path,
            "",
            "Current design-contract pointer is missing loop_run_path.",
        )
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        return (
            pointer_path,
            "",
            "Current design-contract pointer path must be project-relative.",
        )
    canonical_root = root.resolve(strict=False)
    candidate = canonical_root / path
    try:
        candidate.resolve(strict=False).relative_to(canonical_root)
    except ValueError:
        return (
            pointer_path,
            "",
            "Current design-contract pointer path must stay within project.",
        )
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

    require_design_check_published(path.parent)
    try:
        content = (
            read_stable_text(root, path, encoding="utf-8")
            if root is not None
            else path.read_text(encoding="utf-8")
        )
        payload = json.loads(content)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"Design-contract loop-run.json is not readable: {exc}"
        ) from exc
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

    require_design_check_published(path.parent)
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

    require_design_check_published(path.parent)
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
