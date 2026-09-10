"""M0-E2 pilot RoleRunner adapter 测试（fake 注入零 token）。

with_fake 的 implementer/reviewer demo 经 FakeClaudeSDK + 真实 SdkRunContext
组装跑通 RoleOutcome；review_verdict 解析入口畸形 → ValueError（#26）；
越界/越权写由 decide_tool 权限矩阵拒绝（taskbook §5.6 #8 可选判定）。
"""

from __future__ import annotations

import asyncio
import hashlib
from decimal import Decimal

import pytest
import claude_agent_sdk as sdk

from aotf.agents.tools import decide_tool
from aotf.agents.schema import SDKReason, SDKRunOutcome
from aotf.pilot.adapter import RealRoleRunner, review_verdict, role_outcome


def _runner(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir(parents=True)
    (wt / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    return RealRoleRunner("task-demo", cwd=str(wt))


def test_fake_implementer_proposes_without_writing(tmp_path) -> None:
    runner = _runner(tmp_path).with_fake()
    wt = str(tmp_path / "wt")
    outcome = asyncio.run(runner.run(
        "implementer", inputs={"allowed_files": ("a.py",),
                               "worktree_path": wt}))
    assert outcome.status == "completed"
    assert (tmp_path / "wt" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert outcome.mutations[0].path == "a.py"
    assert "# demo edit" in (outcome.mutations[0].content or "")


def test_fake_reviewer_pass_verdict(tmp_path) -> None:
    runner = _runner(tmp_path).with_fake()
    outcome = asyncio.run(runner.run(
        "reviewer", inputs={"allowed_files": ()}))
    assert outcome.status == "completed"
    assert outcome.verdict == "PASS"


def test_review_verdict_malformed_json_valueerror() -> None:
    with pytest.raises(ValueError):
        review_verdict("not-json")
    assert review_verdict('{"verdict": "PASS", "findings": []}') == "PASS"


def test_role_outcome_reviewer_uses_structured_result() -> None:
    outcome = SDKRunOutcome(
        reason=SDKReason.COMPLETED,
        structured_output={"verdict": "PASS", "findings": []},
        resolved_model="claude-test", estimated_cost_usd=Decimal("0.01"))
    mapped = role_outcome(outcome, role="reviewer")
    assert mapped.verdict == "PASS"
    assert mapped.resolved_model == "claude-test"


def test_role_outcome_reviewer_missing_or_malformed_fails_closed() -> None:
    with pytest.raises(ValueError, match="reviewer output missing"):
        role_outcome(SDKRunOutcome(), role="reviewer")
    with pytest.raises(ValueError, match="reviewer output not JSON"):
        role_outcome(SDKRunOutcome(result_text="not-json"), role="reviewer")


def test_real_reviewer_result_is_parsed_with_read_only_tools(tmp_path) -> None:
    captured: list = []

    async def q(prompt: str, options: object):
        captured.append(options)
        yield sdk.ResultMessage(
            subtype="result", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="review-session",
            result='{"verdict":"PASS","findings":[]}',
            model_usage={"claude-test": {
                "inputTokens": 2, "outputTokens": 3, "costUSD": 0.01,
                "canonicalModel": "claude-test"}},
        )

    wt = tmp_path / "wt"
    wt.mkdir()
    runner = RealRoleRunner("task-demo", cwd=str(wt), query_fn=q)
    outcome = asyncio.run(runner.run("reviewer", inputs={
        "proposal_text": "review a.py",
        "allowed_files": ("a.py",),
        "baseline_tree": "a" * 40,
        "actual_tree": "b" * 40,
        "changes_summary": "changed files: a.py",
        "patch_text": "diff --git a/a.py b/a.py",
    }))
    assert outcome.verdict == "PASS"
    assert outcome.resolved_model == "claude-test"
    assert captured[0].tools == []
    assert captured[0].max_turns == 1


def test_real_implementer_is_one_shot_toolless_and_returns_intents(tmp_path) -> None:
    captured: list[tuple[str, object]] = []
    before = b"x = 1\n"

    async def q(prompt: str, options: object):
        captured.append((prompt, options))
        yield sdk.ResultMessage(
            subtype="result", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="impl-session",
            result=(
                '{"outcome":"completed","summary":"update",'
                '"mutations":[{"path":"a.py","operation":"replace",'
                f'"expected_sha256":"{hashlib.sha256(before).hexdigest()}",'
                '"content":"x = 2\\n"}]}'),
            model_usage={},
        )

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a.py").write_bytes(before)
    runner = RealRoleRunner("task-demo", cwd=str(wt), query_fn=q)
    outcome = asyncio.run(runner.run("implementer", inputs={
        "proposal_text": "change x", "allowed_files": ("a.py",),
        "worktree_path": str(wt), "baseline_tree": "a" * 40,
    }))
    prompt, options = captured[0]
    assert options.tools == []
    assert options.max_turns == 1
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {}
    assert hashlib.sha256(before).hexdigest() in prompt
    assert outcome.mutations[0].content == "x = 2\n"
    assert (wt / "a.py").read_bytes() == before


def test_real_implementer_turn_override_rejected(tmp_path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    with pytest.raises(ValueError, match="single controller-owned turn"):
        RealRoleRunner("task-demo", cwd=str(wt),
                       max_turns={"implementer": 2})


def test_real_reviewer_turn_override_rejected(tmp_path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    with pytest.raises(ValueError, match="single controller-owned turn"):
        RealRoleRunner("task-demo", cwd=str(wt),
                       max_turns={"reviewer": 2})


def test_tool_matrix_denies_out_of_scope_and_traversal(tmp_path) -> None:
    wt = str(tmp_path / "wt")
    assert not decide_tool(
        "implementer", tool_name="Write",
        tool_input={"file_path": str(tmp_path / "elsewhere.py")},
        worktree_path=wt, allowed_files=("a.py",)).allow  # traversal
    assert not decide_tool(
        "implementer", tool_name="Write",
        tool_input={"file_path": str(tmp_path / "wt" / "b.py")},
        worktree_path=wt, allowed_files=("a.py",)).allow  # out-of-scope
