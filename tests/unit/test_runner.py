"""M0-A4a AgentRunner 协议 + MockAgentRunner 测试。

无网络/时钟/DB/文件；async 经 asyncio.run 驱动；确定性=纯 handler。
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aotf.runner import AgentRunRequest, AgentRunResult, MockAgentRunner

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
DIGEST = "a" * 64


def _req(**kw):
    base = dict(run_id="run-1", task_id="task-min", role="planner",
                requested_model="opus", input_digest=DIGEST, max_turns=20,
                max_budget_usd=Decimal("20.00"),
                deadline_at=T0 + timedelta(hours=1))
    base.update(kw)
    return AgentRunRequest(**base)


def _result(**kw):
    base = dict(run_id="run-1", status="completed", resolved_model="claude-opus-5",
                input_tokens=100, output_tokens=50,
                estimated_cost_usd=Decimal("0.001000"),
                output_artifact_id=None, error_code=None)
    base.update(kw)
    return AgentRunResult(**base)


def _run(mock, req):
    return asyncio.run(mock.run(req))


def test_request_frozen_and_valid() -> None:
    req = _req()
    assert req.run_id == "run-1"
    assert req.role == "planner"
    assert req.max_budget_usd == Decimal("20.00")
    with pytest.raises(FrozenInstanceError):
        req.max_turns = 5  # type: ignore[misc]


def test_result_frozen_and_valid() -> None:
    res = _result()
    assert res.status == "completed"
    assert res.estimated_cost_usd == Decimal("0.001000")
    with pytest.raises(FrozenInstanceError):
        res.input_tokens = 1  # type: ignore[misc]


def test_mock_returns_handler_result_completed() -> None:
    mock = MockAgentRunner(lambda r: _result())
    out = _run(mock, _req())
    assert out == _result()


def test_mock_returns_failed_result() -> None:
    mock = MockAgentRunner(lambda r: _result(status="failed", error_code="agent_error"))
    out = _run(mock, _req())
    assert out.status == "failed"
    assert out.error_code == "agent_error"


def test_mock_supports_async_handler() -> None:
    async def handler(r):
        return _result(status="budget_exceeded", error_code="BUDGET_EXCEEDED")
    mock = MockAgentRunner(handler)
    out = _run(mock, _req())
    assert out.status == "budget_exceeded"


def test_mock_deterministic_same_input_same_output() -> None:
    mock = MockAgentRunner(lambda r: _result(input_tokens=42))
    assert _run(mock, _req()) == _run(mock, _req())


def test_mock_rejects_mismatched_run_id() -> None:
    mock = MockAgentRunner(lambda r: _result(run_id="run-other"))
    with pytest.raises(ValueError):
        _run(mock, _req())


@pytest.mark.parametrize("kwargs", [
    {"role": "scientist"},
    {"deadline_at": datetime(2026, 9, 1)},
    {"max_turns": 0},
])
def test_request_rejects_invalid_fields(kwargs) -> None:
    with pytest.raises(ValueError):
        _req(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"status": "succeeded"},
    {"input_tokens": -1},
])
def test_result_rejects_invalid_status_or_tokens(kwargs) -> None:
    with pytest.raises(ValueError):
        _result(**kwargs)
