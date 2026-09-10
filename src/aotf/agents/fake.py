"""AOTF FakeClaudeSDK（M0-D1）：确定性 SDK 门。

实现 SdkRunner 面（schema.SdkRunner），镜像 spec §7.1 的 SDK 能力面
（max_turns / max_budget_usd / permission_mode=dontAsk 记录 / can_use_tool
判定记录），供 D2 真实 adapter（claude_sdk.py）同面替换（§7.1 可替换性）。

确定性：结果由注入 handler 全权决定 + SDK 门 enforce（模拟真实 SDK 终止
语义，确定性次序）：
  1. 前置：ctx.cwd 须存在且为目录（执行面畸形 → ValueError，调用方 bug）；
  2. handler(ctx) 产 desired outcome；handler 抛异常或返回非 SDKRunOutcome
     → 返回 reason=ERROR、error_code="handler_error" 的 outcome（不冒泡）；
  3. SDK 门 enforce（仅 completed 可被覆写；非 completed reason 保持）：
     - turns > ctx.max_turns             → 覆写 reason=TIMEOUT、turns=max_turns；
     - 否则 ctx.max_budget_usd>0 且 cost>上限 → 覆写 reason=BUDGET_EXCEEDED
       （turns 不改）。先 turns 后 budget，确定性。
  4. session_id/usage/attempts/error_code 透传。

零网络/零真实 SDK/零副作用；与真实 SDK 差异（真实 SDK 自行推进、真实 usage/
cost 换算）由 D2 adapter 校订，本模块只保 SDK 门语义与记录事实。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .schema import (
    SDKReason,
    SDKRunOutcome,
    SdkRunContext,
    SdkRunner,
)

__all__ = ["FakeClaudeSDK"]


class FakeClaudeSDK:
    """确定性 SDK 门（实现 SdkRunner 面；结果由注入 handler 决定）。"""

    def __init__(
        self,
        handler: Callable[[SdkRunContext], SDKRunOutcome],
    ) -> None:
        if not callable(handler):
            raise ValueError("handler must be callable")
        self._handler = handler

    async def run(self, ctx: SdkRunContext) -> SDKRunOutcome:
        if not Path(ctx.cwd).is_dir():
            raise ValueError("fake cwd not a directory")
        try:
            desired = self._handler(ctx)
            if not isinstance(desired, SDKRunOutcome):
                raise TypeError("handler must return SDKRunOutcome")
        except Exception:
            return SDKRunOutcome(reason=SDKReason.ERROR,
                                 error_code="handler_error")
        # SDK 门 enforce（仅 completed 可被覆写；先 turns 后 budget）
        if desired.reason == SDKReason.COMPLETED:
            if desired.turns > ctx.max_turns:
                return replace(desired, reason=SDKReason.TIMEOUT,
                               turns=ctx.max_turns)
            if ctx.max_budget_usd > 0 \
                    and desired.estimated_cost_usd > ctx.max_budget_usd:
                return replace(desired, reason=SDKReason.BUDGET_EXCEEDED)
        return desired
