"""静态评分视角只提供模板，不声明尚未接入的阶段能力。"""

from __future__ import annotations

import json
from importlib.resources import files

import pytest


def _catalog() -> dict:
    resource = files("ai_sdlc.rules").joinpath("quantified-stage-profiles.json")
    assert resource.is_file(), "安装资源缺少分阶段评分模板"
    return json.loads(resource.read_text(encoding="utf-8"))


def test_packaged_catalog_advertises_explicit_stage_capabilities() -> None:
    catalog = _catalog()

    assert catalog["schema_version"] == "quantified-stage-profiles-v1"
    assert catalog["supported_capabilities"] == {
        "requirement": ["stage-simulation-v1"],
        "design-contract": ["stage-simulation-v1"],
        "implementation": ["implementation-simulation-v1", "stage-simulation-v1"],
        "frontend-evidence": ["stage-simulation-v1"],
        "local-pr-review": ["stage-simulation-v1"],
    }
    assert catalog["profile_bundles"] == {
        "requirement": ["requirement-analysis-v1"],
        "design-contract": ["design-contract-v1"],
        "implementation": ["implementation-plan-v1", "code-result-v1"],
        "frontend-evidence": ["frontend-evidence-v1"],
        "local-pr-review": ["delivery-readiness-v1"],
    }
    assert {
        key
        for key, value in catalog["profiles"].items()
        if value["availability"] == "supported"
    } == set(catalog["profiles"])


def test_catalog_defaults_keep_finite_comparison_and_goal_target() -> None:
    defaults = _catalog()["defaults"]

    assert defaults["goal_weighting"] == "equal"
    assert defaults["target_level"] == 3
    assert set(defaults["anchors"]) == {"0", "1", "2", "3", "4"}
    assert defaults["simulation_policy"] == {
        "max_batches": 2,
        "max_candidates": 3,
        "max_judges": 1,
    }
    assert defaults["max_criteria_per_profile"] == 16
    assert defaults["max_candidate_sketch_bytes"] == 65536
    assert defaults["assessment_origin"] == "forecast"


def test_new_router_commands_follow_supported_catalog_without_upgrading_old_ids() -> (
    None
):
    from ai_sdlc.core.loop_router import _new_start_command
    from ai_sdlc.rules import supported_decision_capabilities

    supported = supported_decision_capabilities()
    assert supported["implementation"] == [
        "implementation-b1",
        "implementation-simulation-v1",
        "stage-simulation-v1",
    ]
    for stage, capabilities in supported.items():
        command = _new_start_command(stage)
        assert f"--decision-capability {capabilities[-1]}" in command
        assert "--decision-mode adaptive-quantified" in command
        assert "--loop-id" not in command


@pytest.mark.parametrize(
    ("profile_id", "loop_type"),
    [
        ("requirement-analysis-v1", "requirement"),
        ("design-contract-v1", "design-contract"),
        ("implementation-plan-v1", "implementation"),
        ("code-result-v1", "implementation"),
        ("frontend-evidence-v1", "frontend-evidence"),
        ("delivery-readiness-v1", "local-pr-review"),
    ],
)
def test_each_profile_has_task_instantiated_counting_guidance(
    profile_id: str, loop_type: str
) -> None:
    catalog = _catalog()
    assert len(catalog["profiles"]) == 6
    profile = catalog["profiles"][profile_id]

    assert profile["profile_version"] == "1"
    assert profile["loop_type"] == loop_type
    assert profile["view"]
    assert profile["downstream_boundary"]
    assert profile["availability"] in {"supported", "template-only"}
    criteria = profile["criterion_templates"]
    assert 3 <= len(criteria) <= 6
    assert len({item["id"] for item in criteria}) == len(criteria)
    for criterion in criteria:
        assert criterion["statement"]
        assert criterion["counted_items"]
        assert criterion["target_anchor"]
        assert criterion["count_basis"] in {
            "frozen-contract-set",
            "frozen-candidate-inventory",
        }
        assert criterion["kind"] in {"coverage", "unknown-count", "conflict-count"}


def test_catalog_has_no_executable_formula_or_vendor_requirement() -> None:
    catalog = _catalog()
    encoded = json.dumps(catalog, ensure_ascii=False).lower()

    for forbidden in (
        '"formula"',
        '"expression"',
        '"command"',
        '"provider_id"',
        "openai",
        "anthropic",
    ):
        assert forbidden not in encoded
    assert catalog["profiles"]["delivery-readiness-v1"]["execution_policy"] == (
        "evaluate-current-staged-tree-only"
    )
