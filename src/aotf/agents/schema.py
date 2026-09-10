"""AOTF agents SDK 执行面 typed 契约（M0-D1）。

spec §7.1 所述 Python Claude Agent SDK 能力面的值对象化：一次 SDK 运行
所需的完整执行参数（SdkRunContext）与 SDK 层裸结果事实（SDKRunOutcome）。
§7.1 SDK 能力面映射：max_turns/max_budget_usd/tools 可见性/permission_mode
（dontAsk）/can_use_tool 动态判定记录（SDKToolAttempt）。

两域分离设计裁决：A4a（aotf.runner）的 AgentRunRequest/AgentRunResult 是
「编排面」契约（M0-A 冻结，不含 cwd/tools 等 SDK 参数）；本模块是「SDK
执行面」，由 M0-E orchestrator 组装（request→ctx→SdkRunner→outcome→A4a
result）。fake.py（FakeClaudeSDK）与 D2 真实 adapter 都实现 SdkRunner 面，
可替换性 §7.1。

deadline 归 A4b（normalize_status 按 ended_at 判，#25），SDK 无独立 deadline
语义 → SdkRunContext 不含 deadline_at，不重复。

本模块零依赖（不 import aotf 任何模块，自足零环）；校验失败抛 ValueError
（运行期契约，非领域错误、不扩冻结 16 码，对齐 runner.py 先例）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "SDKReason",
    "SDKRunOutcome",
    "SDKToolAttempt",
    "SDKUsage",
    "SdkRunContext",
    "SdkRunner",
]

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


class SDKReason(StrEnum):
    """SDK 层裸终止原因。同值同 A4a 编排 status 但不同域（SDK 终止事实）。"""

    COMPLETED = "completed"
    TIMEOUT = "timeout"            # SDK 内 turns 耗尽终止
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"
    CANCELLED = "cancelled"


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _check_token(name: str, value: object) -> None:
    if (not isinstance(value, str) or value in (".", "..")
            or not _TOKEN_RE.fullmatch(value)):
        raise ValueError(f"invalid {name} token")


def _check_path_field(name: str, value: object) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"invalid {name}")


def _check_cwd(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid cwd")
    p = Path(value)
    win = PureWindowsPath(value)
    absolute = p.is_absolute() or bool(win.drive and win.root)
    # cwd 须为完整绝对路径（Windows = drive + root）。'/x' '\\x' 无盘符时
    # is_absolute()==False 但 root 非空（根化）——缺 drive 不算可执行工作目录。
    if not absolute or ".." in p.parts or ".." in win.parts:
        raise ValueError("invalid cwd")


def _check_int_nonneg(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be non-negative int")


def _check_decimal_nonneg(name: str, value: object) -> None:
    if not isinstance(value, Decimal) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be non-negative Decimal")


@dataclass(frozen=True, slots=True)
class SdkRunContext:
    """一次 SDK 运行的完整执行参数（spec §7.1 能力面）。

    cwd 必填绝对路径；requested_model 用别名（opus/sonnet/…）不写死易过期
    完整 model id（§7.2 末，resolved 由真实 SDK/调用方记录）；visible_tools
    决定模型可见工具；allowed_rules 只表示预批准（≠限制，§7.3）；denied_rules
    显式拒绝；allowed 与 denied 不重叠（配置矛盾 fail-closed）。
    """

    cwd: str
    system_prompt_path: str | None = None
    input_manifest_path: str | None = None
    requested_model: str = "opus"
    visible_tools: tuple[str, ...] = ()
    allowed_rules: tuple[str, ...] = ()
    denied_rules: tuple[str, ...] = ()
    max_turns: int = 1
    max_budget_usd: Decimal = Decimal("0")  # 0 = 未设预算上限（SDK 语义保守可配）
    permission_mode: Literal["default", "dontAsk"] = "default"

    def __post_init__(self) -> None:
        _check_cwd(self.cwd)
        _check_path_field("system_prompt_path", self.system_prompt_path)
        _check_path_field("input_manifest_path", self.input_manifest_path)
        _check_token("requested_model", self.requested_model)
        for name, coll in (
            ("visible_tools", self.visible_tools),
            ("allowed_rules", self.allowed_rules),
            ("denied_rules", self.denied_rules),
        ):
            if not isinstance(coll, tuple):
                raise ValueError(f"{name} must be a tuple")
            for entry in coll:
                _check_token(name, entry)
        overlap = set(self.allowed_rules) & set(self.denied_rules)
        if overlap:
            raise ValueError("allowed and denied overlap")
        if not isinstance(self.max_turns, int) or isinstance(self.max_turns, bool) \
                or self.max_turns < 1:
            raise ValueError("max_turns must be positive int")
        _check_decimal_nonneg("max_budget_usd", self.max_budget_usd)
        if self.permission_mode not in ("default", "dontAsk"):
            raise ValueError("invalid permission_mode")


@dataclass(frozen=True, slots=True)
class SDKToolAttempt:
    """can_use_tool 动态判定的一次记录（§7.1/§7.3 hook 事实；D4 细化判定）。"""

    tool_name: str
    path: str | None = None
    allowed: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        _check_token("tool_name", self.tool_name)
        if self.path is not None and (not isinstance(self.path, str)
                                      or not self.path):
            raise ValueError("invalid attempt path")
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be bool")
        if self.reason is not None and (not isinstance(self.reason, str)
                                        or not self.reason):
            raise ValueError("invalid attempt reason")


@dataclass(frozen=True, slots=True)
class SDKUsage:
    """token 事实（§7.1 结构化 token）。"""

    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        _check_int_nonneg("input_tokens", self.input_tokens)
        _check_int_nonneg("output_tokens", self.output_tokens)


@dataclass(frozen=True, slots=True)
class SDKRunOutcome:
    """SDK 层裸结果（fake 与 D2 真实 runner 同出口）。

    result_text/structured_output 是角色适配层进行机械判定的事实输入；
    resolved_model 直接来自终止消息，避免调用方丢失实际模型。attempts 记录
    can_use_tool 的每次裁决。turns/budget 门归 fake。
    """

    session_id: str | None = None
    turns: int = 0
    reason: SDKReason = SDKReason.COMPLETED
    usage: SDKUsage = SDKUsage()
    attempts: tuple[SDKToolAttempt, ...] = ()
    result_text: str | None = None
    structured_output: object | None = None
    resolved_model: str | None = None
    estimated_cost_usd: Decimal = Decimal("0")
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.session_id is not None and (not isinstance(self.session_id, str)
                                            or not self.session_id):
            raise ValueError("invalid session_id")
        _check_int_nonneg("turns", self.turns)
        if not isinstance(self.reason, SDKReason):
            raise ValueError("invalid reason")
        if not isinstance(self.usage, SDKUsage):
            raise ValueError("usage must be SDKUsage")
        if not isinstance(self.attempts, tuple) \
                or not all(isinstance(a, SDKToolAttempt) for a in self.attempts):
            raise ValueError("attempts must be tuple of SDKToolAttempt")
        if self.result_text is not None and (not isinstance(self.result_text, str)
                                             or not self.result_text.strip()):
            raise ValueError("invalid result_text")
        if self.structured_output is not None:
            try:
                json.dumps(self.structured_output, allow_nan=False)
            except (TypeError, ValueError):
                raise ValueError("structured_output must be JSON-compatible") \
                    from None
        if self.resolved_model is not None and (
                not isinstance(self.resolved_model, str)
                or not self.resolved_model.strip()):
            raise ValueError("invalid resolved_model")
        _check_decimal_nonneg("estimated_cost_usd", self.estimated_cost_usd)
        if self.error_code is not None and (not isinstance(self.error_code, str)
                                            or not self.error_code):
            raise ValueError("invalid error_code")


@runtime_checkable
class SdkRunner(Protocol):
    """SDK 面：一次运行 ctx → outcome。fake 与 D2 真实 adapter 同实现。"""

    async def run(self, ctx: SdkRunContext) -> SDKRunOutcome:
        ...
