"""M0-D1 FakeClaudeSDK（SDK 门 enforce）测试。

确定性 handler 注入；async run 用 asyncio.run 包装（项目无 pytest-asyncio）。
tmp_path 目录充当执行 cwd。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from aotf.agents.fake import FakeClaudeSDK
from aotf.agents.schema import (
    SDKReason,
    SDKRunOutcome,
    SDKToolAttempt,
    SDKUsage,
    SdkRunContext,
    SdkRunner,
)


def _ctx(tmp_path, **over) -> SdkRunContext:
    base = dict(cwd=str(tmp_path), max_turns=5)
    base.update(over)
    return SdkRunContext(**base)


def _run(sdk: FakeClaudeSDK, ctx: SdkRunContext) -> SDKRunOutcome:
    return asyncio.run(sdk.run(ctx))


def _completed(**over) -> SDKRunOutcome:
    base = dict(reason=SDKReason.COMPLETED, turns=1)
    base.update(over)
    return SDKRunOutcome(**base)


def test_fake_completed_passthrough(tmp_path) -> None:
    out = _completed(
        turns=2, session_id="s1", usage=SDKUsage(10, 20),
        attempts=(SDKToolAttempt(tool_name="Edit", path="C:/a.py",
                                 allowed=True, reason="ok"),))
    got = _run(FakeClaudeSDK(lambda ctx: out), _ctx(tmp_path))
    assert got == out
    assert got.reason == SDKReason.COMPLETED


def test_fake_turns_exceed_timeout(tmp_path) -> None:
    ctx = _ctx(tmp_path, max_turns=2)
    got = _run(FakeClaudeSDK(lambda c: _completed(turns=3)), ctx)
    assert got.reason == SDKReason.TIMEOUT
    assert got.turns == 2


def test_fake_budget_exceeded(tmp_path) -> None:
    ctx = _ctx(tmp_path, max_budget_usd=Decimal("0.5"))
    got = _run(FakeClaudeSDK(lambda c: _completed(
        turns=1, estimated_cost_usd=Decimal("0.9"))), ctx)
    assert got.reason == SDKReason.BUDGET_EXCEEDED
    assert got.turns == 1  # budget 覆写不改 turns


def test_fake_turns_checked_before_budget(tmp_path) -> None:
    ctx = _ctx(tmp_path, max_turns=2, max_budget_usd=Decimal("0.5"))
    got = _run(FakeClaudeSDK(lambda c: _completed(
        turns=3, estimated_cost_usd=Decimal("0.9"))), ctx)
    assert got.reason == SDKReason.TIMEOUT  # 先 turns 后 budget


def test_fake_zero_budget_no_limit(tmp_path) -> None:
    got = _run(FakeClaudeSDK(lambda c: _completed(
        turns=1, estimated_cost_usd=Decimal("100"))), _ctx(tmp_path))
    assert got.reason == SDKReason.COMPLETED  # 0 = 未设预算上限


def test_fake_non_completed_reason_kept(tmp_path) -> None:
    ctx = _ctx(tmp_path, max_turns=2)
    sdk = FakeClaudeSDK(lambda c: SDKRunOutcome(
        reason=SDKReason.ERROR, turns=9, estimated_cost_usd=Decimal("99")))
    got = _run(sdk, ctx)
    assert got.reason == SDKReason.ERROR  # 非 completed 不覆写
    assert got.turns == 9


def test_fake_handler_raises_error_outcome(tmp_path) -> None:
    def boom(_ctx: SdkRunContext) -> SDKRunOutcome:
        raise RuntimeError("boom")

    got = _run(FakeClaudeSDK(boom), _ctx(tmp_path))
    assert got.reason == SDKReason.ERROR
    assert got.error_code == "handler_error"


def test_fake_missing_cwd_valueerror(tmp_path) -> None:
    sdk = FakeClaudeSDK(lambda c: SDKRunOutcome())
    ctx = SdkRunContext(cwd=str(tmp_path / "nope"), max_turns=5)
    with pytest.raises(ValueError, match="fake cwd not a directory"):
        _run(sdk, ctx)


def test_fake_attempts_passthrough(tmp_path) -> None:
    denied = SDKToolAttempt(tool_name="Edit", path="C:/secret.py",
                            allowed=False, reason="permission denied")
    got = _run(FakeClaudeSDK(lambda c: _completed(
        turns=1, attempts=(denied,))), _ctx(tmp_path))
    assert got.attempts == (denied,)
    assert got.attempts[0].allowed is False


def test_fake_sdk_runner_protocol() -> None:
    sdk = FakeClaudeSDK(lambda c: SDKRunOutcome())
    assert isinstance(sdk, SdkRunner)  # 可替换性 §7.1 类型保证


def test_fake_async_run_deterministic(tmp_path) -> None:
    out = _completed(turns=2, session_id="s9", usage=SDKUsage(1, 2))
    sdk = FakeClaudeSDK(lambda c: out)
    ctx = _ctx(tmp_path)
    assert _run(sdk, ctx) == _run(sdk, ctx)


def test_fake_session_usage_passthrough(tmp_path) -> None:
    got = _run(FakeClaudeSDK(lambda c: _completed(
        turns=1, session_id="sess-42", usage=SDKUsage(3, 4))), _ctx(tmp_path))
    assert got.session_id == "sess-42"
    assert got.usage == SDKUsage(3, 4)
