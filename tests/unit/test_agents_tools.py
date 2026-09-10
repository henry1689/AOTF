"""M0-D4 工具权限矩阵测试（filter/decide + can_use_tool 接线）。

#30（网络默认移除）、#12（planner/reviewer 拒写）、#10（canonical 越界/
traversal）、§7.3（Bash 白名单）各有权衡；wiring 用注入 query_fn 零真实请求。
"""

from __future__ import annotations

import asyncio

import pytest

import claude_agent_sdk as sdk

from aotf.agents.claude_sdk import ClaudeSdkRunner
from aotf.agents.schema import SdkRunContext
from aotf.agents.tools import (
    NETWORK_TOOLS,
    WRITE_TOOLS,
    decide_tool,
    filter_visible_tools,
)


def _decide(role, tmp_path, *, tool="Edit", inp=None, allowed=("a.py",),
            **over) -> tuple[bool, str]:
    d = decide_tool(role, tool_name=tool, tool_input=inp if inp is not None else {},
                    worktree_path=str(tmp_path), allowed_files=allowed, **over)
    return d.allow, d.reason


def test_filter_network_removed_by_default() -> None:
    out = filter_visible_tools("implementer",
                               visible=("Edit", "Read", "WebFetch"))
    assert out == ("Edit", "Read")  # #30


def test_filter_network_opt_in() -> None:
    out = filter_visible_tools("implementer", visible=("WebSearch",),
                               network=True)
    assert out == ("WebSearch",)


def test_filter_reviewer_no_write() -> None:
    out = filter_visible_tools("reviewer", visible=("Edit", "Write", "Read"))
    assert out == ("Read",)  # #12


def test_filter_planner_no_write() -> None:
    out = filter_visible_tools("planner", visible=("Write", "Read"))
    assert out == ("Read",)


def test_filter_implementer_keeps_write() -> None:
    out = filter_visible_tools("implementer", visible=("Edit", "Read"))
    assert "Edit" in out


def test_filter_unknown_role_rejected() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        filter_visible_tools("adversary", visible=("Read",))


def test_decide_read_tool_allowed(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path, tool="Read")
    assert allow and reason == "ok-other"


def test_decide_reviewer_write_denied(tmp_path) -> None:
    allow, reason = _decide("reviewer", tmp_path, tool="Edit")
    assert not allow and reason == "role-read-only"  # #12


def test_decide_planner_write_denied(tmp_path) -> None:
    allow, reason = _decide("planner", tmp_path, tool="Write")
    assert not allow and reason == "role-read-only"


def test_decide_network_denied(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path, tool="WebFetch")
    assert not allow and reason == "network-disabled"  # #30


def test_decide_bash_denied_by_default(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path, tool="Bash",
                            inp={"command": "rm -rf /"})
    assert not allow and reason == "bash-disabled"  # §7.3 M0


def test_decide_bash_allowlist_hit(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path, tool="Bash",
                            inp={"command": "pytest -q"},
                            bash_allowlist=("pytest -q",))
    assert allow and reason == "ok"


def test_decide_edit_allowed_in_scope(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path,
                            inp={"file_path": str(tmp_path / "a.py")})
    assert allow and reason == "ok"


def test_decide_edit_out_of_scope(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path,
                            inp={"file_path": str(tmp_path / "b.py")})
    assert not allow and reason == "out-of-scope"


def test_decide_edit_traversal_denied(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path,
                            inp={"file_path": str(tmp_path.parent / "secret.py")})
    assert not allow and reason == "traversal"  # #10


def test_decide_edit_no_path_denied(tmp_path) -> None:
    allow, reason = _decide("implementer", tmp_path, inp={})
    assert not allow and reason == "no-path"


def _ctx(tmp_path) -> SdkRunContext:
    return SdkRunContext(
        cwd=str(tmp_path), max_turns=5,
        visible_tools=("Read", "Glob", "Grep", "Edit", "Write"))


def _runner(prompt, *, role="implementer", allowed=("a.py",),
            decider=decide_tool, captured):
    async def q(prompt: str, options: object):
        captured.append(options)
        yield sdk.ResultMessage(
            subtype="result", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", result="ok")

    return ClaudeSdkRunner(prompt, query_fn=q, role=role,
                           allowed_files=allowed, tool_decider=decider)


def test_can_use_tool_wiring_allow(tmp_path) -> None:
    captured: list = []
    runner = _runner("p", captured=captured)
    asyncio.run(runner.run(_ctx(tmp_path)))
    opts = captured[0]
    assert opts.can_use_tool is not None
    res = asyncio.run(opts.can_use_tool(
        "Edit", {"file_path": str(tmp_path / "a.py")}, None))
    assert res.behavior == "allow"


def test_can_use_tool_wiring_deny(tmp_path) -> None:
    captured: list = []
    runner = _runner("p", captured=captured)
    asyncio.run(runner.run(_ctx(tmp_path)))
    opts = captured[0]
    res = asyncio.run(opts.can_use_tool(
        "Edit", {"file_path": str(tmp_path.parent / "x.py")}, None))
    assert res.behavior == "deny"
    assert res.message == "traversal"


def test_runner_default_no_can_use_tool(tmp_path) -> None:
    captured: list = []

    async def q(prompt: str, options: object):
        captured.append(options)
        yield sdk.ResultMessage(
            subtype="result", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", result="ok")

    runner = ClaudeSdkRunner("p", query_fn=q)  # tool_decider=None 默认
    asyncio.run(runner.run(_ctx(tmp_path)))
    assert captured[0].can_use_tool is None  # 安全侧


def test_can_use_tool_attempt_is_audited(tmp_path) -> None:
    captured: list = []

    async def q(prompt: str, options: object):
        captured.append(options)
        await options.can_use_tool(
            "Edit", {"file_path": str(tmp_path / "a.py")}, None)
        yield sdk.ResultMessage(
            subtype="result", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", result="ok")

    runner = ClaudeSdkRunner(
        "p", query_fn=q, role="implementer", allowed_files=("a.py",),
        tool_decider=decide_tool)
    out = asyncio.run(runner.run(_ctx(tmp_path)))
    assert len(out.attempts) == 1
    assert out.attempts[0].tool_name == "Edit"
    assert out.attempts[0].allowed is True
    assert out.attempts[0].reason == "ok"


def test_can_use_tool_not_visible_denied_and_audited(tmp_path) -> None:
    captured: list = []

    async def q(prompt: str, options: object):
        captured.append(options)
        result = await options.can_use_tool("Bash", {"command": "pwd"}, None)
        assert result.behavior == "deny"
        assert result.message == "tool-not-visible"
        yield sdk.ResultMessage(
            subtype="result", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", result="ok")

    runner = ClaudeSdkRunner(
        "p", query_fn=q, role="implementer", allowed_files=("a.py",),
        tool_decider=decide_tool)
    out = asyncio.run(runner.run(_ctx(tmp_path)))
    assert out.attempts[0].allowed is False
    assert out.attempts[0].reason == "tool-not-visible"


def test_tool_sets_shapes() -> None:
    assert NETWORK_TOOLS and WRITE_TOOLS  # 常量非空
    assert NETWORK_TOOLS.isdisjoint(WRITE_TOOLS)
