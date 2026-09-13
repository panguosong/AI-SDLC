"""全阶段身份必须显式绑定，旧记录不能因新能力而自动迁移。"""

import pytest
from pydantic import ValidationError

from ai_sdlc.core.loop_models import LoopRun, LoopType


@pytest.mark.parametrize("stage", list(LoopType))
def test_new_stage_capability_is_bound_to_each_loop(stage):
    run = LoopRun(
        loop_id="stage-identity",
        loop_type=stage,
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    assert run.decision_capability == "stage-simulation-v1"
    assert LoopRun.model_validate_json(run.model_dump_json()) == run


@pytest.mark.parametrize("stage", list(LoopType))
def test_legacy_stage_serialization_stays_unchanged(stage):
    run = LoopRun(loop_id="old-loop", loop_type=stage)
    assert "decision_mode" not in run.model_dump()
    assert "decision_capability" not in run.model_dump()


@pytest.mark.parametrize(
    "mode, capability",
    [("legacy", "stage-simulation-v1"), ("adaptive-quantified", None)],
)
def test_mixed_stage_identity_is_rejected(mode, capability):
    with pytest.raises(ValidationError):
        LoopRun(
            loop_id="bad-identity",
            loop_type="requirement",
            decision_mode=mode,
            decision_capability=capability,
        )
