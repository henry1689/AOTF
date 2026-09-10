"""AOTF orchestrator 编排基元 (A4b).

把一次 MockAgentRunner 运行收束为可审计、可判定、可推进的机器语义：
record_run 映射 request+result 为 AgentRunRecord 落库（含 resolved model，
§20.5 #29）；normalize_status 做 budget/deadline 越限裁决（deadline 先于
budget，§20.5 #25，越限不推进）；advance_target 为脚本化角色推进策略
（completed planner/imlementer/reviewer 各自到固定目标 phase，其余不推进，
§20.5 #27）。本模块只提供基元，不做循环/全流程驱动（A4c drill 组合）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from aotf import store
from aotf.models import AgentRunRecord, TaskPhase
from aotf.runner import AgentRunRequest, AgentRunResult

_ROLE_TARGETS = {
    "planner": TaskPhase.PLAN_READY,
    "implementer": TaskPhase.DELTA_CAPTURED,
    "reviewer": TaskPhase.POLICY_EVALUATING,
}


def record_run(
    conn: sqlite3.Connection,
    *,
    request: AgentRunRequest,
    result: AgentRunResult,
    started_at: datetime,
    ended_at: datetime,
) -> AgentRunRecord:
    if result.run_id != request.run_id:
        raise ValueError("record run_id mismatch")
    record = AgentRunRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        role=request.role,
        session_id=None,
        requested_model=request.requested_model,
        resolved_model=result.resolved_model,
        status=result.status,
        input_digest=request.input_digest,
        output_artifact_id=result.output_artifact_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        started_at=started_at,
        ended_at=ended_at,
    )
    store.insert_agent_run(conn, record)
    return record


def normalize_status(
    request: AgentRunRequest,
    result: AgentRunResult,
    ended_at: datetime,
) -> str:
    if result.status != "completed":
        return result.status
    if ended_at > request.deadline_at:
        return "timeout"
    if result.estimated_cost_usd > request.max_budget_usd:
        return "budget_exceeded"
    return "completed"


def advance_target(role: str, status: str) -> TaskPhase | None:
    if status != "completed":
        return None
    return _ROLE_TARGETS.get(role)
