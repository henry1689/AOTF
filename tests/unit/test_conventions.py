"""约定守卫：把跨平台与文档一致性惯例从「靠自觉」升级为机械约束。

背景（F1-b 根因）：
    Windows 文本模式会把 ``\\n`` 写成 ``\\r\\n``。若测试用 ``write_text`` 构造
    「字节精确」的期望值，同一提交在 Windows 与 Linux 下结果不同——
    ``test_controller_create_and_delete`` 即此缺陷（preimage hash mismatch）。
    修一处不够：只要惯例没有机械约束，同类问题必然复发。

设计裁决：
    检测逻辑（``_write_text_calls_without_newline``）与断言分离，使其可被
    反例自检——守卫本身若被改宽松，反例用例会立刻失败，不会静默失效。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent.parent

_READ_ENCODING = "utf-8"

#: 行内豁免标记。仅当某处确实需要平台相关的换行转换时才可使用；
#: 使用后 ``test_no_silent_eol_exemptions`` 会失败，强制豁免变成一次
#: 显式的、可评审的测试改动，而不是静默放宽。
EXEMPT_MARKER = "# eol-exempt:"


def _scan_write_text_calls(source: str) -> tuple[list[int], list[int]]:
    """扫描 write_text 调用，返回 ``(缺少 newline 的行号, 因豁免跳过的行号)``。

    只检测 AST 中真实的调用节点：字符串字面量或注释里出现的同名文本不会误报，
    也不会被误判为豁免——豁免只对「带标记的 write_text 调用行」生效。
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    offenders: list[int] = []
    exempted: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(
            node.func, "id", None)
        if name != "write_text":
            continue
        if any(keyword.arg == "newline" for keyword in node.keywords):
            continue
        text = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        if EXEMPT_MARKER in text:
            exempted.append(node.lineno)
            continue
        offenders.append(node.lineno)
    return offenders, exempted


def _write_text_calls_without_newline(source: str) -> list[int]:
    """兼容入口：仅返回缺少 newline 的行号。"""
    return _scan_write_text_calls(source)[0]


def _iter_test_sources():
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        yield path, path.read_text(encoding=_READ_ENCODING)


# ── 主守卫 ───────────────────────────────────────────────────────────

def test_no_write_text_without_newline_in_tests() -> None:
    """测试代码中任何 write_text 都必须显式声明换行策略。"""
    offenders = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{lineno}"
        for path, source in _iter_test_sources()
        for lineno in _write_text_calls_without_newline(source)
    ]
    assert not offenders, (
        'write_text 缺少 newline=""：Windows 会写出 \\r\\n，'
        "使「字节精确」断言跨平台不稳定。请改用 write_bytes(b\"...\") "
        "或补 newline=\"\"。违反点：\n  " + "\n  ".join(offenders)
    )


def test_no_silent_eol_exemptions() -> None:
    """豁免必须显式化：当前不允许任何静默豁免。

    只统计「因豁免标记而被跳过的 write_text 调用行」——守卫文件自身在注释与
    文档字符串里描述该标记，不构成豁免。若确需豁免，应连同本断言一起修改，
    使豁免成为一次可评审的测试改动。
    """
    marked = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{lineno}"
        for path, source in _iter_test_sources()
        for lineno in _scan_write_text_calls(source)[1]
    ]
    assert not marked, (
        f"检测到换行豁免 {EXEMPT_MARKER!r}：{marked}。"
        "豁免须同步更新本断言并说明理由。"
    )


# ── 反例自检：守卫本身必须被证明有效 ─────────────────────────────────

def test_detector_flags_missing_newline() -> None:
    source = 'p.write_text("x\\n", encoding="utf-8")\n'
    assert _write_text_calls_without_newline(source) == [1]


def test_detector_accepts_declared_newline() -> None:
    source = 'p.write_text("x\\n", encoding="utf-8", newline="")\n'
    assert _write_text_calls_without_newline(source) == []


def test_detector_accepts_byte_exact_writer() -> None:
    source = 'p.write_bytes(b"x\\n")\n'
    assert _write_text_calls_without_newline(source) == []


def test_detector_ignores_string_literal_mentions() -> None:
    """字面量里提到 write_text 不应误报（本守卫文件自身即含此类文本）。"""
    source = 'DOC = "call p.write_text(\\"x\\") somewhere"\n'
    assert _write_text_calls_without_newline(source) == []


def test_detector_respects_exempt_marker() -> None:
    source = f'p.write_text("x", encoding="utf-8")  {EXEMPT_MARKER} 仅样例\n'
    assert _write_text_calls_without_newline(source) == []


# ── README 与测试文件同步守卫 ────────────────────────────────────────

def test_readme_test_command_covers_all_test_files() -> None:
    """README 的全量测试命令必须与实际测试文件集合一致。

    手工枚举一旦漂移，新增测试就不会被文档命令执行，而「全绿」仍会成立。
    """
    readme = (PROJECT_ROOT / "README.md").read_text(encoding=_READ_ENCODING)
    on_disk = {path.name for path in TESTS_ROOT.glob("test_*.py")}
    listed = set(re.findall(r"tests/unit/(test_[a-z0-9_]+\.py)", readme))
    assert not (on_disk - listed), (
        f"README 测试命令未覆盖：{sorted(on_disk - listed)}"
    )
    assert not (listed - on_disk), (
        f"README 测试命令引用了不存在的文件：{sorted(listed - on_disk)}"
    )
