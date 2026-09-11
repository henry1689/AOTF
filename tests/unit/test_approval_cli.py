"""AOTF Approval CLI 单元测试（M1）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aotf.approval_cli import (
    create_approval_record,
    verify_approval,
    load_private_key,
    load_public_key,
)
from aotf.models import ApprovalRecord


class TestCreateApprovalRecord:
    """审批记录创建。"""

    def test_basic_creation(self):
        record = create_approval_record(
            task_id="task-1",
            proposal_text="Do something",
            allowed_files=("a.py", "b.py"),
            baseline_tree="abc123" + "0" * 34,
            expiry_minutes=60,
        )
        assert record.task_id == "task-1"
        assert record.decision == "approved"
        assert record.source == "human"
        assert record.scope_sha256  # non-empty
        assert record.proposal_sha256  # non-empty

    def test_expires_after_minutes(self):
        record = create_approval_record(
            task_id="task-1",
            proposal_text="x",
            allowed_files=("a.py",),
            baseline_tree="a" * 40,
            expiry_minutes=30,
        )
        assert record.expires_at is not None
        assert record.expires_at > record.created_at
        delta = record.expires_at - record.created_at
        assert abs(delta.total_seconds() - 30 * 60) < 1

    def test_deterministic_hashes(self):
        r1 = create_approval_record(
            task_id="t", proposal_text="p",
            allowed_files=("a.py",), baseline_tree="b" * 40,
        )
        r2 = create_approval_record(
            task_id="t", proposal_text="p",
            allowed_files=("a.py",), baseline_tree="b" * 40,
        )
        assert r1.scope_sha256 == r2.scope_sha256
        assert r1.proposal_sha256 == r2.proposal_sha256


class TestVerifyApproval:
    """签名验证（mock key）。"""

    def test_verify_with_mock_keys(self):
        approval = create_approval_record(
            task_id="task-1",
            proposal_text="test",
            allowed_files=("a.py",),
            baseline_tree="a" * 40,
        )
        # 使用固定公钥（仅验证逻辑不崩溃）
        public_key = b"\x00" * 32
        signature = b"\x00" * 64
        # 验签应返回 False（无效签名）
        assert verify_approval(approval, signature, public_key) is False


class TestKeyLoading:
    """密钥加载（需要真实密钥文件）。"""

    def test_load_private_key_requires_file(self, tmp_path):
        fake_key = tmp_path / "key.pem"
        fake_key.write_text("FAKE", newline="")
        with pytest.raises(Exception):  # 无效 PEM
            load_private_key(fake_key)

    def test_load_public_key_requires_file(self, tmp_path):
        fake_pub = tmp_path / "pub.pem"
        fake_pub.write_text("FAKE", newline="")
        with pytest.raises(Exception):
            load_public_key(fake_pub)
