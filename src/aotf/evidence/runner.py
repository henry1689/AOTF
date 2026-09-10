"""AOTF EvidenceRunner（M0-C2）：subprocess 隔离执行 + 命令注册表。

spec §19 M0-C / §9.3：把「按 command_id 解析注册命令 → 在 task worktree
用子进程隔离执行 → 产出 EvidenceCheck 运行事实 + stdout/stderr 原始 bytes」
变成确定性、可审计的机器动作。

隔离（S1）：shell=False（list argv，无 shell 拆分/注入）；argv 只来自
build_registry 的 allowlist；cwd 限于 worktree 下相对路径；不注入 shell/
网络代理；Windows CREATE_NO_WINDOW。错误/超时以 status='error' 的
EvidenceCheck 事实呈现（不 raise，证据面不留半截；Policy 可判）。

哨兵退出码（文档化）：TIMEOUT_EXIT=-1（超时）、NOT_FOUND_EXIT=-2
（argv[0]/cwd 不存在）。

注意：与 aotf.runner（M0-A AgentRunner 协议）同名异包，勿混淆；本模块
是「证据命令执行器」，AgentRunner 是「LLM Agent 运行协议」。runner
identity/host_fingerprint/时间、tree/delta 绑定、stdout/stderr 落 artifact
（B6）与 TestEvidenceRecord 组装归 C3。仅 stdlib + 引用 schema 值对象
（单向无环）。

补充注意点：
- 子进程只继承 PATH/系统根/临时目录/locale/Python 编码等最小环境，并显式
  禁止 user-site 与 Git 交互提示；API key/token/cookie/proxy 不下传；
- 捕获子进程原始字节：Windows 文本管道 print() 产出 CRLF（Linux LF），
  stdout_sha 即捕获字节事实，跨 OS 会不同（证据记录原样，不做归一化）；
- spec 应取自 build_registry、主 cwd 参数应为 worktree 绝对根（隔离闸门
  在调用方）。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import time as _time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from aotf.evidence.schema import EvidenceCheck, EvidenceError

__all__ = [
    "TIMEOUT_EXIT",
    "NOT_FOUND_EXIT",
    "CommandSpec",
    "build_registry",
    "EvidenceRunResult",
    "run_command",
]

# 证据命令只继承启动进程所需的最小环境。尤其不得把 API key、token、
# cookie 或代理凭据交给被测项目代码。
_SAFE_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV",
})

TIMEOUT_EXIT = -1
NOT_FOUND_EXIT = -2

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def _check_token(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise EvidenceError(f"invalid {name} token")
    if value in (".", ".."):
        raise EvidenceError(f"invalid {name} token")
    if not _TOKEN_RE.fullmatch(value):
        raise EvidenceError(f"invalid {name} token")


def _require_relative_cwd(cwd: object) -> None:
    if not isinstance(cwd, str):
        raise EvidenceError("invalid cwd")
    p = Path(cwd)
    if not cwd or p.is_absolute() or p.root or ".." in p.parts:
        raise EvidenceError("invalid cwd")


def _check_timeout(t: object, name: str = "timeout") -> None:
    if (isinstance(t, bool) or not isinstance(t, (int, float))
            or not math.isfinite(t) or t <= 0):
        raise EvidenceError(f"{name} must be a finite positive number")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _minimal_env() -> dict[str, str]:
    allowed = {key.casefold() for key in _SAFE_ENV_KEYS}
    env = {key: value for key, value in os.environ.items()
           if key.casefold() in allowed}
    env["PYTHONNOUSERSITE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """注册表内的一条命令（argv allowlist，无自由 shell 字符串）。"""

    command_id: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout: float = 30.0

    def __post_init__(self) -> None:
        _check_token("command_id", self.command_id)
        if not isinstance(self.argv, tuple) or not self.argv:
            raise EvidenceError("argv must be non-empty tuple")
        if not all(isinstance(a, str) and a for a in self.argv):
            raise EvidenceError("argv entries must be non-empty str")
        _require_relative_cwd(self.cwd)
        _check_timeout(self.timeout)


def build_registry(specs: tuple[CommandSpec, ...]) -> Mapping[str, CommandSpec]:
    """构造只读命令注册表（command_id 唯一）。"""
    out: dict[str, CommandSpec] = {}
    for spec in specs:
        if spec.command_id in out:
            raise EvidenceError(f"duplicate command_id: {spec.command_id}")
        out[spec.command_id] = spec
    return MappingProxyType(out)


@dataclass(frozen=True, slots=True)
class EvidenceRunResult:
    """一次 run 的事实 + 捕获字节（bytes 供 C3 经 B6 ingest）。"""

    check: EvidenceCheck
    stdout: bytes
    stderr: bytes


def run_command(
    cwd: str | Path,
    spec: CommandSpec,
    *,
    check_id: str,
    timeout: float | None = None,
) -> EvidenceRunResult:
    """在 cwd/spec.cwd 下隔离执行注册命令，返回运行事实。

    成功/非零/超时/缺失都以 EvidenceCheck status∈{passed,failed,error} 事实
    呈现（不 raise）；cwd 主参数非目录抛 ValueError。
    """
    _check_token("check_id", check_id)
    if timeout is not None:
        _check_timeout(timeout, "timeout override")
    work = Path(cwd)
    if not work.exists() or not work.is_dir():
        raise ValueError(f"cwd not found or not a directory: {cwd}")
    exec_cwd = work if spec.cwd == "." else work / spec.cwd
    t = timeout if timeout is not None else spec.timeout

    stdout = b""
    stderr = b""
    duration_ms = 0
    if not exec_cwd.exists() or not exec_cwd.is_dir():
        rc, status = NOT_FOUND_EXIT, "error"
        stderr = f"cwd not found: {exec_cwd}".encode("utf-8")
    else:
        start = _time.perf_counter()
        try:
            proc = subprocess.run(
                list(spec.argv),
                cwd=str(exec_cwd),
                capture_output=True,
                shell=False,
                env=_minimal_env(),
                timeout=t,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stdout, stderr = proc.stdout, proc.stderr
            rc = proc.returncode
            status = "passed" if rc == 0 else "failed"
        except subprocess.TimeoutExpired:
            rc, status = TIMEOUT_EXIT, "error"
            stderr = f"timed out after {t}s".encode("utf-8")
        except FileNotFoundError:
            rc, status = NOT_FOUND_EXIT, "error"
            # 固定 ASCII，防 locale 漂移（同错误跨机器 stderr_sha 稳定）
            stderr = b"FileNotFoundError: command or cwd not found"
        except OSError:
            rc, status = NOT_FOUND_EXIT, "error"
            stderr = b"OSError: cannot execute command"
        finally:
            duration_ms = max(
                0, int(round((_time.perf_counter() - start) * 1000))
            )

    check = EvidenceCheck(
        check_id=check_id,
        command_id=spec.command_id,
        cwd=spec.cwd,
        exit_code=rc,
        stdout_sha256=_sha(stdout),
        stderr_sha256=_sha(stderr),
        duration_ms=duration_ms,
        status=status,
    )
    return EvidenceRunResult(check=check, stdout=stdout, stderr=stderr)
