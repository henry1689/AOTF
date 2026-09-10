"""M0-C2 EvidenceRunner（subprocess 隔离执行）测试。

命令统一 [sys.executable, "-c", ...]（跨平台确定性）；cwd 全在 tmp_path；
错误/超时以 status=error 事实呈现（不 raise）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

import aotf.evidence.schema as evschema
from aotf.evidence.runner import (
    NOT_FOUND_EXIT,
    TIMEOUT_EXIT,
    CommandSpec,
    EvidenceRunResult,
    build_registry,
    run_command,
)
from aotf.evidence.schema import EvidenceError


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _py(code: str) -> CommandSpec:
    return CommandSpec(command_id="py", argv=(sys.executable, "-c", code))


def _spec(command_id="unit", code="print('ok')", **kw) -> CommandSpec:
    base = dict(command_id=command_id, argv=(sys.executable, "-c", code))
    base.update(kw)
    return CommandSpec(**base)


def _lf(data: bytes) -> bytes:
    # Windows 子进程文本管道 print() 输出 CRLF；归一化后再比内容。
    return data.replace(b"\r\n", b"\n")


def test_run_success_passed_stdout(tmp_path) -> None:
    res = run_command(tmp_path, _spec(code="print('hello')"),
                      check_id="unit")
    assert isinstance(res, EvidenceRunResult)
    assert _lf(res.stdout) == b"hello\n"
    assert res.stderr == b""
    assert res.check.status == "passed"
    assert res.check.exit_code == 0
    assert res.check.command_id == "unit"
    assert res.check.check_id == "unit"
    assert res.check.stdout_sha256 == _sha(res.stdout)


def test_run_stderr_only(tmp_path) -> None:
    res = run_command(tmp_path, _spec(code="import sys;"
                                        "sys.stderr.write('e!')"),
                      check_id="unit")
    assert res.stderr == b"e!"
    assert res.stdout == b""
    assert res.check.status == "passed"


def test_run_nonzero_exit_failed(tmp_path) -> None:
    res = run_command(tmp_path, _spec(code="import sys;sys.exit(3)"),
                      check_id="unit")
    assert res.check.exit_code == 3
    assert res.check.status == "failed"


def test_run_empty_output(tmp_path) -> None:
    res = run_command(tmp_path, _spec(code="pass"), check_id="unit")
    assert res.stdout == b""
    assert res.stderr == b""
    assert res.check.stdout_sha256 == _sha(b"")
    assert res.check.stderr_sha256 == _sha(b"")


def test_run_unicode_stdout_bytes(tmp_path) -> None:
    code = ("import sys;"
            "sys.stdout.buffer.write('你好'.encode('utf-8'))")
    res = run_command(tmp_path, _spec(code=code), check_id="unit")
    assert res.stdout == "你好".encode("utf-8")
    assert res.check.stdout_sha256 == _sha("你好".encode("utf-8"))


def test_relative_cwd_effective(tmp_path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "m.txt").write_bytes(b"marker\n")
    spec = _spec(command_id="unit",
                 code="import sys;print(open('m.txt').read().strip())",
                 cwd="sub")
    res = run_command(tmp_path, spec, check_id="unit")
    assert _lf(res.stdout) == b"marker\n"
    assert res.check.cwd == "sub"


def test_timeout_error_sentinel(tmp_path) -> None:
    spec = _spec(command_id="unit",
                 code="import time;time.sleep(5)",
                 timeout=0.1)
    res = run_command(tmp_path, spec, check_id="unit")
    assert res.check.status == "error"
    assert res.check.exit_code == TIMEOUT_EXIT
    assert b"timed out" in res.stderr


def test_argv0_missing_error_sentinel(tmp_path) -> None:
    spec = CommandSpec(command_id="unit",
                       argv=("aotf-cmd-does-not-exist-xyz-12345",))
    res = run_command(tmp_path, spec, check_id="unit")
    assert res.check.status == "error"
    assert res.check.exit_code == NOT_FOUND_EXIT
    assert res.stderr


def test_missing_cwd_error_fact(tmp_path) -> None:
    spec = _spec(command_id="unit", code="pass", cwd="nope")
    res = run_command(tmp_path, spec, check_id="unit")
    assert res.check.status == "error"
    assert res.check.exit_code == NOT_FOUND_EXIT
    assert b"cwd not found" in res.stderr


def test_command_spec_validation() -> None:
    with pytest.raises(EvidenceError):
        CommandSpec(command_id="..", argv=(sys.executable,))
    with pytest.raises(EvidenceError):
        CommandSpec(command_id="unit", argv=())
    with pytest.raises(EvidenceError):
        CommandSpec(command_id="unit", argv=(sys.executable, ""))
    with pytest.raises(EvidenceError, match="cwd"):
        CommandSpec(command_id="unit", argv=(sys.executable,), cwd="/abs")
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(EvidenceError, match="timeout"):
            CommandSpec(command_id="unit", argv=(sys.executable,),
                        timeout=bad)
    with pytest.raises(EvidenceError, match="timeout"):
        CommandSpec(command_id="unit", argv=(sys.executable,), cwd=".",
                    timeout="1")


def test_build_registry_unique() -> None:
    specs = (_spec("a"), _spec("b"), _spec("a"))
    with pytest.raises(EvidenceError, match="duplicate"):
        build_registry(specs)
    reg = build_registry((_spec("a"), _spec("b")))
    assert reg["a"].command_id == "a"


def test_build_registry_readonly() -> None:
    reg = build_registry((_spec("a"),))
    with pytest.raises(TypeError):
        reg["b"] = reg["a"]  # type: ignore[index]


def test_shell_false_no_injection(tmp_path) -> None:
    payload = "a; echo pwn"
    spec = CommandSpec(
        command_id="unit",
        argv=(sys.executable, "-c",
              "import sys;print(repr(sys.argv[1]))", payload),
    )
    res = run_command(tmp_path, spec, check_id="unit")
    out = _lf(res.stdout)
    assert res.check.status == "passed"
    # payload 原样作为单个 argv 传递（无 shell 拆分/执行 echo）
    assert out == repr(payload).encode("utf-8") + b"\n"
    assert b"\npwn\n" not in out


def test_check_sha_matches_captured(tmp_path) -> None:
    res = run_command(tmp_path, _spec(code="print('x' * 100)"),
                      check_id="unit")
    assert res.check.stdout_sha256 == _sha(res.stdout)
    assert res.check.stderr_sha256 == _sha(res.stderr)


def test_duration_nonnegative_int(tmp_path) -> None:
    res = run_command(tmp_path, _spec(code="pass"), check_id="unit")
    assert isinstance(res.check.duration_ms, int)
    assert res.check.duration_ms >= 0


def test_run_command_missing_cwd_valueerror(tmp_path) -> None:
    with pytest.raises(ValueError):
        run_command(tmp_path / "nope", _spec(code="pass"), check_id="unit")
    with pytest.raises(EvidenceError, match="timeout"):
        run_command(tmp_path, _spec(code="pass"), check_id="unit",
                    timeout=float("nan"))


def test_multiple_commands_registry(tmp_path) -> None:
    reg = build_registry((_spec("a", code="print('A')"),
                          _spec("b", code="print('B')")))
    ra = run_command(tmp_path, reg["a"], check_id="c1")
    rb = run_command(tmp_path, reg["b"], check_id="c2")
    assert _lf(ra.stdout) == b"A\n"
    assert _lf(rb.stdout) == b"B\n"
    assert ra.check.command_id == "a"
    assert rb.check.command_id == "b"


def test_subprocess_gets_minimal_environment_without_secrets(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AOTF_TEST_SECRET_TOKEN", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak-either")
    code = (
        "import os;"
        "print(os.environ.get('AOTF_TEST_SECRET_TOKEN', 'absent'));"
        "print(os.environ.get('ANTHROPIC_API_KEY', 'absent'));"
        "print('path' if os.environ.get('PATH') else 'no-path')"
    )
    res = run_command(tmp_path, _spec(code=code), check_id="unit")
    assert _lf(res.stdout) == b"absent\nabsent\npath\n"
