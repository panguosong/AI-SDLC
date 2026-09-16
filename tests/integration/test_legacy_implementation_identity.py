"""原生 legacy 的输入身份不能通过删除摘要降级成旧 opaque 凭据。"""

import json
import shutil
import sys

from ai_sdlc.core.design_contract_store import (
    build_contract_input,
    design_contract_input_digest,
)
from ai_sdlc.core.frontend_evidence_loop import _implementation_gate
from ai_sdlc.core.implementation_loop import (
    ImplementationCloseOptions,
    close_implementation_loop,
)
from ai_sdlc.core.implementation_models import ImplementationInput
from ai_sdlc.core.implementation_store import implementation_input_digest
from ai_sdlc.core.loop_models import LoopRun, LoopStatus, LoopType
from ai_sdlc.core.loop_review_service import read_verified_implementation_close
from tests.integration.test_cli_loop_review import _write_cli_expert_results
from tests.integration.test_cli_repair_readiness import (
    LOOP,
    _closed_design_with_supplemented_requirement,
)
from tests.integration.test_frontend_delivery_normal_path import (
    _write_closed_implementation,
)
from tests.integration.test_quantified_implementation import _cli, _payload
from tests.unit.test_implementation_loop import _record_successful_quality_result

IMPLEMENTATION = "native-legacy-identity"


def _loop_bytes(root):
    return {
        p.relative_to(root): p.read_bytes()
        for p in (root / ".ai-sdlc/loops").rglob("*")
        if p.is_file()
    }


def _native_closed_baseline(root):
    requirement, _ = _closed_design_with_supplemented_requirement(root, frontend=True)
    _payload(_cli(
        root, "loop", "implementation", "start", "--wi", "specs/stage-requirement",
        "--design-contract-loop-id", LOOP, "--loop-id", IMPLEMENTATION, "--json",
    ))
    _payload(_cli(
        root, "loop", "implementation", "record", "--loop-id", IMPLEMENTATION,
        "--task-id", "T11", "--status", "done", "--verification", "python -c pass",
        "--json",
    ))
    assert _record_successful_quality_result(root, IMPLEMENTATION, "T11").status == "ready"
    reviewed = _payload(_cli(
        root, "loop", "review", "--type", "implementation",
        "--loop-id", IMPLEMENTATION, "--json",
    ))
    result_args = []
    for path in _write_cli_expert_results(
        root / ".ai-sdlc/loops/implementation" / IMPLEMENTATION, reviewed,
    ):
        result_args += ["--result", str(path)]
    _payload(_cli(
        root, "loop", "review-record", "--type", "implementation",
        "--loop-id", IMPLEMENTATION, "--expect-digest", reviewed["input_digest"],
        *result_args, "--json",
    ))
    opened = root.parent / "native-legacy-open"
    shutil.copytree(root, opened)
    assert _payload(_cli(
        root, "loop", "implementation", "close", "--loop-id", IMPLEMENTATION,
        "--expect-review-digest", reviewed["input_digest"], "--yes", "--json",
    ))["closed"]
    assert not _implementation_gate(root, IMPLEMENTATION, work_item_id="stage-requirement")[2]
    return opened, requirement.relative_to(root)


def _redirect_and_damage(root, requirement, damage, *, design_has_run):
    alternate = "same-wi-without-requirement"
    folder = root / ".ai-sdlc/loops/design-contract" / alternate
    folder.mkdir()
    contract = build_contract_input(
        root=root, loop_id=alternate,
        work_item_dir=root / "specs/stage-requirement", requirement_loop_id="",
    )
    (folder / "design-contract-input.json").write_text(contract.model_dump_json())
    if design_has_run:
        run = LoopRun(
            loop_id=alternate, loop_type=LoopType.DESIGN_CONTRACT,
            status=LoopStatus.CLOSED, work_item_id="stage-requirement",
            input_digest=design_contract_input_digest(contract),
        )
        (folder / "loop-run.json").write_text(run.model_dump_json())
    directory = root / ".ai-sdlc/loops/implementation" / IMPLEMENTATION
    input_path, run_path = directory / "implementation-input.json", directory / "loop-run.json"
    impl_input = ImplementationInput.model_validate_json(input_path.read_bytes())
    changed = impl_input.model_copy(update={
        "design_contract_loop_id": alternate,
        "design_contract_report_path": (folder / "design-contract-report.json").relative_to(root).as_posix(),
    })
    input_path.write_text(changed.model_dump_json())
    run = json.loads(run_path.read_bytes())
    if damage == "digest-deleted":
        run.pop("input_digest")
    elif damage == "digest-empty":
        run["input_digest"] = ""
    elif damage == "digest-recomputed":
        run["input_digest"] = implementation_input_digest(changed)
    else:
        run["input_digest"] = "sha256:" + "a" * 64
    run_path.write_text(json.dumps(run))
    (root / requirement / "repair-readiness-supplement.json").unlink()


def test_native_legacy_identity_cannot_downgrade_and_erase_requirement(initialized_project_dir):
    root = initialized_project_dir
    opened, requirement = _native_closed_baseline(root)
    original_closed, original_open = _loop_bytes(root), _loop_bytes(opened)
    results = {}
    for damage in ("digest-deleted", "digest-empty", "digest-replaced", "digest-recomputed"):
        for consumer in ("record", "verify", "close", "frontend"):
            case = root.parent / f"identity-{damage}-{consumer}"
            shutil.copytree(root if consumer == "frontend" else opened, case)
            # 线上反例为无 Design run 的 opaque 冒充；重算摘要另有完整无 Requirement Design。
            _redirect_and_damage(
                case, requirement, damage,
                design_has_run=consumer != "frontend" or damage == "digest-recomputed",
            )
            before = _loop_bytes(case)
            if consumer == "frontend":
                blocked = bool(_implementation_gate(case, IMPLEMENTATION, work_item_id="stage-requirement")[2])
            elif consumer == "close":
                result = close_implementation_loop(ImplementationCloseOptions(
                    root=case, loop_id=IMPLEMENTATION, yes=True,
                ))
                blocked = result.status == "blocked" and not result.closed
            else:
                argv = ["loop", "implementation", consumer, "--loop-id", IMPLEMENTATION, "--task-id", "T11"]
                argv += (["--status", "in_progress", "--json"] if consumer == "record" else [
                    "--json", "--", sys.executable, "-c",
                    "from pathlib import Path; Path('unexpected-execution').write_text('ran')",
                ])
                result = _cli(case, *argv)
                blocked = result.returncode == 1 and json.loads(result.stdout)["status"] == "blocked"
            results[f"{damage}/{consumer}"] = {
                "blocked": blocked,
                "original_history_not_rewritten": _loop_bytes(case) == before,
                "no_execution": not (case / "unexpected-execution").exists(),
            }
    # 输入与摘要一起删除仍有原生 execution/review 足迹，不能落入无输入旧凭据。
    erased = root.parent / "native-legacy-input-and-digest-erased"
    shutil.copytree(root, erased)
    directory = erased / ".ai-sdlc/loops/implementation" / IMPLEMENTATION
    (directory / "implementation-input.json").unlink()
    run_path = directory / "loop-run.json"
    run = json.loads(run_path.read_bytes())
    run.pop("input_digest")
    run_path.write_text(json.dumps(run))
    before = _loop_bytes(erased)
    results["input-and-digest-erased"] = {
        "blocked": bool(_implementation_gate(erased, IMPLEMENTATION, work_item_id="stage-requirement")[2]),
        "original_history_not_rewritten": _loop_bytes(erased) == before,
    }
    assert _loop_bytes(root) == original_closed
    assert _loop_bytes(opened) == original_open
    assert all(all(checks.values()) for checks in results.values()), results


def test_true_opaque_historical_implementation_still_consumes_unbound_design(tmp_path):
    work_item = tmp_path / "specs/001-ui"
    work_item.mkdir(parents=True)
    for name in ("spec.md", "plan.md", "tasks.md"):
        (work_item / name).write_text("# Historical frontend\n")
    _write_closed_implementation(tmp_path, "001-ui")
    before = _loop_bytes(tmp_path)
    assert read_verified_implementation_close(tmp_path, "impl-frontend-normal").loop_id == "impl-frontend-normal"
    assert _loop_bytes(tmp_path) == before
