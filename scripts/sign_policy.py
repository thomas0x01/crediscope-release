#!/usr/bin/env python3
"""sign_policy.py — 签名/校验签名政策清单（policy.json → policy.json.sig）。

签名格式与 Tauri updater 制品完全同款（minisign，Ed25519+BLAKE2b 预哈希，
.sig 文件=四行签名文本的 base64 单行）——客户端 policy.rs 用 updater 现有
公钥验签，零新增密码学。配套文档：docs/签名政策运维.md。

子命令：
  sign    签名。生产路径=tauri CLI（读 TAURI_SIGNING_PRIVATE_KEY /
          TAURI_SIGNING_PRIVATE_KEY_PASSWORD 环境变量，即 CI 签更新制品的
          同一把钥匙，加密钥匙也能签）；开发/测试路径=--seed-hex 裸种子
          直签（openssl ed25519 + hashlib blake2b，零 node 依赖）。
  verify  校验。blake2b+openssl ed25519 复算两道签名（载荷签名+全局
          签名），公钥取 --pubkey-conf tauri.conf.json（即 updater 配置
          的 plugins.updater.pubkey）或 --pubkey-b64；上传 R2 前的门禁。

用法示例：
  # 生产（hub CI，钥匙在环境变量里）
  python sign_policy.py sign --in policy.json
  python sign_policy.py verify --in policy.json --sig policy.json.sig \\
      --pubkey-conf src/desktop/src-tauri/tauri.conf.json

  # 本地试验（生成一把一次性种子钥匙）
  python sign_policy.py keygen --seed-hex <64位hex> --emit-pubkey-b64
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PKCS8_SEED_TEMPLATE = bytes.fromhex("302e020100300506032b657004220420")  # +seed32
SPKI_PUB_TEMPLATE = bytes.fromhex("302a300506032b6570032100")  # +pk32
ALG_ED = b"Ed"   # 0x45 0x64 老式（对载荷直接签名；公钥固定此算法位）
ALG_EDD = b"ED"  # 0x45 0x44 预哈希（tauri signer 产出的签名形态）


def _die(msg: str) -> None:
    print(f"::error::sign_policy: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _pem(der: bytes, kind: str) -> bytes:
    b64 = base64.encodebytes(der)
    return f"-----BEGIN {kind} KEY-----\n".encode() + b64 + f"-----END {kind} KEY-----\n".encode()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, check=False, **kw)


# ── openssl ed25519 原语 ────────────────────────────────────────────

def _ed25519_sign(seed: Path, data: bytes, tmpdir: Path) -> bytes:
    key_pem = tmpdir / "key.pem"
    msg = tmpdir / "msg.bin"
    sig = tmpdir / "sig.bin"
    key_pem.write_bytes(_pem(PKCS8_SEED_TEMPLATE + seed, "PRIVATE"))
    msg.write_bytes(data)
    r = _run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key_pem), "-in", str(msg), "-out", str(sig)])
    if r.returncode != 0:
        _die(f"openssl ed25519 签名失败: {r.stderr.decode(errors='replace').strip()}")
    return sig.read_bytes()


def _ed25519_verify(pub_pem: Path, data: bytes, sig64: bytes, tmpdir: Path) -> bool:
    msg = tmpdir / "vmsg.bin"
    sfile = tmpdir / "vsig.bin"
    msg.write_bytes(data)
    sfile.write_bytes(sig64)
    r = _run(["openssl", "pkeyutl", "-verify", "-pubin", "-rawin",
              "-inkey", str(pub_pem), "-in", str(msg), "-sigfile", str(sfile)])
    return r.returncode == 0


def _seed_to_pub(seed: bytes, tmpdir: Path) -> bytes:
    """从种子推导 ed25519 公钥（openssl pubout，取 SPKI 末 32 字节）。"""
    key_pem = tmpdir / "k.pem"
    key_pem.write_bytes(_pem(PKCS8_SEED_TEMPLATE + seed, "PRIVATE"))
    r = _run(["openssl", "pkey", "-in", str(key_pem), "-pubout", "-outform", "DER"])
    if r.returncode != 0 or len(r.stdout) < 32:
        _die("openssl 公钥推导失败")
    return r.stdout[-32:]


# ── minisign 组装/解析（与客户端 policy.rs 同口径）─────────────────

def _normalize_sig_text(raw: str) -> str:
    """四行原文原样过；否则视作 updater 制品形态（整体 base64 单行）先解一层。"""
    t = raw.strip()
    if t.startswith("untrusted comment:"):
        return t
    try:
        return base64.b64decode(t, validate=True).decode("utf-8")
    except Exception as e:
        _die(f"签名文件形态不认（非四行原文也非 base64 单行）: {e}")


def _parse_sig(raw: str) -> tuple[bytes, bytes, bytes, bytes, str]:
    """→ (algo, key_id, sig64, global_sig64, trusted_comment)"""
    lines = _normalize_sig_text(raw).splitlines()
    if len(lines) < 4:
        _die(f"签名应为四行 minisign 文本，得到 {len(lines)} 行")
    b1 = base64.b64decode(lines[1])
    if len(b1) != 74:
        _die(f"签名首段应 74 字节，得到 {len(b1)}")
    global_sig = base64.b64decode(lines[3])
    comment = lines[2]
    if not comment.startswith("trusted comment: "):
        _die("第三行应为 trusted comment")
    return b1[:2], b1[2:10], b1[10:], global_sig, comment[len("trusted comment: "):]


def _parse_pubkey(conf_b64: str) -> tuple[bytes, bytes]:
    """conf 形态公钥（两行文本整体 base64，即 tauri.conf.json 里的形态）→ (key_id, pk32)"""
    text = base64.b64decode(conf_b64.strip()).decode("utf-8")
    kb = base64.b64decode(text.splitlines()[1])
    if len(kb) != 42:
        _die(f"公钥应 42 字节，得到 {len(kb)}")
    return kb[2:10], kb[10:]


def _sig_wire_bytes(algo: bytes, key_id: bytes, sig64: bytes, comment: str, global_sig: bytes) -> bytes:
    four_line = (
        "untrusted comment: signature from crediscope policy key\n"
        + base64.b64encode(algo + key_id + sig64).decode()
        + "\n"
        + f"trusted comment: {comment}\n"
        + base64.b64encode(global_sig).decode()
        + "\n"
    )
    # .sig 落盘形态与 updater 制品一致：四行文本整体 base64 单行
    return base64.b64encode(four_line.encode("utf-8")) + b"\n"


# ── 子命令 ─────────────────────────────────────────────────────────

def find_tauri_cli(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    candidates = [
        os.environ.get("TAURI_CLI"),
        "node_modules/.bin/tauri",
        shutil.which("tauri"),
        shutil.which("npx"),  # 以 npx --no-install 兜底
    ]
    for c in candidates:
        if not c:
            continue
        if Path(c).exists() or shutil.which(c):
            return c
    return None


def cmd_sign(args: argparse.Namespace) -> int:
    payload_path: Path = args.infile
    if not payload_path.is_file():
        _die(f"找不到 {payload_path}")
    payload = payload_path.read_bytes()

    # 路径 A：--seed-hex 裸种子直签（开发/测试；openssl+blake2b 零依赖）
    if args.seed_hex:
        try:
            seed = bytes.fromhex(args.seed_hex)
        except ValueError:
            _die("--seed-hex 应为 64 位十六进制（32 字节 ed25519 种子）")
        if len(seed) != 32:
            _die(f"--seed-hex 应 32 字节，得到 {len(seed)}")
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            pk = _seed_to_pub(seed, tmpdir)
            key_id = hashlib.blake2b(pk, digest_size=8).digest()
            h = hashlib.blake2b(payload, digest_size=64).digest()  # EdD 预哈希
            sig64 = _ed25519_sign(seed, h, tmpdir)
            comment = f"timestamp:{int(time.time())}\tfile:{payload_path.name}"
            global_sig = _ed25519_sign(seed, sig64 + comment.encode("utf-8"), tmpdir)
            wire = _sig_wire_bytes(ALG_EDD, key_id, sig64, comment, global_sig)
        out = args.out or payload_path.with_name(payload_path.name + ".sig")
        out.write_bytes(wire)
        print(f"已签名（seed 路径）：{out}")
        return 0

    # 路径 B：tauri CLI（生产路径——签更新制品的同一把钥匙/同一工具）
    cli = find_tauri_cli(args.tauri_cli)
    if cli is None:
        _die("未找到 tauri CLI（生产签名需要）。装法：npm i -g @tauri-apps/cli "
             "或 --tauri-cli 指到 node_modules/.bin/tauri；本地试验可用 --seed-hex")
    key_content = args.key_file.read_text(encoding="utf-8").strip() if args.key_file else os.environ.get("TAURI_SIGNING_PRIVATE_KEY")
    if not key_content:
        _die("缺私钥：--key-file <路径> 或环境变量 TAURI_SIGNING_PRIVATE_KEY（即 CI 签更新包的同一把）")
    password = args.password or os.environ.get("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "")

    # tauri signer sign 把 <file>.sig 写在输入文件旁——复制进临时目录签名，
    # 不污染工作区；钥匙走 env（不进命令行，避免 ps 泄露）
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        staged = tmpdir / payload_path.name
        staged.write_bytes(payload)
        env = dict(os.environ)
        env["TAURI_SIGNING_PRIVATE_KEY"] = key_content
        env["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"] = password
        base = [cli] if Path(cli).name != "npx" and cli != "npx" else [cli, "--no-install", "@tauri-apps/cli"]
        r = _run(base + ["signer", "sign", str(staged)], env=env)
        if r.returncode != 0:
            _die(f"tauri signer sign 失败: {r.stderr.decode(errors='replace').strip()}")
        produced = staged.with_name(staged.name + ".sig")
        if not produced.is_file():
            _die("tauri CLI 未产出 .sig 文件")
        wire = produced.read_bytes()
    out = args.out or payload_path.with_name(payload_path.name + ".sig")
    out.write_bytes(wire)
    print(f"已签名（tauri CLI）：{out}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    payload = args.infile.read_bytes() if args.infile.is_file() else _die(f"找不到 {args.infile}")
    sig_raw = args.sig.read_text(encoding="utf-8") if args.sig.is_file() else _die(f"找不到 {args.sig}")
    if args.pubkey_conf:
        conf = json.loads(args.pubkey_conf.read_text(encoding="utf-8"))
        conf_b64 = conf["plugins"]["updater"]["pubkey"]
    else:
        conf_b64 = args.pubkey_b64 or _die("缺公钥：--pubkey-conf 或 --pubkey-b64")
    key_id, pk32 = _parse_pubkey(conf_b64)
    algo, sig_key_id, sig64, global_sig, comment = _parse_sig(sig_raw)

    if sig_key_id != key_id:
        _die(f"key_id 不匹配：签名 {sig_key_id.hex()} vs 公钥 {key_id.hex()}（用错钥匙签的？）")
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        pub_pem = tmpdir / "pub.pem"
        pub_pem.write_bytes(_pem(SPKI_PUB_TEMPLATE + pk32, "PUBLIC"))
        # EdD 预哈希=验 blake2b 摘要；Ed 老式=验载荷原文
        target = hashlib.blake2b(payload, digest_size=64).digest() if algo == ALG_EDD else payload
        if not _ed25519_verify(pub_pem, target, sig64, tmpdir):
            _die("载荷签名校验失败（内容被改过，或不是这把钥匙签的）")
        if not _ed25519_verify(pub_pem, sig64 + comment.encode("utf-8"), global_sig, tmpdir):
            _die("全局签名（trusted comment）校验失败")
    print(f"签名校验通过：{args.infile.name}（algo={algo.decode()}，key_id={key_id.hex()}）")
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    """开发/测试用：从 --seed-hex 推导 conf 形态公钥（不产出私钥文件，
    种子即私钥）。生产钥匙一律 `tauri signer generate`（见运维文档）。"""
    seed = bytes.fromhex(args.seed_hex)
    if len(seed) != 32:
        _die("--seed-hex 应 64 位十六进制")
    with tempfile.TemporaryDirectory() as td:
        pk = _seed_to_pub(seed, Path(td))
    key_id = hashlib.blake2b(pk, digest_size=8).digest()
    two_line = (
        f"untrusted comment: minisign public key: {key_id.hex().upper()}\n"
        + base64.b64encode(ALG_ED + key_id + pk).decode()
        + "\n"
    )
    print(base64.b64encode(two_line.encode("utf-8")).decode())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sign = sub.add_parser("sign", help="签名 policy.json")
    p_sign.add_argument("--in", dest="infile", type=Path, required=True)
    p_sign.add_argument("--out", type=Path, help="输出 .sig 路径（缺省 <in>.sig）")
    p_sign.add_argument("--key-file", type=Path, help="tauri 私钥文件（缺省读 env TAURI_SIGNING_PRIVATE_KEY）")
    p_sign.add_argument("--password", help="私钥口令（缺省读 env TAURI_SIGNING_PRIVATE_KEY_PASSWORD）")
    p_sign.add_argument("--tauri-cli", help="tauri CLI 路径（缺省自动探测）")
    p_sign.add_argument("--seed-hex", help="开发/测试：32 字节 ed25519 种子直签（openssl 路径）")
    p_sign.set_defaults(func=cmd_sign)

    p_verify = sub.add_parser("verify", help="校验签名（上传 R2 前的门禁）")
    p_verify.add_argument("--in", dest="infile", type=Path, required=True)
    p_verify.add_argument("--sig", type=Path, required=True)
    p_verify.add_argument("--pubkey-conf", type=Path, help="tauri.conf.json（读 plugins.updater.pubkey）")
    p_verify.add_argument("--pubkey-b64", help="conf 形态公钥 base64 原文")
    p_verify.set_defaults(func=cmd_verify)

    p_keygen = sub.add_parser("keygen", help="开发/测试：种子→conf 形态公钥")
    p_keygen.add_argument("--seed-hex", required=True)
    p_keygen.set_defaults(func=cmd_keygen)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
