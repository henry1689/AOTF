"""M0-D1 agents SDK 执行面 schema 校验测试。

纯值对象 fail-closed（ValueError）；无 git/DB/时间/async 依赖。
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from aotf.agents.schema import (
    SDKReason,
    SDKRunOutcome,
    SDKToolAttempt,
    SDKUsage,
    SdkRunContext,
)

CWD = "C:/worktree/task-1"


def _ctx(**over) -> SdkRunContext:
    base = dict(cwd=CWD, max_turns=5)
    base.update(over)
    return SdkRunContext(**base)


def test_sdk_context_defaults_ok() -> None:
    ctx = SdkRunContext(cwd=CWD)
    assert ctx.requested_model == "opus"
    assert ctx.max_turns == 1
    assert ctx.max_budget_usd == Decimal("0")
    assert ctx.permission_mode == "default"
    assert ctx.visible_tools == ()


def test_cwd_rejects_relative_and_parent() -> None:
    for bad in ("wt", "relative/dir", "C:/a/../b", ""):
        with pytest.raises(ValueError, match="invalid cwd"):
            _ctx(cwd=bad)


def test_cwd_windows_root_no_drive_rejected() -> None:
    # '\\x' 始终是无盘符 Windows 根化路径；'/x' 在 POSIX 是合法绝对路径，
    # 只在 Windows 作为无盘符根化路径拒绝。
    bad_paths = ["\\x"]
    if os.name == "nt":
        bad_paths.append("/x")
    for bad in bad_paths:
        with pytest.raises(ValueError, match="invalid cwd"):
            _ctx(cwd=bad)


def test_token_fields_validated() -> None:
    with pytest.raises(ValueError, match="requested_model"):
        _ctx(requested_model="a b")
    with pytest.raises(ValueError, match="requested_model"):
        _ctx(requested_model="..")
    with pytest.raises(ValueError, match="visible_tools"):
        _ctx(visible_tools=("..",))
    with pytest.raises(ValueError, match="allowed_rules"):
        _ctx(allowed_rules=("",))
    with pytest.raises(ValueError, match="denied_rules"):
        _ctx(denied_rules=("x" * 65,))
    with pytest.raises(ValueError, match="visible_tools must be a tuple"):
        _ctx(visible_tools=["Edit"])


def test_allowed_denied_overlap_rejected() -> None:
    with pytest.raises(ValueError, match="allowed and denied overlap"):
        _ctx(allowed_rules=("Edit",), denied_rules=("Edit",))
    _ctx(allowed_rules=("Edit",), denied_rules=("Write",))  # 不重叠放行


def test_max_turns_positive_int() -> None:
    for bad in (0, -1, True, "5"):
        with pytest.raises(ValueError, match="max_turns"):
            _ctx(max_turns=bad)


def test_max_budget_decimal_nonneg() -> None:
    with pytest.raises(ValueError, match="max_budget_usd"):
        _ctx(max_budget_usd=Decimal("-0.1"))
    with pytest.raises(ValueError, match="max_budget_usd"):
        _ctx(max_budget_usd=True)
    with pytest.raises(ValueError, match="max_budget_usd"):
        _ctx(max_budget_usd="1")
    assert _ctx(max_budget_usd=Decimal("1.5")).max_budget_usd == Decimal("1.5")


def test_permission_mode_enum() -> None:
    with pytest.raises(ValueError, match="permission_mode"):
        _ctx(permission_mode="ask")
    assert _ctx(permission_mode="dontAsk").permission_mode == "dontAsk"


def test_tool_attempt_validated() -> None:
    with pytest.raises(ValueError, match="tool_name"):
        SDKToolAttempt(tool_name="a b")
    with pytest.raises(ValueError, match="path"):
        SDKToolAttempt(tool_name="Edit", path="")
    with pytest.raises(ValueError, match="allowed must be bool"):
        SDKToolAttempt(tool_name="Edit", allowed=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reason"):
        SDKToolAttempt(tool_name="Edit", reason="")
    SDKToolAttempt(tool_name="Edit", path="C:/a.py", allowed=False,
                   reason="permission denied")


def test_usage_validated() -> None:
    with pytest.raises(ValueError, match="input_tokens"):
        SDKUsage(input_tokens=-1)
    with pytest.raises(ValueError, match="output_tokens"):
        SDKUsage(output_tokens=True)  # type: ignore[arg-type]


def test_outcome_validated() -> None:
    with pytest.raises(ValueError, match="turns"):
        SDKRunOutcome(turns=-1)
    with pytest.raises(ValueError, match="turns"):
        SDKRunOutcome(turns=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reason"):
        SDKRunOutcome(reason="completed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage"):
        SDKRunOutcome(usage=SDKUsage)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="attempts"):
        SDKRunOutcome(attempts=(1,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="estimated_cost_usd"):
        SDKRunOutcome(estimated_cost_usd=Decimal("-1"))
    with pytest.raises(ValueError, match="error_code"):
        SDKRunOutcome(error_code="")
    with pytest.raises(ValueError, match="result_text"):
        SDKRunOutcome(result_text="")
    with pytest.raises(ValueError, match="structured_output"):
        SDKRunOutcome(structured_output={"bad": float("nan")})
    with pytest.raises(ValueError, match="resolved_model"):
        SDKRunOutcome(resolved_model="")


def test_outcome_usage_defaults() -> None:
    out = SDKRunOutcome()
    assert out.reason == SDKReason.COMPLETED
    assert out.usage == SDKUsage()
    assert out.turns == 0
    assert out.attempts == ()
    assert out.result_text is None
    assert out.structured_output is None
    assert out.resolved_model is None
