#!/usr/bin/env python3
"""gen_policy.py — 生成/校验签名政策清单 policy.json（版本门+合规 kill-switch）。

配套文档：docs/签名政策运维.md（生成→签名→上传→启用全流程）。
消费方：桌面客户端 policy.rs（与 updater 同通道拉取、updater 现有
minisign 公钥验签）。本件只管「出一份合法的未签名 policy.json」，
签名走 sign_policy.py（tauri signer / minisign 同款格式）。

布点态默认（通道上线、能力就绪、不拦任何人）：
    {"issued_at": <now>, "min_supported": "0.0.0", "blocked": []}

用法：
    # 生成布点态（上线首日就用这个，零行为变化）
    python gen_policy.py --out policy.json

    # 启用版本门 + 停用某版本
    python gen_policy.py --out policy.json \\
        --min-supported 0.2.0 \\
        --blocked '0.1.13:compliance:依据监管要求，本版本已停用，请联系管理员。'

    # 上传前校验既有文件（CI 门禁用）
    python gen_policy.py --check policy.json

注意：签名针对文件字节——生成后不得再格式化/重排（改一个空格签名即失效），
修订政策一律重新生成+重新签名。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 与客户端 policy.rs parse_version_triple 同口径：X.Y.Z[. …/后缀宽容]。
# 生成侧收紧为「纯 X.Y.Z」——政策是官方出品不搞花式版本号。
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

MAX_REASON_LEN = 64     # 原因码：阻断页小字+排障日志
MAX_MESSAGE_LEN = 500   # 用户文案：阻断页主体，过长撑爆页面

ERR_HEADER = "policy.json 不合法"


def _fail(msg: str) -> None:
    print(f"::error::{ERR_HEADER}: {msg}", file=sys.stderr)
    raise SystemExit(1)


def validate_version(v: str, field: str) -> None:
    if not _VERSION_RE.match(v):
        _fail(f"{field}={v!r} 应为 X.Y.Z 纯三段数字（如 0.2.0）")


def validate_doc(doc: dict) -> None:
    """字段级校验（生成与 --check 共用）。任何不合格当场退出非零。"""
    if not isinstance(doc, dict):
        _fail("顶层应为 JSON 对象")
    unknown = set(doc) - {"issued_at", "min_supported", "blocked"}
    if unknown:
        _fail(f"未知字段 {sorted(unknown)}——客户端宽松忽略，但官方清单不允许拼写错误漏网")
    issued = doc.get("issued_at", "")
    if not isinstance(issued, str) or not re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$", issued
    ):
        _fail("issued_at 缺失或非 ISO-8601（如 2026-10-03T00:00:00Z）")
    min_supported = doc.get("min_supported", "")
    if not isinstance(min_supported, str):
        _fail("min_supported 应为字符串版本号")
    validate_version(min_supported, "min_supported")
    blocked = doc.get("blocked", [])
    if not isinstance(blocked, list):
        _fail("blocked 应为数组")
    for i, entry in enumerate(blocked):
        if not isinstance(entry, dict):
            _fail(f"blocked[{i}] 应为对象")
        missing = {"version", "reason", "message"} - set(entry)
        if missing:
            _fail(f"blocked[{i}] 缺字段 {sorted(missing)}")
        v = entry["version"]
        if v != "*" and not _VERSION_RE.match(v):
            _fail(f"blocked[{i}].version={v!r} 应为 X.Y.Z 或通配 '*'")
        if not isinstance(entry["reason"], str) or not entry["reason"]:
            _fail(f"blocked[{i}].reason 应为非空字符串（内部原因码）")
        if len(entry["reason"]) > MAX_REASON_LEN:
            _fail(f"blocked[{i}].reason 超 {MAX_REASON_LEN} 字")
        if not isinstance(entry["message"], str) or not entry["message"].strip():
            _fail(f"blocked[{i}].message 应为非空用户文案")
        if len(entry["message"]) > MAX_MESSAGE_LEN:
            _fail(f"blocked[{i}].message 超 {MAX_MESSAGE_LEN} 字（阻断页撑爆）")


def build_doc(min_supported: str, blocked: list[dict]) -> dict:
    doc = {
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_supported": min_supported,
        "blocked": blocked,
    }
    validate_doc(doc)
    return doc


def parse_blocked(specs: list[str]) -> list[dict]:
    """'version:reason:message' 三段冒号分隔（message 内允许冒号）。"""
    out = []
    for spec in specs:
        parts = spec.split(":", 2)
        if len(parts) != 3:
            _fail(f"--blocked 应为 version:reason:message 三段，得到 {spec!r}")
        out.append({"version": parts[0], "reason": parts[1], "message": parts[2]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, help="输出 policy.json 路径（缺省打印到 stdout）")
    ap.add_argument("--min-supported", default="0.0.0",
                    help="最低支持版本（默认 0.0.0=不拦，布点态）")
    ap.add_argument("--blocked", action="append", default=[],
                    metavar="VERSION:REASON:MESSAGE",
                    help="停用条目，可多次；VERSION 为 X.Y.Z 或 '*'")
    ap.add_argument("--check", type=Path, metavar="FILE",
                    help="校验既有 policy.json（不生成）")
    args = ap.parse_args()

    if args.check:
        try:
            doc = json.loads(args.check.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _fail(f"读取/解析失败: {e}")
        validate_doc(doc)
        n = len(doc.get("blocked", []))
        print(f"policy.json 合法：min_supported={doc['min_supported']} blocked={n} 条")
        return 0

    doc = build_doc(args.min_supported, parse_blocked(args.blocked))
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"policy.json 已生成：{args.out}（min_supported={doc['min_supported']}，"
              f"blocked={len(doc['blocked'])} 条）→ 下一步 sign_policy.py sign")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
