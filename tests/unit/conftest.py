"""M0-B1 git 沙箱夹具（M0-B 后续包共用底座）。

GitSandbox 用真实 git 在 pytest tmp_path 内创建一次性已提交仓库，供
preflight/worktree/delta 等测试使用。所有仓库严格位于 base（=tmp_path）
之下，绝不接触 D:\\tools\\aotf 或任何真实仓；夹具仅 stdlib、不 import
aotf。既有测试不使用本 fixture，零影响。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_FIXED_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"}


class GitSandbox:
    """在 self.base 下构建一次性 git 仓库的夹具助手。"""

    def __init__(self, base: Path) -> None:
        self.base = base

    def git(self, repo: Path, *args: str) -> str:
        """夹具内部 git 调用：返回 stdout 文本；非零抛 AssertionError。"""
        env = dict(os.environ)
        env.update(_FIXED_ENV)
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} rc={proc.returncode}: "
                f"{proc.stderr.strip()}"
            )
        return proc.stdout.rstrip("\n")

    def init(self, name: str = "work", *, bare: bool = False,
             commit: bool = True) -> Path:
        """新建仓库 base/name。非 bare：本地固定 user/autocrlf/gpgsign。"""
        repo = self.base / name
        repo.mkdir(parents=True)
        if bare:
            self.git(self.base, "init", "--bare", name)
            return repo
        self.git(self.base, "init", "-b", "main", name)
        self.git(repo, "config", "user.name", "aotf-test")
        self.git(repo, "config", "user.email", "aotf-test@example.invalid")
        self.git(repo, "config", "core.autocrlf", "false")
        self.git(repo, "config", "commit.gpgsign", "false")
        if commit:
            self.commit_file(repo, "init.txt", content="init\n", msg="init")
        return repo

    def commit_file(self, repo: Path, rel: str, content: str = "line\n",
                    msg: str = "commit") -> str:
        """写文件（父目录自动建）→ add → commit；返回 HEAD sha。"""
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        self.git(repo, "add", "--", rel)
        self.git(repo, "commit", "-m", msg)
        return self.git(repo, "rev-parse", "--verify", "HEAD")

    def write(self, repo: Path, rel: str, content: str = "changed\n") -> None:
        """写/改文件但不 commit（制造 unstaged）。"""
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")

    def delete(self, repo: Path, rel: str) -> None:
        """删除已跟踪文件但不 commit（制造 unstaged deletion）。"""
        (repo / rel).unlink()

    def stage(self, repo: Path, rel: str) -> None:
        """git add（制造 staged change）。"""
        self.git(repo, "add", "--", rel)

    def untracked(self, repo: Path, rel: str, content: str = "x\n") -> None:
        """写未跟踪文件。"""
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")

    def detach(self, repo: Path) -> None:
        """checkout --detach HEAD。"""
        self.git(repo, "checkout", "--detach", "HEAD")

    def add_submodule_gitlink(self, repo: Path, name: str = "sub") -> str:
        """plumbing 造 gitlink（零 clone/网络）：<repo>/<name>/ 为独立仓且
        HEAD 与主仓 index 160000 条目一致，status 保持 clean。返回 name。"""
        sub = repo / name
        sub.mkdir(parents=True)
        self.git(sub, "init", "-b", "main")
        self.git(sub, "config", "user.name", "aotf-test")
        self.git(sub, "config", "user.email", "aotf-test@example.invalid")
        self.git(sub, "config", "core.autocrlf", "false")
        self.git(sub, "config", "commit.gpgsign", "false")
        (sub / "f.txt").write_text("s\n", encoding="utf-8", newline="")
        self.git(sub, "add", "--", "f.txt")
        self.git(sub, "commit", "-m", "sub commit")
        sha = self.git(sub, "rev-parse", "--verify", "HEAD")
        self.git(repo, "update-index", "--add", "--cacheinfo",
                 f"160000,{sha},{name}")
        self.git(repo, "commit", "-m", "add submodule gitlink")
        return name


@pytest.fixture
def sandbox(tmp_path: Path) -> GitSandbox:
    """一次性 git 沙箱（仓库均位于 tmp_path 下）。"""
    return GitSandbox(tmp_path)
