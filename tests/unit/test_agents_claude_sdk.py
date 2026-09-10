"""M0-D2 ClaudeSdkRunner + A4a mapper 测试。

探针校订（SDK 0.2.152）：真实 ResultMessage 以 is_error/model_usage/cost 表
达终止，无独立「成功超 turns」态 → taskbook 13/14 名校订为
result_error_mapped / cost_over_budget_mapped（超 turns 归 SDK error，
deadline 超时归 A4b normalize_status）。runner 测试注入 query_fn 零真实
请求；ResultMessage/ClaudeAgentOptions 用已装 SDK 真实构造。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from aotf.agents.claude_sdk import (
    ALIAS_MODELS,
    ClaudeSdkRunner,
    resolve_model,
    resolved_model_from_result,
    to_agent_run_result,
)
from aotf.agents.schema import (
    SDKReason,
    SDKRunOutcome,
    SDKUsage,
    SdkRunContext,
    SdkRunner,
)
from aotf.runner import AgentRunRequest

import claude_agent_sdk as sdk

H64 = "d" * 64


def _req(run_id: str = "r1") -> AgentRunRequest:
    return AgentRunRequest(
        run_id=run_id, task_id="t1", role="implementer",
        requested_model="opus", input_digest=H64, max_turns=5,
        max_budget_usd=Decimal("1.0"),
        deadline_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _outcome(**over) -> SDKRunOutcome:
    base = dict(reason=SDKReason.COMPLETED, turns=2,
                usage=SDKUsage(10, 20),
                estimated_cost_usd=Decimal("0.01"))
    base.update(over)
    return SDKRunOutcome(**base)


def _ctx(tmp_path, **over) -> SdkRunContext:
    base = dict(cwd=str(tmp_path), max_turns=5)
    base.update(over)
    return SdkRunContext(**base)


def _arun(runner: ClaudeSdkRunner, ctx: SdkRunContext) -> SDKRunOutcome:
    return asyncio.run(runner.run(ctx))


def _result(is_error: bool = False, *, session_id: str = "s1",
            num_turns: int = 2, total_cost_usd: float | None = 0.01,
            model_usage: dict | None = None, errors: list | None = None,
            terminal_reason: str | None = None, result: str = "ok",
            structured_output: object | None = None):
    return sdk.ResultMessage(
        subtype="result", duration_ms=10, duration_api_ms=10,
        is_error=is_error, num_turns=num_turns, session_id=session_id,
        stop_reason=None, total_cost_usd=total_cost_usd, result=result,
        structured_output=structured_output,
        model_usage=model_usage, errors=errors, terminal_reason=terminal_reason,
    )


_MU = {"claude-opus-test-1": {"inputTokens": 10, "outputTokens": 20,
                              "costUSD": 0.01, "canonicalModel": "claude-opus-test-1"}}


def test_mapper_completed() -> None:
    res = to_agent_run_result(_req(), _outcome(), resolved_model="opus")
    assert res.status == "completed"
    assert res.resolved_model == "opus"
    assert res.input_tokens == 10 and res.output_tokens == 20
    assert res.estimated_cost_usd == Decimal("0.01")
    assert res.run_id == "r1"


def test_mapper_timeout() -> None:
    res = to_agent_run_result(_req(), _outcome(reason=SDKReason.TIMEOUT),
                              resolved_model=None)
    assert res.status == "timeout"


def test_mapper_budget_exceeded() -> None:
    res = to_agent_run_result(_req(), _outcome(reason=SDKReason.BUDGET_EXCEEDED),
                              resolved_model=None)
    assert res.status == "budget_exceeded"


def test_mapper_error_failed() -> None:
    res = to_agent_run_result(
        _req(), _outcome(reason=SDKReason.ERROR, error_code="boom"),
        resolved_model=None)
    assert res.status == "failed"
    assert res.error_code == "boom"


def test_mapper_cancelled() -> None:
    res = to_agent_run_result(_req(), _outcome(reason=SDKReason.CANCELLED),
                              resolved_model=None)
    assert res.status == "cancelled"


def test_mapper_run_id_bound_to_request() -> None:
    # mapper 以 request.run_id 绑定产物（与 record_run 语义一致）；A4a 校验兜底
    res = to_agent_run_result(_req("r9"), _outcome(), resolved_model=None)
    assert res.run_id == "r9"


def test_mapper_output_artifact_id() -> None:
    res = to_agent_run_result(_req(), _outcome(), resolved_model=None,
                              output_artifact_id="art-1")
    assert res.output_artifact_id == "art-1"


def test_alias_resolution() -> None:
    assert {"opus", "sonnet", "haiku"} <= set(ALIAS_MODELS)
    for alias, model in ALIAS_MODELS.items():
        assert model and model != alias or model == alias  # 值非空
        assert resolve_model(alias) == model


def test_alias_unknown_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model alias"):
        resolve_model("gpt-x")


def test_runner_injects_context_options(tmp_path) -> None:
    captured: list = []

    async def q(prompt: str, options: object):
        captured.append((prompt, options))
        yield _result()

    ctx = _ctx(tmp_path, max_budget_usd=Decimal("0.5"),
               permission_mode="dontAsk", visible_tools=("Read",),
               allowed_rules=("Edit",), denied_rules=("Bash",))
    _arun(ClaudeSdkRunner("do it", query_fn=q), ctx)
    prompt, options = captured[0]
    assert prompt == "do it"
    assert options.model == "opus"
    assert options.cwd == str(tmp_path)
    assert options.max_turns == 5
    assert options.max_budget_usd == 0.5
    assert options.permission_mode == "dontAsk"
    assert options.tools == ["Read"]
    assert options.allowed_tools == ["Edit"]
    assert options.disallowed_tools == ["Bash"]


def test_runner_success_outcome(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"plan": true}', encoding="utf-8", newline="")
    seen: list = []

    async def q(prompt: str, options: object):
        seen.append(prompt)
        yield _result(model_usage=_MU)

    ctx = _ctx(tmp_path, input_manifest_path=str(manifest))
    out = _arun(ClaudeSdkRunner("do it", query_fn=q), ctx)
    assert out.reason == SDKReason.COMPLETED
    assert out.session_id == "s1"
    assert out.turns == 2
    assert out.usage == SDKUsage(10, 20)
    assert out.estimated_cost_usd == Decimal("0.01")
    assert out.result_text == "ok"
    assert out.resolved_model == "claude-opus-test-1"
    assert '{"plan": true}' in seen[0]  # manifest 并入 prompt
    assert resolved_model_from_result(_result(model_usage=_MU)) == "claude-opus-test-1"


def test_runner_structured_output_and_empty_tools(tmp_path) -> None:
    captured: list = []

    async def q(prompt: str, options: object):
        captured.append(options)
        yield _result(result='{"verdict":"PASS","findings":[]}',
                      structured_output={"verdict": "PASS", "findings": []})

    out = _arun(ClaudeSdkRunner("do it", query_fn=q), _ctx(tmp_path))
    assert captured[0].tools == []  # 空白名单必须禁用全部，不能扩成默认工具集
    assert out.structured_output == {"verdict": "PASS", "findings": []}


def test_runner_sdk_error_to_error_outcome(tmp_path) -> None:
    async def boom(prompt: str, options: object):
        raise RuntimeError("conn refused")
        yield  # pragma: no cover

    out = _arun(ClaudeSdkRunner("do it", query_fn=boom), _ctx(tmp_path))
    assert out.reason == SDKReason.ERROR
    assert out.error_code == "sdk_error:RuntimeError"


def test_runner_result_error_mapped(tmp_path) -> None:
    async def q(prompt: str, options: object):
        yield _result(is_error=True, errors=["boom"], terminal_reason="x")

    out = _arun(ClaudeSdkRunner("do it", query_fn=q), _ctx(tmp_path))
    assert out.reason == SDKReason.ERROR
    assert out.error_code == "boom"


def test_runner_cost_over_budget_mapped(tmp_path) -> None:
    ctx = _ctx(tmp_path, max_budget_usd=Decimal("0.5"))

    async def q(prompt: str, options: object):
        yield _result(total_cost_usd=0.9)

    out = _arun(ClaudeSdkRunner("do it", query_fn=q), ctx)
    assert out.reason == SDKReason.BUDGET_EXCEEDED


def test_runner_missing_cwd_valueerror(tmp_path) -> None:
    ctx = SdkRunContext(cwd=str(tmp_path / "nope"), max_turns=5)
    with pytest.raises(ValueError, match="cwd not a directory"):
        _arun(ClaudeSdkRunner("do it", query_fn=None), ctx)


def test_runner_implements_sdk_runner() -> None:
    assert isinstance(ClaudeSdkRunner("hi"), SdkRunner)


def test_live_controlled_example(tmp_path) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    ctx = _ctx(tmp_path, max_turns=3, permission_mode="dontAsk")
    runner = ClaudeSdkRunner("Reply with exactly: ok")
    out = asyncio.run(runner.run(ctx))
    assert out.reason in set(SDKReason)
    assert out.session_id is not None or out.usage != SDKUsage()
