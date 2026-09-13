"""Local PR 的合同来源和已保存身份不能降级为自证结果。"""

from __future__ import annotations

import hashlib

import pytest

from ai_sdlc.core.loop_decision_models import DecisionSource
from ai_sdlc.core.pr_review_decision import _validate_sources
from ai_sdlc.core.pr_review_models import ReviewRun


@pytest.mark.parametrize(
    "name",
    [
        "decision-context.json",
        "review-run.json",
        "final-report.md",
        "review-outcome-round-1.json",
        "review-outcome-round-2.json",
    ],
)
@pytest.mark.parametrize("review_id", ["current", "other"])
def test_any_pr_derived_state_cannot_be_current_actual_source(
    tmp_path, name, review_id
):
    source = DecisionSource(
        id="bad",
        path=f".ai-sdlc/reviews/pr/{review_id}/{name}",
        sha256="a" * 64,
        locator="score",
        claim="自报通过",
    )
    with pytest.raises(ValueError, match="derived-state-forbidden"):
        _validate_sources(
            tmp_path,
            ReviewRun(review_id="current", loop_id="loop-current"),
            [source],
            check_digest=True,
        )


def test_business_file_with_same_basename_remains_valid(tmp_path):
    path = tmp_path / "decision-context.json"
    path.write_text('{"business": "state"}\n', encoding="utf-8")
    source = DecisionSource(
        id="business",
        path=path.name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        locator="business",
        claim="业务原件",
    )
    _validate_sources(
        tmp_path,
        ReviewRun(review_id="current", loop_id="loop-current"),
        [source],
        check_digest=True,
    )
