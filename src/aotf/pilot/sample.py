"""AOTF M0-E2 pilot 样例仓生成器（samplelib demo 库）。

生成一个受控**非生产**样例仓（spec §19「真实试点」目标仓；owner 指定
D:\\tools\\aotf-e2e-sample，独立于 aotf 冻结清单、仅该仓可 commit）：

- src/samplelib/*.py：文本/统计工具 demo 库（确定性内容，含实现与 docstring）；
- tests/test_*.py：每模块 pytest（生成器按用例数据生成，基线全绿）；
- conftest.py：把 src 注入 sys.path（pytest 无需安装即可 import samplelib）；
- git init -b main + 首次 commit（clean）。

幂等：root 已存在且 clean → 返回现状（不覆盖、不重建）；存在但 dirty /
unborn / 非仓（且非空）→ raise。规模说明：taskbook §1.1 引用 spec 建议
5k–20k 行；生成器如实统计并报告，**不强凑装饰性空行**（E2 验收以真实规模
为准）。样例仓服务于 M0-E2 三任务试点（slugify/normalize/cleaning 上做
加功能/修 bug/重构），demo 库规模足够承载这些改动。

行数统计口径：物理行 = 文件 splitlines 计数（walk 排除 .git）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aotf.git.base import run_git
from aotf.git.preflight import preflight

__all__ = ["SAMPLE_ROOT", "SampleInfo", "generate_sample"]

SAMPLE_ROOT = r"D:\tools\aotf-e2e-sample"


@dataclass(frozen=True, slots=True)
class SampleInfo:
    """样例仓生成/幂等返回信息。"""

    root: str
    head_commit: str
    head_tree: str
    file_count: int
    lines: int


# ---------------------------------------------------------------------------
# 样例库内容（确定性；normalize 与 slugify 各自内联 _strip_control —— t3 重构点）
# 外层 r'''：内容 docstring 全用三双引号（不产生 '''，不截断外层）。
# 反斜杠按字面写入样例源码（r 前缀），保证 \r \n \ufeff 等为样例库源码字面。
# ---------------------------------------------------------------------------

_CONTENT: dict[str, str] = {
    "cleaning": r'''
"""samplelib cleaning：文本清洗助手（demo 样例库）。

提供 BOM 剥离 / 制表符展开 / 连续空行折叠等确定性工具。
"""


def strip_bom(text):
    """去掉 UTF-8 BOM 前缀。"""
    return text[1:] if text.startswith('\ufeff') else text


def expand_tabs(text, tabstop=8):
    """按 tabstop 展开制表符为空格。"""
    return text.expandtabs(tabstop)


def collapse_blank_lines(text, *, max_blank=1):
    """把连续空行折叠到至多 max_blank 个（段落分隔保持）。"""
    out = []
    blank = 0
    for line in text.split("\n"):
        if not line.strip():
            blank += 1
            if blank <= max_blank:
                out.append("")
        else:
            blank = 0
            out.append(line)
    return "\n".join(out).strip("\n")
''',
    "normalize": r'''
"""samplelib normalize：文本规范化（demo 样例库）。

统一换行到 LF 并剥离行尾空白、折叠多余段落空行。
"""


def normalize(text, *, collapse=True):
    """规范化文本：统一换行 + 逐行去尾空白 + 折叠段落空行。

    注：裸 CR 换行（旧 Mac）未归一（t2 修复点）；CRLF 与 LF 已统一。
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    s = _strip_control(text)
    s = s.replace("\r\n", "\n")
    lines = [line.rstrip() for line in s.split("\n")]
    if collapse:
        kept = []
        blank = 0
        for line in lines:
            if not line:
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            kept.append(line)
        lines = kept
    return "\n".join(lines).strip("\n")


def _strip_control(text):
    """剥离控制字符（与 slugify 内联重复——t3 抽取点）。"""
    return "".join(ch for ch in text if ch >= " " or ch == "\n")
''',
    "slugify": r'''
"""samplelib slugify：URL/文件名友好 slug 生成（demo 样例库）。

示例：slugify("Hello, World!") -> "hello-world"。
"""

import re


def slugify(text, *, separator="-", lowercase=True):
    """转 slug：剥离控制字符、折叠空白、非词字符替换为 separator。"""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    s = _strip_control(text)
    s = " ".join(s.split())
    if lowercase:
        s = s.lower()
    pattern = r"[^a-z0-9_]+" if lowercase else r"[^A-Za-z0-9_]+"
    return re.sub(pattern, separator, s).strip(separator)


def _strip_control(text):
    """剥离控制字符（与 normalize 内联重复——t3 抽取点）。"""
    return "".join(ch for ch in text if ch >= " " or ch == "\n")
''',
    "casing": r'''
"""samplelib casing：命名风格转换（demo 样例库）。

在 snake_case / kebab-case 间按词边界转换。
"""

import re


def to_snake_case(text):
    """转 snake_case：词边界（camelCase 或分隔符）→ 下划线。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_").lower()


def to_kebab_case(text):
    """转 kebab-case：词边界 → 连字符。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s)
    return s.strip("-").lower()
''',
    "stats": r'''
"""samplelib stats：文本统计（demo 样例库）。"""


def word_count(text):
    """按空白分词计词数。"""
    return len(text.split())


def char_count(text):
    """字符数。"""
    return len(text)


def line_count(text):
    """行数（splitlines 语义）。"""
    return len(text.splitlines())


def sentence_count(text):
    """粗略句数（. ! ? 计数）。"""
    return sum(1 for ch in text if ch in ".!?")
''',
}

# 公共函数 + 用例（测试由生成器生成；expected 支持 int/str）
_PUBLIC: dict[str, tuple[tuple[str, tuple[tuple[str, object], ...]], ...]] = {
    "cleaning": (
        ("strip_bom", (("\ufeffabc", "abc"), ("plain", "plain"))),
        ("expand_tabs", (("a\tb", "a       b"), ("no-tab", "no-tab"))),
        ("collapse_blank_lines",
         (("a\n\n\n\nb", "a\n\nb"), ("a\n\nb", "a\n\nb"))),
    ),
    "normalize": (
        ("normalize", (("a\n\n\n\n\nb", "a\n\nb"), ("  x  \n", "  x"),
                       ("a\r\nb", "a\nb"))),
    ),
    "slugify": (
        ("slugify", (("Hello, World!", "hello-world"), ("  A--B  ", "a-b"),
                     ("Under_Score", "under_score"))),
    ),
    "casing": (
        ("to_snake_case", (("HelloWorld", "hello_world"),
                           ("foo bar", "foo_bar"))),
        ("to_kebab_case", (("HelloWorld", "hello-world"),
                           ("foo_bar", "foo-bar"))),
    ),
    "stats": (
        ("word_count", (("a b  c", 3), ("", 0))),
        ("char_count", (("abc", 3), ("", 0))),
        ("line_count", (("a\nb\nc", 3), ("", 0))),
        ("sentence_count", (("Hi! Bye.", 2), ("", 0))),
    ),
}

_MODULE_ORDER = ("cleaning", "normalize", "slugify", "casing", "stats")

_CONFTEST = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent / \"src\"))\n"
)

_INIT = (
    '"""samplelib —— AOTF M0-E2 试点样例仓库（demo 文本/统计工具库）。"""\n'
    "\n"
    "from samplelib.casing import to_kebab_case, to_snake_case\n"
    "from samplelib.cleaning import (collapse_blank_lines, expand_tabs,\n"
    "                                strip_bom)\n"
    "from samplelib.normalize import normalize\n"
    "from samplelib.slugify import slugify\n"
    "from samplelib.stats import (char_count, line_count, sentence_count,\n"
    "                             word_count)\n"
    "\n"
    "__version__ = \"0.1.0\"\n"
    "\n"
    "__all__ = [\n"
    "    \"collapse_blank_lines\", \"expand_tabs\", \"strip_bom\",\n"
    "    \"normalize\", \"slugify\", \"to_kebab_case\", \"to_snake_case\",\n"
    "    \"char_count\", \"line_count\", \"sentence_count\", \"word_count\",\n"
    "    \"__version__\",\n"
    "]\n"
)


def _test_source(module: str) -> str:
    # importlib：samplelib.__init__ 重导出同名函数（normalize/slugify）会遮蔽子模块
    lines = [f'"""samplelib {module} 模块测试（由 pilot.sample 生成）。"""',
             "", "import importlib",
             f"_m = importlib.import_module('samplelib.{module}')", ""]
    for name, cases in _PUBLIC[module]:
        for index, (arg, expected) in enumerate(cases):
            lines.append(f"def test_{name}_case{index}():")
            lines.append(f"    assert _m.{name}({arg!r}) == {expected!r}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _sample_files() -> dict[str, str]:
    files: dict[str, str] = {"conftest.py": _CONFTEST,
                             "src/samplelib/__init__.py": _INIT}
    for module in _MODULE_ORDER:
        files[f"src/samplelib/{module}.py"] = _CONTENT[module].lstrip("\n")
        files[f"tests/test_{module}.py"] = _test_source(module)
    return files


def _stats(root: Path) -> tuple[int, int]:
    file_count = lines = 0
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        file_count += 1
        lines += len(path.read_text(encoding="utf-8").splitlines())
    return file_count, lines


def _head(root: Path) -> tuple[str, str]:
    commit = run_git(root, "rev-parse", "--verify", "HEAD").strip()
    tree = run_git(root, "rev-parse", "--verify", "HEAD^{tree}").strip()
    return commit, tree


def generate_sample(
    root: str | Path = SAMPLE_ROOT, *, seed: str = "samplelib",
) -> SampleInfo:
    """生成/幂等返回样例仓。内容确定性（单模板，seed 保留兼容位）。"""
    target = Path(os.path.abspath(os.fspath(root)))
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"sample root is not a directory: {target}")
        report = preflight(target)
        if report.verdict == "clean":
            head_commit, head_tree = _head(target)
            file_count, lines = _stats(target)
            return SampleInfo(report.root, head_commit, head_tree,
                              file_count, lines)
        if report.verdict != "not_a_repo" or any(target.iterdir()):
            raise ValueError(
                f"sample root not idempotent (verdict={report.verdict}): "
                f"{target}")
    else:
        target.mkdir(parents=True)
    for rel, content in _sample_files().items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    run_git(target, "init", "-b", "main")
    run_git(target, "config", "user.name", "aotf-sample")
    run_git(target, "config", "user.email", "aotf-sample@example.invalid")
    run_git(target, "config", "core.autocrlf", "false")
    run_git(target, "config", "commit.gpgsign", "false")
    run_git(target, "add", "--", ".")
    run_git(target, "commit", "-m", "samplelib baseline")
    head_commit, head_tree = _head(target)
    file_count, lines = _stats(target)
    return SampleInfo(os.path.normpath(str(target)), head_commit, head_tree,
                      file_count, lines)
