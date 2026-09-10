"""AOTF git 子进程运行器（M0-B1）。

机械 git 调用底座：以给定仓库为 cwd、无 shell、list argv 调真实 git，
成功返回 stdout 文本；非零退出或超时抛 GitCommandError。本层固定 env：

- GIT_OPTIONAL_LOCKS=0：只读（禁 index 刷新等可选锁，不写 .git 任何字节）；
- GIT_TERMINAL_PROMPT=0：不挂死（禁凭据/交互提示）；
- LC_ALL=C / LANG=C：输出稳定可解析。

设计裁决：git 命令失败是机械层事件，不属于冻结 16 错误码（状态机/事务
域）。故定义独立领域异常 GitCommandError，不扩展 ErrorCode、不冒充
AotfError。state/DB/事件层不感知本模块。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

__all__ = ["GitCommandError", "git_executable", "run_git"]

_READONLY_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
    "LANG": "C",
}


class GitCommandError(Exception):
    """git 命令机械失败（非状态机错误；不携带 ErrorCode）。

    - argv：实际执行的参数（含 git 可执行路径）；
    - returncode：子进程退出码；None 仅当 git 缺失或超时（timed_out=True）；
    - stdout/stderr：原始 bytes；
    - timed_out：是否超时终止。
    """

    def __init__(
        self,
        argv: tuple[str, ...],
        returncode: int | None,
        stdout: bytes,
        stderr: bytes,
        *,
        timed_out: bool = False,
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        tail = tail[-1] if tail else ""
        head = "git " + " ".join(argv[1:]) if len(argv) > 1 else "git"
        if timed_out:
            message = f"{head} timed out"
        elif returncode is None:
            message = f"{head} unavailable: {tail or 'git executable not found'}"
        else:
            message = f"{head} exited rc={returncode}"
            if tail:
                message += f": {tail}"
        super().__init__(message)


def git_executable() -> str:
    """返回 git 可执行路径；未安装抛 GitCommandError(returncode=None)。"""
    exe = shutil.which("git")
    if exe is None:
        raise GitCommandError((), None, b"", b"git executable not found")
    return exe


def run_git(repo: str | Path, *args: str, timeout: float = 30.0) -> str:
    """以 repo 为 cwd 运行 git，返回解码 stdout（rstrip 单个尾部换行）。

    固定注入 ``-c core.quotepath=false``：diff/ls-files 等路径输出保持原始
    UTF-8，不被 C-quote 转义（中文/非 ASCII 文件名数据保真）。

    非零退出抛 GitCommandError；超时抛 GitCommandError(timed_out=True)。
    """
    exe = git_executable()
    argv = (exe, "-c", "core.quotepath=false", *args)
    env = dict(os.environ)
    env.update(_READONLY_ENV)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            argv,
            cwd=str(Path(repo)),
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(
            argv, None, exc.stdout or b"", exc.stderr or b"",
            timed_out=True,
        ) from exc
    if proc.returncode != 0:
        raise GitCommandError(argv, proc.returncode, proc.stdout, proc.stderr)
    return proc.stdout.decode("utf-8", "surrogateescape").rstrip("\n")
