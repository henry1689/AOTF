"""AOTF AgentRunner 执行接口基座 (A4a).

spec §7.2 参考形状的落地：AgentRunRequest / AgentRunResult frozen 值对象、
AgentRunner Protocol（async run）、MockAgentRunner（确定性 handler 注入 mock，
无网络/无真实 SDK/无 DB/无副作用——§19 M0-A「无需任何真实 Agent」的工具）。
budget/deadline 裁决与 agent_runs 落库归 A4b；真实 Agent/SDK 属 M0-D。
本模块不导入任何 aotf 内部模块（无环、无 DB 依赖）；值对象校验失败抛
ValueError（运行期契约，非领域错误）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

_ROLES = ("planner", "implementer", "reviewer", "adversary")
_STATUSES = ("completed", "failed", "timeout", "budget_exceeded", "cancelled")


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _token(value: object, name: str) -> None:
    _check(type(value) is str and bool(value.strip()), f"{name} required")


def _hex64(value: object, name: str) -> None:
    _check(type(value) is str and len(value) == 64
           and all(c in "0123456789abcdef" for c in value),
           f"{name} must be sha256 hex")


def _utc(value: object, name: str) -> None:
    _check(type(value) is datetime and value.tzinfo is not None
           and value.utcoffset() == timedelta(0),
           f"{name} must be aware UTC datetime")


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    run_id: str
    task_id: str
    role: Literal["planner", "implementer", "reviewer", "adversary"]
    requested_model: str
    input_digest: str
    max_turns: int
    max_budget_usd: Decimal
    deadline_at: datetime

    def __post_init__(self) -> None:
        _token(self.run_id, "run_id")
        _token(self.task_id, "task_id")
        _check(self.role in _ROLES, "invalid role")
        _token(self.requested_model, "requested_model")
        _hex64(self.input_digest, "input_digest")
        _check(type(self.max_turns) is int and self.max_turns >= 1,
               "max_turns must be positive int")
        _check(type(self.max_budget_usd) is Decimal and self.max_budget_usd >= 0,
               "max_budget_usd must be non-negative Decimal")
        _utc(self.deadline_at, "deadline_at")


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    run_id: str
    status: Literal["completed", "failed", "timeout", "budget_exceeded",
                    "cancelled"]
    resolved_model: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    output_artifact_id: str | None
    error_code: str | None

    def __post_init__(self) -> None:
        _token(self.run_id, "run_id")
        _check(self.status in _STATUSES, "invalid status")
        _check(self.resolved_model is None or type(self.resolved_model) is str,
               "resolved_model invalid")
        _check(type(self.input_tokens) is int and self.input_tokens >= 0,
               "input_tokens must be non-negative int")
        _check(type(self.output_tokens) is int and self.output_tokens >= 0,
               "output_tokens must be non-negative int")
        _check(type(self.estimated_cost_usd) is Decimal
               and self.estimated_cost_usd >= 0,
               "estimated_cost_usd must be non-negative Decimal")
        _check(self.output_artifact_id is None or type(self.output_artifact_id) is str,
               "output_artifact_id invalid")
        _check(self.error_code is None or type(self.error_code) is str,
               "error_code invalid")


class AgentRunner(Protocol):
    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        ...


class MockAgentRunner:
    """确定性 mock：结果完全由注入 handler 决定，无任何副作用。"""

    def __init__(self, handler: Callable[[AgentRunRequest], AgentRunResult]) -> None:
        self._handler = handler

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        result = self._handler(request)
        if hasattr(result, "__await__"):
            result = await result
        if result.run_id != request.run_id:
            raise ValueError("mock result run_id mismatch")
        return result
