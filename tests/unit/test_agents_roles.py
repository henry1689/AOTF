"""M0-D3 三角色 adapter roles 测试（prompt 约束 / 输入清单校验 / render）。

#28 结构保证（ReviewerInputs 无 summary 字段）与 prompt 侧约束均有断言。
"""

from __future__ import annotations

import pytest

from aotf.agents.roles import (
    ROLE_IDS,
    ROLE_SYSTEM_PROMPTS,
    ImplementerInputs,
    PlannerInputs,
    ReviewerInputs,
    render_role_inputs,
    role_system_prompt,
)

H40 = "a" * 40


def _planner(**over) -> PlannerInputs:
    base = dict(project_context="ctx", baseline_commit=H40, baseline_tree=H40)
    base.update(over)
    return PlannerInputs(**base)


def _implementer(**over) -> ImplementerInputs:
    base = dict(task_id="t1", proposal_text="p", allowed_files=("a.py",),
                worktree_path="C:/wt", baseline_tree=H40)
    base.update(over)
    return ImplementerInputs(**base)


def _reviewer(**over) -> ReviewerInputs:
    base = dict(task_id="t1", proposal_text="p", allowed_files=("a.py",),
                baseline_tree=H40, actual_tree=H40, changes_summary="delta",
                patch_text="diff --git a/a.py b/a.py")
    base.update(over)
    return ReviewerInputs(**base)


def test_role_ids() -> None:
    assert ROLE_IDS == ("planner", "implementer", "reviewer")
    assert set(ROLE_SYSTEM_PROMPTS) == set(ROLE_IDS)


def test_planner_prompt_constraints() -> None:
    p = role_system_prompt("planner")
    assert "只读" in p
    assert "proposal" in p
    assert "止血" in p  # P0/P1 不得等重构


def test_implementer_prompt_constraints() -> None:
    p = role_system_prompt("implementer")
    assert "批准文件快照" in p
    assert "不拥有文件写入" in p
    assert "AOTF controller" in p
    assert "escalated" in p
    assert "批准" in p


def test_reviewer_prompt_no_subjective_summary() -> None:
    # #28 prompt 侧：不读 Implementer 主观总结
    p = role_system_prompt("reviewer")
    assert "主观总结" in p
    assert "不得" in p and "读取" in p
    for dim in ("correctness", "structure", "tests", "compatibility", "risk"):
        assert dim in p  # 五维


def test_unknown_role_rejected() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        role_system_prompt("adversary")
    with pytest.raises(ValueError, match="unknown role"):
        render_role_inputs("adversary", _planner())


def test_planner_inputs_validated() -> None:
    with pytest.raises(ValueError, match="baseline_commit"):
        _planner(baseline_commit="x" * 40)
    with pytest.raises(ValueError, match="constraints entry"):
        _planner(constraints=("",))
    with pytest.raises(ValueError, match="project_context"):
        _planner(project_context="  ")


def test_implementer_inputs_validated() -> None:
    with pytest.raises(ValueError, match="task_id"):
        _implementer(task_id="a b")  # 非 token（含空白）
    with pytest.raises(ValueError, match="worktree_path"):
        _implementer(worktree_path="wt")
    with pytest.raises(ValueError, match="allowed_files entry"):
        _implementer(allowed_files=("a/../b",))
    with pytest.raises(ValueError, match="proposal_text"):
        _implementer(proposal_text="")


def test_reviewer_inputs_no_summary_field() -> None:
    # #28 结构侧：ReviewerInputs 无 implementer summary 字段
    r = _reviewer()
    assert not hasattr(r, "summary")
    assert r.changes_summary == "delta"
    assert "diff --git" in r.patch_text
    with pytest.raises(ValueError, match="actual_tree"):
        _reviewer(actual_tree="z" * 40)


def test_render_inputs_roles() -> None:
    text_p = render_role_inputs("planner", _planner())
    assert "任务输入" in text_p and H40 in text_p
    text_i = render_role_inputs("implementer", _implementer())
    assert "C:/wt" in text_i and "a.py" in text_i
    text_r = render_role_inputs("reviewer", _reviewer())
    assert "delta" in text_r and "actual_tree" in text_r
    with pytest.raises(ValueError, match="requires PlannerInputs"):
        render_role_inputs("planner", _implementer())
