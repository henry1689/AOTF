"""AOTF 工具权限矩阵（M0-D4）：spec §7.3 权限原则的确定性判定。

§7.3 逐条落位（纯权限原语与历史兼容测试）：
- tools 决定模型可见（filter_visible_tools：去网络 + 按角色去写工具）；
- Reviewer/Planner 不提供 Edit/Write（#12，role-read-only）；
- Implementer 的 Edit/Write 输入由 can_use_tool 对 canonical path 动态判定
  （decide_tool 写工具分支：resolve 规范化 + worktree 前缀 + allowed_files
  集内；越界/.. /symlink 逃逸 → traversal，§20.2 #10）；
- M0 不向 Implementer 暴露通用 Bash：默认 deny（bash-disabled），必要诊断
  经 bash_allowlist 精确命令白名单（command-id 工具承载）；
- 网络工具默认移除（#30：WebFetch/WebSearch 默认不可见，network=True 才保留）。

设计裁决：本模块零 SDK import（判定纯函数，返回自定 ToolDecision，由
ClaudeSdkRunner 闭包包装为 SDK PermissionResultAllow/Deny）；Windows 前缀
比较经 os.path.normcase（文件系统大小写不敏感）；symlink 逃逸经
Path.resolve()（存在链展开），真实建链端到端留 M0-E（本包不建脆弱
symlink 测试，用路径遍历/越界覆盖 canonical 语义）。

2026-09-09 控制权加固后，生产 RealRoleRunner 对所有模型角色固定 tools=[]，
不再使用 Edit/Write 判定路径；本模块保留为底层拒绝原语和回归证据，不构成
“允许 Claude Code 直接编辑”的运行授权。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aotf.agents.roles import ROLE_IDS

__all__ = [
    "EXEC_TOOLS",
    "NETWORK_TOOLS",
    "WRITE_TOOLS",
    "ToolDecision",
    "decide_tool",
    "filter_visible_tools",
]

NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})  # #30 默认移除
WRITE_TOOLS = frozenset({"Edit", "Write"})  # #12 Reviewer/Planner 无源码写工具
EXEC_TOOLS = frozenset({"Bash"})  # M0 不暴露通用 Bash（§7.3）

_READONLY_ROLES = ("planner", "reviewer")


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ToolDecision:
    """单次工具判定。reason 为机器码，供 SDK Deny message 与审计。"""

    allow: bool
    reason: str


def filter_visible_tools(
    role: str,
    *,
    visible: tuple[str, ...] | None = None,
    network: bool = False,
    allow_write: bool | None = None,
) -> tuple[str, ...]:
    """按角色过滤可见工具（去重保序）。

    visible=None 视为 SDK 默认全集需调用方展开；本函数只做约束过滤：
    去 NETWORK_TOOLS（除非 network=True，#30）；planner/reviewer 且非
    allow_write → 去 WRITE_TOOLS（#12）。
    """
    _require(role in ROLE_IDS, f"unknown role: {role}")
    base = list(visible or ())
    seen: set[str] = set()
    out: list[str] = []
    for tool in base:
        if tool in seen:
            continue
        seen.add(tool)
        if tool in NETWORK_TOOLS and not network:
            continue
        if tool in WRITE_TOOLS and role in _READONLY_ROLES \
                and not allow_write:
            continue
        out.append(tool)
    return tuple(out)


def _canonical(worktree: str, raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = Path(worktree) / p
    return p.resolve(strict=False)  # symlink 逃逸经 resolve 展开后暴露


def _path_inside(worktree: str, resolved: Path) -> str | None:
    """resolved 在 worktree 内 → 返回其相对 posix 路径；否则 None。"""
    wt = os.path.normcase(str(Path(worktree).resolve()))
    rs = os.path.normcase(str(resolved))
    if not (rs == wt or rs.startswith(wt + os.sep)):
        return None
    rel = os.path.relpath(str(resolved), str(Path(worktree).resolve()))
    return Path(rel).as_posix()


def decide_tool(
    role: str,
    *,
    tool_name: str,
    tool_input: dict,
    worktree_path: str,
    allowed_files: tuple[str, ...],
    bash_allowlist: tuple[str, ...] = (),
    deny_network: bool = True,
) -> ToolDecision:
    """判定序（确定性）：非法工具名拒 → role 只读拒写 → 网络拒 →
    Bash 白名单 → 写工具 canonical → 其它允许。"""
    _require(role in ROLE_IDS, f"unknown role: {role}")
    if not isinstance(tool_name, str) or not tool_name:
        return ToolDecision(False, "invalid-tool")
    if role in _READONLY_ROLES and tool_name in WRITE_TOOLS:
        return ToolDecision(False, "role-read-only")  # #12
    if tool_name in NETWORK_TOOLS and deny_network:
        return ToolDecision(False, "network-disabled")  # #30
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) \
            else None
        if isinstance(command, str) and command in bash_allowlist:
            return ToolDecision(True, "ok")
        return ToolDecision(False, "bash-disabled")  # M0 不暴露通用 Bash
    if tool_name in WRITE_TOOLS:
        raw = tool_input.get("file_path") if isinstance(tool_input, dict) \
            else None
        if not isinstance(raw, str) or not raw:
            return ToolDecision(False, "no-path")
        resolved = _canonical(worktree_path, raw)
        rel = _path_inside(worktree_path, resolved)
        if rel is None:
            return ToolDecision(False, "traversal")  # #10 越界/symlink 逃逸
        allowed = {os.path.normcase(Path(f).as_posix()) for f in allowed_files}
        if os.path.normcase(rel) not in allowed:
            return ToolDecision(False, "out-of-scope")
        return ToolDecision(True, "ok")
    return ToolDecision(True, "ok-other")  # 读工具只读自由
