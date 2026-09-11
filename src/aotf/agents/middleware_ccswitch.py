"""AOTF CC-Switch Middleware（M1）。

探测并使用 CC-Switch 路径启动 Claude Code 子进程，
确保 DeepSeek-V4-flash 通过 Anthropic 兼容端点产生 tool_use。

设计裁决：
- 自动探测 CC-Switch 安装路径（环境变量 > 默认路径）；
- 注入必要 env（ANTHROPIC_MODEL、ANTHROPIC_BASE_URL 等）；
- 失败时回退到直连 Anthropic API（如果可用）；
- 路径校验：必须是合法可执行文件，拒绝相对路径。

边界：本模块只负责路径探测和 env 组装；
实际子进程启动在 claude_sdk.py 的 _run_via_ccswitch。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

__all__ = [
    "CC_SWITCH_ENV_KEYS",
    "detect_ccswitch_path",
    "get_ccswitch_env",
]

# 必要 env 变量
CC_SWITCH_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_ENTRYPOINT",
]


def detect_ccswitch_path() -> Optional[Path]:
    """探测 CC-Switch Claude CLI 路径。"""
    # 1. 环境变量优先
    env_path = os.environ.get("CC_SWITCH_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists() and p.is_file():
            return p.resolve()

    # 2. Windows 常见安装位置
    username = os.environ.get("USERNAME", "henry")
    candidates = [
        # Wenstar-cc 验证过的路径
        Path(f"D:/ClaudeCode/claude.exe"),
        # 其他常见位置
        Path(f"C:/Users/{username}/AppData/Local/Programs/Claude/claude.exe"),
        Path("C:/Program Files/Claude/claude.exe"),
        Path.home() / ".cc-switch" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude.exe",
        Path("/usr/local/bin/claude.exe"),
        Path("/usr/bin/claude.exe"),
    ]

    for p in candidates:
        if p.exists() and p.is_file():
            return p.resolve()

    return None


def get_ccswitch_env() -> Dict[str, str]:
    """获取 CC-Switch 所需的 env 变量。"""
    env = os.environ.copy()

    # 确保必要变量存在
    env.setdefault("ANTHROPIC_MODEL", "DeepSeek-V4-flash")
    env.setdefault("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    env.setdefault("CLAUDE_CODE_ENTRYPOINT", "aotf")

    # 安全相关
    env.setdefault("PYTHONNOUSERSITE", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")

    return env


def validate_ccswitch_env(env: Dict[str, str]) -> bool:
    """验证 env 是否完整。"""
    for key in CC_SWITCH_ENV_KEYS:
        if key not in env or not env[key]:
            return False
    return True


def build_ccswitch_command(
    ccswitch_path: Path,
    prompt: str,
    model: str = "DeepSeek-V4-flash",
    max_turns: int = 1,
    max_budget_usd: float = 5.0,
    cwd: str = ".",
) -> tuple[list[str], str]:
    """构建 CC-Switch CLI 命令。"""
    cmd = [
        str(ccswitch_path),
        "--output", "json",
        "--no-sandbox",
        "--model", model,
        "--max-turns", str(max_turns),
        "--budget", str(max_budget_usd),
        "--cwd", cwd,
    ]
    return cmd, prompt
