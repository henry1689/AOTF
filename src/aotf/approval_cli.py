"""AOTF Approval CLI（M1）。

operator 通过 CLI 提交签名审批，支持：
- 创建审批（ed25519 签名 + scope hash）
- 验证审批（公钥验签）
- 列出审批（DB 查询）
- 过期检查

设计裁决：
- 私钥从文件加载（PEM/DER），密码保护由调用方负责；
- 审批 ID 自动生成（ap-{task_id}-{uuid}）；
- 签名绑定 approval record 的 canonical payload；
- 验证失败返回 False（fail-closed）。

边界：本模块只处理 CLI 接口和签名操作；
approval record 的存储/读取由 store.py 负责。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aotf.attest import canonical_approval_payload, sign as attest_sign, verify as attest_verify
from aotf.models import ApprovalRecord
from aotf.store import get_approval, insert_approval

__all__ = [
    "cmd_approve",
    "cmd_verify",
    "cmd_list",
    "create_approval_record",
]


def create_approval_record(
    task_id: str,
    proposal_text: str,
    allowed_files: tuple[str, ...],
    baseline_tree: str,
    expiry_minutes: int = 60,
    source: str = "human",
) -> ApprovalRecord:
    """创建审批 record（不含签名，签名由 caller 另行处理）。"""
    import uuid

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=expiry_minutes)

    # 计算 scope hash
    scope_payload = "\n".join(sorted(allowed_files)) + "\x00" + proposal_text
    scope_sha256 = hashlib.sha256(scope_payload.encode("utf-8")).hexdigest()
    proposal_sha256 = hashlib.sha256(proposal_text.encode("utf-8")).hexdigest()

    approval_id = f"ap-{task_id}-{uuid.uuid4().hex[:8]}"

    return ApprovalRecord(
        approval_id=approval_id,
        task_id=task_id,
        decision="approved",
        scope_sha256=scope_sha256,
        proposal_sha256=proposal_sha256,
        baseline_tree=baseline_tree,
        source=source,
        expires_at=expires_at,
        created_at=now,
    )


def sign_approval(
    approval: ApprovalRecord,
    private_key: Ed25519PrivateKey,
) -> bytes:
    """对 approval 签名，返回原始 64 字节 Ed25519 签名。"""
    payload = canonical_approval_payload(
        approval_id=approval.approval_id,
        task_id=approval.task_id,
        decision=approval.decision,
        scope_sha256=approval.scope_sha256,
        proposal_sha256=approval.proposal_sha256,
        baseline_tree=approval.baseline_tree,
        created_at=approval.created_at,
        expires_at=approval.expires_at,
    )
    signature = private_key.sign(payload)
    return signature


def verify_approval(
    approval: ApprovalRecord,
    signature: bytes,
    public_key: bytes,  # 32 raw bytes
) -> bool:
    """验证 approval 签名。"""
    payload = canonical_approval_payload(
        approval_id=approval.approval_id,
        task_id=approval.task_id,
        decision=approval.decision,
        scope_sha256=approval.scope_sha256,
        proposal_sha256=approval.proposal_sha256,
        baseline_tree=approval.baseline_tree,
        created_at=approval.created_at,
        expires_at=approval.expires_at,
    )
    return attest_verify(payload, signature.hex(), public_key)


def load_private_key(key_path: Path) -> Ed25519PrivateKey:
    """从 PEM 文件加载私钥（无密码）。"""
    data = key_path.read_bytes()
    return serialization.load_pem_private_key(data, password=None)


def load_public_key(key_path: Path) -> bytes:
    """从 PEM 文件加载公钥，返回 raw 32 字节。"""
    data = key_path.read_bytes()
    pub = serialization.load_pem_public_key(data)
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


# ─── CLI 命令 ───────────────────────────────────────────────────────────────

def cmd_approve(args: argparse.Namespace) -> int:
    """提交审批。"""
    import sqlite3
    from aotf import db

    conn = db.connect(args.db)

    # 加载私钥
    private_key = load_private_key(Path(args.key_file))

    # 创建 approval record
    approval = create_approval_record(
        task_id=args.task_id,
        proposal_text=args.proposal,
        allowed_files=tuple(args.files),
        baseline_tree=args.baseline,
        expiry_minutes=args.expiry,
    )

    # 签名
    signature = sign_approval(approval, private_key)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    # 存储
    insert_approval(conn, approval)
    conn.execute("""
        INSERT OR REPLACE INTO approval_signatures
        (approval_id, signature, public_key, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        approval.approval_id,
        signature.hex(),
        public_key.hex(),
        approval.created_at.isoformat(),
    ))
    conn.commit()

    print(f"✓ Approval created: {approval.approval_id}")
    print(f"  Task: {approval.task_id}")
    print(f"  Expires: {approval.expires_at.isoformat()}")
    print(f"  Scope SHA-256: {approval.scope_sha256}")

    conn.close()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """验证审批签名。"""
    import sqlite3
    from aotf import db

    conn = db.connect(args.db)

    approval = get_approval(conn, args.approval_id)
    if approval is None:
        print(f"✗ Approval not found: {args.approval_id}")
        conn.close()
        return 1

    # 加载签名和公钥
    sig_row = conn.execute(
        "SELECT signature, public_key FROM approval_signatures WHERE approval_id=?",
        (args.approval_id,),
    ).fetchone()

    if sig_row is None:
        print("✗ Signature record not found")
        conn.close()
        return 1

    signature = bytes.fromhex(sig_row["signature"])
    public_key = bytes.fromhex(sig_row["public_key"])

    # 验签
    if verify_approval(approval, signature, public_key):
        print("✓ Signature valid")
        print(f"  Approval: {approval.approval_id}")
        print(f"  Task: {approval.task_id}")
        print(f"  Decision: {approval.decision}")
        print(f"  Expires: {approval.expires_at.isoformat()}")

        # 检查是否过期
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if approval.expires_at and approval.expires_at < now:
            print("⚠ WARNING: Approval expired")
            conn.close()
            return 2
    else:
        print("✗ Signature INVALID")
        conn.close()
        return 1

    conn.close()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """列出审批。"""
    import sqlite3
    from aotf import db

    conn = db.connect(args.db)

    rows = conn.execute("""
        SELECT a.approval_id, a.task_id, a.decision, a.created_at, a.expires_at,
               COALESCE(s.signature, '') as has_signature
        FROM approvals a
        LEFT JOIN approval_signatures s ON a.approval_id = s.approval_id
        ORDER BY a.created_at DESC
        LIMIT ?
    """, (args.limit,)).fetchall()

    if not rows:
        print("No approvals found")
        conn.close()
        return 0

    print(f"{'ID':<40} {'Task':<20} {'Decision':<10} {'Created':<25} {'Expires':<25} {'Signed'}")
    print("-" * 130)
    for r in rows:
        created = r["created_at"][:19] if r["created_at"] else ""
        expires = r["expires_at"][:19] if r["expires_at"] else ""
        signed = "✓" if r["has_signature"] else "✗"
        print(f"{r['approval_id']:<40} {r['task_id']:<20} {r['decision']:<10} {created:<25} {expires:<25} {signed}")

    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(
        prog="aotf-approval",
        description="AOTF Approval Management CLI",
    )
    parser.add_argument("--db", required=True, help="SQLite DB path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # approve
    p_approve = subparsers.add_parser("approve", help="Create new approval")
    p_approve.add_argument("--task-id", required=True, help="Task ID")
    p_approve.add_argument("--proposal", required=True, help="Proposal text file")
    p_approve.add_argument("--files", nargs="+", required=True, help="Allowed files")
    p_approve.add_argument("--baseline", required=True, help="Baseline git OID (40 hex)")
    p_approve.add_argument("--key-file", required=True, help="Private key PEM file")
    p_approve.add_argument("--expiry", type=int, default=60, help="Expiry minutes (default: 60)")

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify approval signature")
    p_verify.add_argument("--approval-id", required=True, help="Approval ID")

    # list
    p_list = subparsers.add_parser("list", help="List approvals")
    p_list.add_argument("--limit", type=int, default=20, help="Max results")

    args = parser.parse_args(argv)

    if args.command == "approve":
        # 读取 proposal 文件
        proposal_text = Path(args.proposal).read_text(encoding="utf-8")
        args.proposal = proposal_text
        return cmd_approve(args)
    elif args.command == "verify":
        return cmd_verify(args)
    elif args.command == "list":
        return cmd_list(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
