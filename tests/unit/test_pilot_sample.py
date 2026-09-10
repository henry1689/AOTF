"""M0-E2 pilot 样例仓生成器测试（tmp 内生成，真实 git + pytest 基线绿）。

生成器在 tmp_path 下产出 samplelib demo 库仓：含 git init+首次 commit
（clean）；生成库可在仓根被子进程 pytest 全绿；幂等（clean 复用）与
dirty 拒绝语义逐条断言。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from aotf.pilot.sample import generate_sample


def _generate(tmp_path):
    return generate_sample(str(tmp_path / "sample"))


def test_generate_creates_clean_committed_repo(tmp_path) -> None:
    info = _generate(tmp_path)
    root = tmp_path / "sample"
    assert info.root == os.path.normpath(str(root))
    assert len(info.head_commit) == 40 and len(info.head_tree) == 40
    assert info.file_count > 0
    assert info.lines > 0
    # 结构落位：src/samplelib + tests + conftest
    assert (root / "src" / "samplelib" / "slugify.py").is_file()
    assert (root / "tests" / "test_normalize.py").is_file()
    assert (root / "conftest.py").is_file()
    # clean + 无未跟踪（.git 目录本身除外）
    out = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0 and out.stdout.strip() == ""


def test_generate_library_pytest_green(tmp_path) -> None:
    info = _generate(tmp_path)
    assert info.lines > 0  # 行数统计如实记录
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=str(tmp_path / "sample"),
        capture_output=True, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generate_idempotent_clean_reuse(tmp_path) -> None:
    first = _generate(tmp_path)
    second = generate_sample(str(tmp_path / "sample"))
    assert first.head_commit == second.head_commit
    assert first.head_tree == second.head_tree
    assert first.file_count == second.file_count
    assert first.lines == second.lines


def test_generate_dirty_rejects(tmp_path) -> None:
    _generate(tmp_path)
    dirty = tmp_path / "sample" / "unexpected.txt"
    dirty.write_text("x\n", encoding="utf-8", newline="")
    with pytest.raises(ValueError):
        generate_sample(str(tmp_path / "sample"))
