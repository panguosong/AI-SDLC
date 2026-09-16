"""补录只开启原剩余复审；合成判断用于协议接线，不是实际专家验收。"""

import json
from pathlib import Path

from tests.integration.test_quantified_implementation import _cli, _payload
from tests.integration.test_stage_quantified_pipeline import (
    CAPABILITY,
    LOOP,
    actual_result_args,
    stage_apply,
    stage_selected,
)


def test_requirement_repair_supplement_preserves_r1_and_freezes_after_r2(
    initialized_project_dir,
):
    root = initialized_project_dir
    stage_selected(root, "requirement")
    stage_apply(
        root, "requirement", {"operation": "seal-for-review", "request_id": "seal"}
    )
    reviewed = _payload(
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        )
    )
    result_args = actual_result_args(root, "requirement", reviewed, status="FAIL")
    for filename in result_args[1::2]:
        path = Path(filename)
        result = json.loads(path.read_text())
        result["assessment"]["repair_readiness"] = {
            "authorization": "UNKNOWN",
            "facts": "UNKNOWN",
            "verification": "UNKNOWN",
            "evidence_refs": [],
            "reason": "送审资料未包括原范围修复依据",
        }
        path.write_text(json.dumps(result))
    first = _payload(
        _cli(
            root,
            "loop",
            "review-record",
            "--type",
            "requirement",
            "--loop-id",
            LOOP,
            "--expect-digest",
            reviewed["input_digest"],
            *result_args,
            "--json",
        )
    )
    assert first["reason"] == "repair-unavailable"
    directory = root / ".ai-sdlc/loops/requirement" / LOOP
    originals = {
        name: (directory / name).read_bytes()
        for name in ("review-outcome-round-1.json", "decision-context.json")
    }
    basis = root / ".ai-sdlc/reviews/repair-basis.md"
    basis.parent.mkdir(parents=True, exist_ok=True)
    basis.write_text("用户授权原范围内修复；按原义务补充拒绝越权行为及其验收方法。")
    common = [
        "--type",
        "requirement",
        "--loop-id",
        LOOP,
        "--evidence",
        basis.relative_to(root).as_posix(),
    ]
    prepared = _payload(_cli(root, "loop", "review-repair-prepare", *common, "--json"))
    digest = prepared["prepare_digest"]
    assert (
        _cli(
            root,
            "loop",
            "review-repair-prepare",
            *common,
            "--read-path",
            basis.relative_to(root).as_posix(),
            "--json",
        ).returncode
        == 1
    )
    captured = _payload(
        _cli(
            root,
            "loop",
            "review-repair-prepare",
            *common,
            "--expect-digest",
            digest,
            "--read-path",
            basis.relative_to(root).as_posix(),
            "--json",
        )
    )
    assert captured["review_snapshot"]["content"] == basis.read_text()
    paths = []
    for index, role in enumerate(prepared["expert_roles"]):
        path = basis.parent / f"readiness-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "role": role,
                    "prepare_digest": digest,
                    "repair_readiness": {
                        "authorization": "PASS",
                        "facts": "PASS",
                        "verification": "PASS",
                        "evidence_refs": ["basis"],
                        "reason": "在原需求内有依据的文档修复",
                    },
                    "evidence": [
                        {
                            "id": "basis",
                            "path": basis.relative_to(root).as_posix(),
                            "sha256": prepared["evidence_manifest"][
                                basis.relative_to(root).as_posix()
                            ],
                            "locator": "全文",
                            "claim": "原范围修复及验证方法已明确",
                        }
                    ],
                }
            )
        )
        paths += ["--result", str(path)]
    _payload(
        _cli(
            root,
            "loop",
            "review-repair-record",
            *common,
            "--expect-digest",
            digest,
            *paths,
            "--json",
        )
    )
    assert (directory / "repair-readiness-supplement.json").is_file()
    unchanged = _payload(
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        )
    )
    assert unchanged["round_number"] == 1
    assert unchanged["review_status"] == "needs_fix"
    assert (
        _cli(
            root,
            "loop",
            "requirement",
            "freeze",
            "--loop-id",
            LOOP,
            "--expect-review-digest",
            reviewed["input_digest"],
            "--yes",
            "--json",
        ).returncode
        == 1
    )
    repaired = _payload(
        _cli(
            root,
            "loop",
            "requirement",
            "start",
            "--idea",
            "拒绝越权请求并保留实际审查",
            "--work-item-id",
            "stage-requirement",
            "--acceptance",
            "拒绝越权请求并给出明确错误，附实际材料",
            "--loop-id",
            LOOP,
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            CAPABILITY,
            "--json",
        )
    )
    assert repaired["status"] == "ready"
    second = _payload(
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        )
    )
    assert second["round_number"] == 2
    basis_relative = basis.relative_to(root).as_posix()
    assert basis_relative in second["evidence_manifest"]
    basis_snapshot = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "requirement",
            "--loop-id",
            LOOP,
            "--expect-digest",
            second["input_digest"],
            "--read-path",
            basis_relative,
            "--json",
        )
    )
    assert basis_snapshot["review_snapshot"]["content"] == basis.read_text()
    second_args = actual_result_args(root, "requirement", second)
    second_results = {
        filename: json.loads(Path(filename).read_text())
        for filename in second_args[1::2]
    }
    record_args = [
        "loop",
        "review-record",
        "--type",
        "requirement",
        "--loop-id",
        LOOP,
        "--expect-digest",
        second["input_digest"],
        *second_args,
        "--json",
    ]
    # 专家须引用原始授权材料；预测合同和补录裁决仍不能自证实际质量或准备度。
    for name in ("decision-context.json", "repair-readiness-supplement.json"):
        derived = (directory / name).relative_to(root).as_posix()
        for filename, result in second_results.items():
            assessment = result["assessment"]
            assessment["repair_readiness"]["evidence_refs"] = ["repair-basis"]
            assessment["evidence"] = assessment["evidence"][:1] + [
                {
                    "id": "repair-basis",
                    "path": derived,
                    "sha256": second["evidence_manifest"][derived],
                    "locator": "全文",
                    "claim": "修复依据",
                }
            ]
            Path(filename).write_text(json.dumps(result))
        rejected = _cli(root, *record_args)
        assert rejected.returncode == 1
        assert (
            json.loads(rejected.stdout)["detail"]
            == "decision-source-derived-state-forbidden"
        )
        assert not (directory / "review-outcome-round-2.json").exists()
    for filename, result in second_results.items():
        result["assessment"]["evidence"][-1].update(
            path=basis_relative,
            sha256=second["evidence_manifest"][basis_relative],
        )
        Path(filename).write_text(json.dumps(result))
    assert _payload(_cli(root, *record_args))["status"] == "passed"
    _payload(
        _cli(
            root,
            "loop",
            "requirement",
            "freeze",
            "--loop-id",
            LOOP,
            "--expect-review-digest",
            second["input_digest"],
            "--yes",
            "--json",
        )
    )
    (root / "later-business.py").write_text("VALUE = 1\n")
    closed = _payload(
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        )
    )
    assert closed["review_status"] == "passed"
    basis_bytes = basis.read_bytes()
    basis.write_text("擅自改变原授权材料")
    assert (
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        ).returncode
        == 1
    )
    basis.write_bytes(basis_bytes)
    supplement_path = directory / "repair-readiness-supplement.json"
    supplement_bytes = supplement_path.read_bytes()
    supplement_mode = supplement_path.stat().st_mode
    tampered = json.loads(supplement_bytes)
    tampered["recorded_at"] = "2000-01-01T00:00:00Z"
    supplement_path.write_text(json.dumps(tampered))
    assert (
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        ).returncode
        == 1
    )
    supplement_path.unlink()
    assert (
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        ).returncode
        == 1
    )
    supplement_path.write_bytes(supplement_bytes)
    supplement_path.chmod(supplement_mode)
    assert (
        _payload(
            _cli(
                root,
                "loop",
                "review",
                "--type",
                "requirement",
                "--loop-id",
                LOOP,
                "--json",
            )
        )["review_status"]
        == "passed"
    )
    for name, original in originals.items():
        assert (directory / name).read_bytes() == original
    assert not (directory / "review-outcome-round-3.json").exists()
