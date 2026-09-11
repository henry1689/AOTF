"""AOTF 真实 Claude Agent SDK adapter（M0-D2）。

把 D1 的 SdkRunner 面落到官方 `claude-agent-sdk`（本包接入版本 0.2.152，
2026-09-06 反射实测——WebFetch 不可达，API 形状以安装后探针为准）：

- 主入口 `query(*, prompt: str, options: ClaudeAgentOptions|None) -> AsyncIterator[Message]`；
- `ClaudeAgentOptions` dataclass：model/cwd/system_prompt/max_turns/
  max_budget_usd/permission_mode('default'|'dontAsk'|…)/tools/allowed_tools/
  disallowed_tools/can_use_tool((tool,input,ctx)->Awaitable[Allow|Deny])；
- 终止消息 `ResultMessage`：num_turns/session_id/is_error/errors/total_cost_usd/
  model_usage: dict[model, ModelUsage{inputTokens,outputTokens,costUSD,canonicalModel,…}]。

映射意图（SdkRunContext → SDK）：cwd→options.cwd；requested_model→
resolve_model；system_prompt_path→读取文本入 options.system_prompt；
input_manifest_path→读取文本并入 user prompt；visible_tools→options.tools
（空 tuple 明确映射 []，禁用全部工具，绝不扩成 SDK 默认全集）；allowed_rules/denied_rules→
allowed_tools/disallowed_tools；max_turns→options.max_turns；
max_budget_usd>0→options.max_budget_usd（0=不设）；permission_mode→
options.permission_mode（dontAsk 忠实透传，headless 用）。所有调用固定
setting_sources=[]、strict_mcp_config=True、mcp_servers={}，不加载用户/项目
设置、plugin MCP 或 hook 形成 AOTF 之外的旁路。

reason 判定（确定性；SDK 无独立「成功但超 turns」态——超 turns 由 Claude
Code 以 error 呈现；deadline 超时归 A4b normalize_status，#25）：
  is_error=True            → SDKReason.ERROR（error_code=errors[0] 或
                             terminal_reason 或 "sdk_error"）；
  否则 total_cost_usd>max_budget(>0) → SDKReason.BUDGET_EXCEEDED（兜底复核）；
  否则                       → SDKReason.COMPLETED。
SDK 调用异常 → ERROR outcome 不冒泡（同 FakeClaudeSDK handler 语义）。

结果通道：ResultMessage.result/structured_output/model_usage 分别映射到
SDKRunOutcome.result_text/structured_output/resolved_model；can_use_tool 每次
裁决映射为 SDKToolAttempt。A4a AgentRunResult.resolved_model 由适配层透传。

依赖单向：agents/schema（同子包）+ aotf.runner（只读 A4a 值对象——
编排面桥，D1「agents 零 aotf import」的唯一豁免，不改冻结模块）。本模块
顶层不 import claude_agent_sdk（延迟 import，mapper/alias 可零 SDK 安装
测试；sdk_module/query_fn 注入点供测试零真实请求）。真实受控样例经
ANTHROPIC_API_KEY 门控（live 测试默认 skip）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import namedtuple
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace

from aotf.agents.middleware_ccswitch import (
    CC_SWITCH_ENV_KEYS,
    detect_ccswitch_path,
    get_ccswitch_env,
)
from aotf.agents.roles import ROLE_IDS
from aotf.agents.schema import (
    SDKReason,
    SDKRunOutcome,
    SDKToolAttempt,
    SDKUsage,
    SdkRunContext,
    SdkRunner,
)
from aotf.runner import AgentRunRequest, AgentRunResult

# 将 CC-Switch JSON dict 转为对象，使 run() 的鸭子类型检测（num_turns/session_id/is_error）可用
_DictToObj = SimpleNamespace

_log = logging.getLogger(__name__)

__all__ = [
    "ALIAS_MODELS",
    "ClaudeSdkRunner",
    "model_usage_map",
    "resolve_model",
    "resolved_model_from_result",
    "to_agent_run_result",
]

# 别名 → 模型。0.2.152 的 options.model 直接透传 Claude Code CLI 解析；
# 若 CLI 不接受别名，在此表填真实 model id（live 校准点）。
ALIAS_MODELS: dict[str, str] = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "deepseek": "sonnet",  # live CLI 只认 Anthropic 别名；deepseek 后端由网关在端侧路由（owner 实测校准）
}

_QUERY_PROMPT = "query"
_QUERY_OPTIONS = "options"


def resolve_model(alias: str) -> str:
    """别名 → model。未知 → ValueError（fail-closed）。"""
    try:
        return ALIAS_MODELS[alias]
    except KeyError:
        raise ValueError(f"unknown model alias: {alias}") from None


def _usage_field(obj: object, name: str) -> object:
    """同时兼容 dict 与对象取值（CLI JSON dict / SDK dataclass）。"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def model_usage_map(result: object) -> dict:
    """取 model→usage 映射。

    SDK ResultMessage 用 `model_usage`（snake_case）；CC-Switch CLI 的
    `--output-format json` 用 `modelUsage`（camelCase）。两条通道都要能统计
    token/resolved_model（M1 指标口径）。
    """
    for field in ("model_usage", "modelUsage"):
        value = getattr(result, field, None)
        if isinstance(value, dict) and value:
            return value
    return {}


def resolved_model_from_result(result: object) -> str | None:
    """ResultMessage.model_usage 首个 key 或 ModelUsage.canonicalModel。"""
    model_usage = model_usage_map(result)
    if model_usage:
        for model_id in model_usage:
            if isinstance(model_id, str) and model_id:
                return model_id
        for usage in model_usage.values():
            canonical = _usage_field(usage, "canonicalModel")
            if isinstance(canonical, str) and canonical:
                return canonical
    return None


def to_agent_run_result(
    request: AgentRunRequest,
    outcome: SDKRunOutcome,
    *,
    resolved_model: str | None,
    output_artifact_id: str | None = None,
) -> AgentRunResult:
    """SDKRunOutcome → A4a AgentRunResult（编排面 record_run 直接可消费）。

    SDKReason→A4a status：COMPLETED→completed / TIMEOUT→timeout /
    BUDGET_EXCEEDED→budget_exceeded / CANCELLED→cancelled / ERROR→failed。
    run_id 绑定 request.run_id（一致性保证同 record_run 语义）；校验委托
    A4a 值对象 __post_init__。
    """
    status = {
        SDKReason.COMPLETED: "completed",
        SDKReason.TIMEOUT: "timeout",
        SDKReason.BUDGET_EXCEEDED: "budget_exceeded",
        SDKReason.CANCELLED: "cancelled",
        SDKReason.ERROR: "failed",
    }[outcome.reason]
    return AgentRunResult(
        run_id=request.run_id,
        status=status,  # type: ignore[arg-type]
        resolved_model=resolved_model,
        input_tokens=outcome.usage.input_tokens,
        output_tokens=outcome.usage.output_tokens,
        estimated_cost_usd=outcome.estimated_cost_usd,
        output_artifact_id=output_artifact_id,
        error_code=outcome.error_code,
    )


class ClaudeSdkRunner:
    """实现 SdkRunner 面：ctx → 真实 claude-agent-sdk 调用 → SDKRunOutcome。

    构造注入 prompt（角色指令文本；D3 三角色 adapter 生成）与可选
    query_fn/options 工厂（默认 import claude_agent_sdk.query）。测试注入
    假 query_fn 零真实请求。
    """

    def __init__(
        self,
        prompt: str,
        *,
        query_fn: Callable[..., AsyncIterator[object]] | None = None,
        role: str = "implementer",
        allowed_files: tuple[str, ...] = (),
        tool_decider: Callable[..., object] | None = None,
        use_ccswitch: bool | None = None,  # M1: 自动探测
    ) -> None:
        """role/allowed_files/tool_decider 为 D4 工具权限接线（§7.3）。

        tool_decider 注入时 _build_options 挂 can_use_tool 闭包（决策→SDK
        Allow/Deny）；默认 None 不设 can_use_tool → SDK dontAsk 拒未授权
        （安全侧）。
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt required")
        if role not in ROLE_IDS:
            raise ValueError(f"unknown role: {role}")
        if not isinstance(allowed_files, tuple):
            raise ValueError("allowed_files must be a tuple")
        if tool_decider is not None and not callable(tool_decider):
            raise ValueError("tool_decider must be callable")
        self._prompt = prompt
        self._query_fn = query_fn
        self._role = role
        self._allowed_files = allowed_files
        self._tool_decider = tool_decider
        # M1: CC-Switch 探测
        if use_ccswitch is None:
            use_ccswitch = detect_ccswitch_path() is not None
        self._use_ccswitch = use_ccswitch
        self._ccswitch_env = get_ccswitch_env() if use_ccswitch else {}

    async def _query(self, prompt: str, options: object):
        # M1: 通过 CC-Switch 子进程启动
        if self._use_ccswitch and not self._query_fn:
            async for event in self._query_via_ccswitch(prompt, options):
                # _query_via_ccswitch 返回 dict（JSON 解析），run() 用鸭子类型检测
                # 终止消息（num_turns/session_id/is_error）需要是对象属性，统一转换
                if isinstance(event, dict):
                    yield _DictToObj(event)
                else:
                    yield event
            return
        
        if self._query_fn is not None:
            async for event in self._query_fn(prompt=prompt, options=options):
                yield event
            return
        import claude_agent_sdk as _sdk  # 延迟：默认路径才触 SDK

        async for event in _sdk.query(prompt=prompt, options=options):
            yield event
    
    async def _query_via_ccswitch(
        self, prompt: str, options: object
    ) -> AsyncIterator[object]:
        """通过 CC-Switch CLI 子进程启动 Claude Code（M1 真实模型通道）。

        架构约束（对齐 SDK 路径 _build_options）：
        - visible_tools 空 tuple → `--tools ""` 禁用全部工具，绝不扩成默认全集；
          这是 controller-only write 模型的硬前提（模型只能提 mutation intents，
          不得自行落盘）；
        - 非空 → 逐名透传（planner 的只读工具集）；
        - 不传 --allowedTools：预批准 ≠ 工具可见性，避免旁路扩大权限面。
        """
        import os as _os
        ccswitch_path = detect_ccswitch_path()
        if ccswitch_path is None:
            raise RuntimeError("CC-Switch path not found")

        # 从 options (SdkRunContext) 提取参数
        model = getattr(options, 'requested_model', 'deepseek') if hasattr(options, 'requested_model') else 'deepseek'
        max_turns = getattr(options, 'max_turns', 1) if hasattr(options, 'max_turns') else 1
        max_budget = getattr(options, 'max_budget_usd', 5.0) if hasattr(options, 'max_budget_usd') else 5.0
        cwd = getattr(options, 'cwd', '.') if hasattr(options, 'cwd') else '.'
        visible_tools = getattr(options, 'visible_tools', ()) or ()
        permission_mode = getattr(options, 'permission_mode', 'dontAsk') or 'dontAsk'
        system_prompt_path = getattr(options, 'system_prompt_path', None)

        # 空 tuple → 显式禁用全部工具（语义同 SDK tools=[]）
        tools_arg = ",".join(visible_tools) if visible_tools else ""

        # CLI 的 turn 计数含内部步骤（系统提示处理、结构化输出生成），与 SDK
        # 单轮语义不等价：实测 max_turns=1 会以 "Reached maximum number of
        # turns (1)" 丢掉已完成的推理。零工具（--tools ""）下多轮无副作用
        # 能力，上限仍由 --max-budget-usd 封住，故设下限而非原值透传。
        cli_max_turns = max(3, int(max_turns))

        cmd = [
            str(ccswitch_path),
            "-p", prompt,
            "--output-format", "json",
            "--tools", tools_arg,
            "--max-turns", str(cli_max_turns),
            "--max-budget-usd", str(float(max_budget)),
            "--permission-mode", permission_mode,
        ]
        if cwd and cwd != ".":
            cmd += ["--add-dir", cwd]
        if system_prompt_path:
            from pathlib import Path as _Path
            sp = _Path(system_prompt_path)
            if sp.is_file():
                cmd += ["--append-system-prompt", sp.read_text(encoding="utf-8")]

        env = get_ccswitch_env()
        env.update(_os.environ)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=300
            )

            output = stdout.decode('utf-8', errors='replace')
            err_text = (stderr.decode('utf-8', errors='replace') if stderr
                        else "")
            parsed: object | None = None
            if output.strip():
                try:
                    parsed = json.loads(output)
                except json.JSONDecodeError:
                    parsed = {"type": "text", "content": output}

            # CLI 自带 is_error 的终止结果即为权威判定（含 error_max_turns /
            # budget 等）。非零退出码不再二次否决——否则已解析出的
            # structured_output/result 会被异常丢弃，模型产出白费。
            if isinstance(parsed, dict) and "is_error" in parsed:
                yield parsed
                return

            if proc.returncode != 0:
                tail = err_text.strip()[:300] or "no stderr"
                raise RuntimeError(
                    f"CC-Switch exit={proc.returncode}: {tail}")
            if parsed is not None:
                yield parsed
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("CC-Switch timeout")

    async def run(self, ctx: SdkRunContext) -> SDKRunOutcome:
        if not Path(ctx.cwd).is_dir():
            raise ValueError("runner cwd not a directory")
        prompt = self._prompt
        if ctx.input_manifest_path is not None:
            manifest = Path(ctx.input_manifest_path)
            if manifest.is_file():
                prompt = prompt + "\n\n" + manifest.read_text(encoding="utf-8")
        attempts: list[SDKToolAttempt] = []
        options = self._build_options(ctx, attempts)
        try:
            result = None
            async for event in self._query(prompt, options):
                if (getattr(event, "num_turns", None) is not None
                        and hasattr(event, "session_id")
                        and hasattr(event, "is_error")):
                    result = event  # 终止 ResultMessage 特征（鸭子，避顶层 import）
            if result is None:
                return SDKRunOutcome(attempts=tuple(attempts),
                                     reason=SDKReason.ERROR,
                                     error_code="no_result_message")
            return self._map_result(ctx, result, attempts)
        except Exception as exc:  # SDK 运行异常 → ERROR 不冒泡
            # error_code 只容纳码（无空格）；原因细节走日志，否则诊断断链
            _log.warning("runner query failed: %s: %s", type(exc).__name__, exc)
            return SDKRunOutcome(
                attempts=tuple(attempts),
                reason=SDKReason.ERROR,
                error_code=f"sdk_error:{type(exc).__name__}",
            )

    def _build_options(self, ctx: SdkRunContext,
                       attempts: list[SDKToolAttempt]) -> object:
        """构造真实 ClaudeAgentOptions（query_fn 注入只替换执行，options 恒真）。"""
        import claude_agent_sdk as _sdk

        kwargs: dict = dict(
            model=resolve_model(ctx.requested_model),
            cwd=ctx.cwd,
            max_turns=ctx.max_turns,
            max_budget_usd=(None if ctx.max_budget_usd <= 0
                            else float(ctx.max_budget_usd)),
            permission_mode=ctx.permission_mode,
            # SDK 语义：None=默认工具集，[]=明确禁用全部。不得把空白名单
            # 扩大成默认全集。
            tools=list(ctx.visible_tools),
            allowed_tools=list(ctx.allowed_rules),
            disallowed_tools=list(ctx.denied_rules),
            # 不加载用户/项目 settings 或外部 MCP。否则 hooks、permission
            # allow rules、plugins/MCP 可能在 AOTF 工具矩阵之外重新取得控制权。
            setting_sources=[],
            mcp_servers={},
            strict_mcp_config=True,
            system_prompt=self._read_optional(ctx.system_prompt_path),
        )
        can_use = self._make_can_use(ctx, attempts)
        if can_use is not None:
            kwargs["can_use_tool"] = can_use
        return _sdk.ClaudeAgentOptions(**kwargs)

    def _make_can_use(self, ctx: SdkRunContext,
                      attempts: list[SDKToolAttempt]):
        """tool_decider 注入时包装为 SDK can_use_tool 闭包（D4，§7.3）。

        判定结果 ToolDecision → SDK PermissionResultAllow/Deny（延迟 import）。
        """
        if self._tool_decider is None:
            return None

        async def can_use(tool_name: str, tool_input: dict, _perm_ctx):
            if tool_name not in ctx.visible_tools:
                allow, reason = False, "tool-not-visible"
            else:
                decision = self._tool_decider(
                    role=self._role, tool_name=tool_name, tool_input=tool_input,
                    worktree_path=ctx.cwd, allowed_files=self._allowed_files,
                )
                allow, reason = bool(decision.allow), str(decision.reason)
            path = None
            if isinstance(tool_input, dict):
                for key in ("file_path", "path", "notebook_path"):
                    value = tool_input.get(key)
                    if isinstance(value, str) and value:
                        path = value
                        break
            attempts.append(SDKToolAttempt(
                tool_name=tool_name, path=path, allowed=allow, reason=reason))
            import claude_agent_sdk as _sdk

            if allow:
                return _sdk.PermissionResultAllow()
            return _sdk.PermissionResultDeny(message=reason)

        return can_use

    def _map_result(self, ctx: SdkRunContext, result: object,
                    attempts: list[SDKToolAttempt]) -> SDKRunOutcome:
        errors = getattr(result, "errors", None) or []
        is_error = bool(getattr(result, "is_error", False))
        if is_error:
            code = ((errors[0] if errors else None)
                    or getattr(result, "terminal_reason", None)
                    or "sdk_error")[:64]  # 截断：error_code 是码不是长消息
            return SDKRunOutcome(
                session_id=getattr(result, "session_id", None),
                turns=int(getattr(result, "num_turns", 0) or 0),
                reason=SDKReason.ERROR,
                usage=self._usage(result),  # error 仍带部分 token 事实
                attempts=tuple(attempts),
                result_text=self._result_text(result),
                structured_output=getattr(result, "structured_output", None),
                resolved_model=resolved_model_from_result(result),
                estimated_cost_usd=_cost(getattr(result, "total_cost_usd", None)),
                error_code=code,
            )
        cost = getattr(result, "total_cost_usd", None)
        if (ctx.max_budget_usd > 0 and isinstance(cost, (int, float))
                and cost > ctx.max_budget_usd):
            return self._outcome(ctx, result, SDKReason.BUDGET_EXCEEDED,
                                 attempts)
        return self._outcome(ctx, result, SDKReason.COMPLETED, attempts)

    def _outcome(self, ctx: SdkRunContext, result: object,
                 reason: SDKReason,
                 attempts: list[SDKToolAttempt]) -> SDKRunOutcome:
        return SDKRunOutcome(
            session_id=getattr(result, "session_id", None),
            turns=int(getattr(result, "num_turns", 0) or 0),
            reason=reason,
            usage=self._usage(result),
            attempts=tuple(attempts),
            result_text=self._result_text(result),
            structured_output=getattr(result, "structured_output", None),
            resolved_model=resolved_model_from_result(result),
            estimated_cost_usd=_cost(getattr(result, "total_cost_usd", None)),
        )

    @staticmethod
    def _result_text(result: object) -> str | None:
        value = getattr(result, "result", None)
        return value if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _usage(result: object) -> SDKUsage:
        inp = out = 0
        for usage in model_usage_map(result).values():
            inp += int(_usage_field(usage, "inputTokens") or 0)
            out += int(_usage_field(usage, "outputTokens") or 0)
        if inp == 0 and out == 0:
            # CLI 顶层 usage 用 snake_case（input_tokens/output_tokens）
            top = getattr(result, "usage", None)
            inp = int(_usage_field(top, "input_tokens") or 0)
            out = int(_usage_field(top, "output_tokens") or 0)
        return SDKUsage(input_tokens=inp, output_tokens=out)

    @staticmethod
    def _read_optional(path: str | None) -> str | None:
        if path is None:
            return None
        p = Path(path)
        return p.read_text(encoding="utf-8") if p.is_file() else None


def _cost(value: object) -> object:
    from decimal import Decimal

    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return Decimal("0")
