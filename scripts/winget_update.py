#!/usr/bin/env python3
r"""winget-pkgs 版本跟提脚本（hub winget.yml 手动触发，亦可本地跑）。

用法: python3 scripts/winget_update.py vX.Y.Z
  - 拒绝带后缀 tag（test/beta/rc 只走 prerelease，不进 winget）
  - 从本仓 Release 取 x64-setup.exe 资产的 sha256 digest
  - 渲染三件 manifest——InstallerUrl 一律 dl.thomas0x00.uk 直链口径
    （勿改 GitHub 直链：境内可达性是选 dl 站的原因；也因此不用第三方
    winget-releaser Action，它会把 URL 改写成 GitHub release 直链）
  - 推分支 thomas0x01.Crediscope-<ver> 到 fork thomas0x01/winget-pkgs，
    向 microsoft/winget-pkgs 开 PR
  - 开 PR 后自动应答看守（默认 75 分钟，WINGET_AUTO_ANSWER_MINUTES 调，
    0=关）：needsCLA 回 agree、域名问询先 GET 实测 InstallerUrl 再发佐证
    （实测不可达则不发，非零退出告警）；人工审不阻塞，到点正常退出

ARP 元数据（AppsAndFeaturesEntries）来自 2026-10-13 VM 注册表实测：
HKCU\...\Uninstall\Crediscope LLM（每用户安装，NSIS /S 静默可验）。
环境: WINGET_TOKEN（classic PAT，public_repo 权限）。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
HUB = "thomas0x01/crediscope-release"
FORK = "thomas0x01/winget-pkgs"
UPSTREAM = "microsoft/winget-pkgs"
PKGID = "thomas0x01.Crediscope"

INSTALLER_TMPL = """PackageIdentifier: {pkgid}
PackageVersion: {ver}
InstallerType: nullsoft
InstallModes:
- interactive
- silent
- silentWithProgress
InstallerSwitches:
  Silent: /S
  SilentWithProgress: /S
UpgradeBehavior: install
ReleaseDate: {date}
Installers:
- Architecture: x64
  InstallerUrl: https://dl.thomas0x00.uk/releases/{tag}/{asset}
  InstallerSha256: {sha}
  AppsAndFeaturesEntries:
  - DisplayName: Crediscope LLM
    DisplayVersion: {ver}
    Publisher: crediscope
    ProductCode: Crediscope LLM
ManifestType: installer
ManifestVersion: 1.6.0
"""

LOCALE_TMPL = """PackageIdentifier: {pkgid}
PackageVersion: {ver}
PackageLocale: en-US
Publisher: crediscope
PublisherUrl: https://crediscope.thomas0x00.uk
PackageName: Crediscope LLM
PackageUrl: https://crediscope.thomas0x00.uk
License: Proprietary
ShortDescription: AI-powered credit investigation and review report workbench for corporate lending
Moniker: crediscope
ManifestType: defaultLocale
ManifestVersion: 1.6.0
"""

VERSION_TMPL = """PackageIdentifier: {pkgid}
PackageVersion: {ver}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: 1.6.0
"""


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}/{path}", method=method,
        headers={
            "Authorization": f"Bearer {os.environ['WINGET_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "crediscope-winget-update",
        })
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"API {method} /{path} -> {e.code}: {e.read().decode()[:400]}")


# ── PR 开出后的自动应答（2026-10-21，backlog「winget 跟提后自动应答」立项；
# 同日红蓝一轮吸收：裸 url 正则过宽/标签读取失败误判「已摘」/最新一条被
# 样板评论压沉/403 UA 敏感/评论分页/CLA 启发式碰巧/先例句误导，全修）──
# 每个新 PR 固定收两道闸：needsCLA（回 agree 约 1 分钟摘标签）+ 域名问询
# （约 45 分钟到，挂 Needs-Author-Feedback；措辞「URL 不像有效」易被误读
# 成链接挂了）。轮询自动应答；人工审不阻塞——到点正常退出。诚实纪律：
# 「刚实测可达」句必须先真 GET 验证再写；实测失败宁可不发并非零退出告警。

CLA_AGREE = "@microsoft-github-policy-service agree"
EVIDENCE_MARK = "dl.thomas0x00.uk is owned and operated by the publisher"
POLL_SEC = 180
# 域名问询只认强信号：installer url 提法/domain 关键词/主机名本身。
# 裸「url」（PublisherUrl/徽章 endpoint?url= 都含）会误判无关问询（红队
# ③d 实证），website 同理太宽，均不入词表。
_DOMAIN_Q_RE = re.compile(r"installer\s*urls?|domain|dl\.thomas0x00\.uk", re.I)
# 问询评论后可能紧跟 validation 样板评论把 latest 压沉（#455291 实证相隔
# 28 秒）——扫描最近 N 条他者评论，不只看最新一条。
_DOMAIN_Q_LOOKBACK = 5


def api_soft(method: str, path: str):
    """轮询读的容错版：网络/HTTP 抖动返回 None（下轮再试），不杀进程。"""
    req = urllib.request.Request(
        f"{API}/{path}", method=method,
        headers={
            "Authorization": f"Bearer {os.environ['WINGET_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "crediscope-winget-update",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def _norm_label(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _labels(pr_number: int) -> set[str] | None:
    """None=读取失败（本轮跳过一切标签判定，防把「读失败」当「已摘」
    提前收尾）；空集=确实无标签。"""
    issue = api_soft("GET", f"repos/{UPSTREAM}/issues/{pr_number}")
    if not isinstance(issue, dict):
        return None
    return {_norm_label(l.get("name", "")) for l in issue.get("labels", [])}


def _comments(pr_number: int) -> list[dict]:
    """分页拉全：单页 100 上限在热 PR 上会漏看自己的历史评论→幂等失效
    重复发。5 页=500 条封顶（远超实际）；中途读取失败返回已捞部分。"""
    out: list[dict] = []
    for page in range(1, 6):
        chunk = api_soft("GET", f"repos/{UPSTREAM}/issues/{pr_number}"
                                f"/comments?per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
    return out


def _installer_reachable(url: str) -> int:
    """GET Range bytes=0-0 实测。用 GET 不用 HEAD：2026-10-14 负缓存事故
    实证 HEAD/GET 缓存形态可分叉，GET 才是 winget/更新器的真实路径。
    CF bot 防护 UA 敏感（2026-10-21 实测：python-urllib 默认 UA 吃 403，
    自定义 UA 206）——403 时换浏览器 UA 重试一次再定谳，防 CF 策略收紧
    造成假阴性带血退出。"""
    for ua in ("crediscope-winget-update",
               "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"):
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0",
                                                   "User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code != 403:
                return e.code
        except Exception:
            return 0
    return 403


def _evidence_body(ctx: dict, status: int) -> str:
    # 不引「先例 PR」：该包历史提交均 closed-unmerged（0 合并），引用失败
    # 提交作先例是误导（红队 S1-2 实证）。域名归属+官网交叉+sha256+实测
    # 可达四件已完整回答问询。
    return (
        "Thanks for checking the installer URL. Confirming ownership and integrity:\n\n"
        f"- {EVIDENCE_MARK} (thomas0x01 / crediscope). The official product page "
        "https://crediscope.thomas0x00.uk links to installers hosted on dl.thomas0x00.uk.\n"
        f"- I have just re-verified that the InstallerUrl is reachable (HTTP {status}).\n"
        f"- InstallerSha256 ({ctx['sha']}) matches the SHA256 digest of the signed "
        f"release asset published at https://github.com/{HUB}/releases/tag/{ctx['tag']}.")


def auto_answer(pr: dict, ctx: dict) -> None:
    try:
        minutes = int(os.environ.get("WINGET_AUTO_ANSWER_MINUTES", "75"))
    except ValueError:  # 非数字输入不得在 PR 已开出后炸（红队 S3-11）
        print("WINGET_AUTO_ANSWER_MINUTES 非数字，按默认 75 分钟")
        minutes = 75
    if minutes <= 0:
        return
    pr_number = pr["number"]
    me = (api_soft("GET", "user") or {}).get("login") or ""
    deadline = time.time() + minutes * 60
    agreed = evidenced = False
    print(f"自动应答看守 {minutes} 分钟（CLA/域名问询两闸；人工审不阻塞）…")
    while time.time() < deadline:
        labels = _labels(pr_number)
        comments = _comments(pr_number)
        ours = [c for c in comments
                if (c.get("user") or {}).get("login") == me]
        agreed = agreed or any(CLA_AGREE in (c.get("body") or "") for c in ours)
        evidenced = evidenced or any(
            EVIDENCE_MARK in (c.get("body") or "") for c in ours)

        # 闸一 CLA：标签在场，或政策机器人问句（其原文即含 agree 指令全文）
        # 在场且未答 → 回 agree
        cla_pending = (bool(labels and labels & {"needscla", "clarequired"})
                       or any(
            "microsoft-github-policy-service"
            in str((c.get("user") or {}).get("login") or "")
            and CLA_AGREE in (c.get("body") or "") for c in comments))
        if cla_pending and not agreed:
            api("POST", f"repos/{UPSTREAM}/issues/{pr_number}/comments",
                {"body": CLA_AGREE})
            agreed = True
            print("已应答 CLA（agree）")

        # 闸二 域名问询：Needs-Author-Feedback + 最近 N 条他者评论命中强信号
        # → 先 GET 实测 InstallerUrl 再发佐证
        if labels and "needsauthorfeedback" in labels and not evidenced:
            others = [c for c in comments
                      if (c.get("user") or {}).get("login") not in (None, me)]
            recent = others[-_DOMAIN_Q_LOOKBACK:]
            if any(_DOMAIN_Q_RE.search(str(c.get("body") or ""))
                   for c in recent):
                url = ("https://dl.thomas0x00.uk/releases/"
                       f"{ctx['tag']}/{ctx['asset']}")
                status = _installer_reachable(url)
                if status not in (200, 206):
                    sys.exit(f"域名问询在场但 InstallerUrl 实测不可达（HTTP "
                             f"{status}）——不发佐证，先修分发链再应答")
                api("POST", f"repos/{UPSTREAM}/issues/{pr_number}/comments",
                    {"body": _evidence_body(ctx, status)})
                evidenced = True
                print(f"已应答域名问询（InstallerUrl 实测 HTTP {status}）")

        if labels is not None and not labels & {"needscla", "clarequired",
                                                "needsauthorfeedback"}:
            state = (api_soft("GET", f"repos/{UPSTREAM}/pulls/{pr_number}")
                     or {}).get("state")
            if state and state != "open":
                print(f"PR 已 {state}，看守结束")
                return
            if agreed and evidenced:
                print("两闸均已应答且标签已摘——余下归人工审，看守收尾")
                return
        time.sleep(POLL_SEC)
    print(f"看守到点（{minutes} 分钟）：agree={agreed} 佐证={evidenced}。"
          "剩余事项归人工审节奏，不算失败")


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else ""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        sys.exit(f"tag 必须是干净正式版 vX.Y.Z（拒绝后缀），收到: {tag!r}")
    ver = tag[1:]

    rel = api("GET", f"repos/{HUB}/releases/tags/{tag}")
    asset = next((a for a in rel["assets"]
                  if re.search(r"_x64-setup\.exe$", a["name"])), None)
    if not asset:
        sys.exit(f"Release {tag} 无 x64-setup.exe 资产")
    sha = asset.get("digest", "").removeprefix("sha256:").upper()
    if not sha:
        sys.exit("资产缺 sha256 digest（hub build 上传资产时应带）")
    date = rel["published_at"][:10]

    ctx = {"pkgid": PKGID, "ver": ver, "tag": tag,
           "asset": asset["name"], "sha": sha, "date": date}
    files = {
        f"{PKGID}.installer.yaml": INSTALLER_TMPL.format(**ctx),
        f"{PKGID}.locale.en-US.yaml": LOCALE_TMPL.format(**ctx),
        # version manifest 文件名规范：仅 PackageIdentifier + .yaml，
        # 不带 .version 中缀（2026-10-13 首提被 bot 打回实证）
        f"{PKGID}.yaml": VERSION_TMPL.format(**ctx),
    }

    branch = f"{PKGID}-{ver}"
    base = api("GET", f"repos/{FORK}/branches/master")["commit"]["sha"]
    api("POST", f"repos/{FORK}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base})
    path = f"manifests/t/thomas0x01/Crediscope/{ver}"
    for name, text in files.items():
        api("PUT", f"repos/{FORK}/contents/{path}/{name}",
            {"message": f"Add {name} for {PKGID} {ver}", "branch": branch,
             "content": base64.b64encode(text.encode()).decode()})

    pr = api("POST", f"repos/{UPSTREAM}/pulls",
             {"title": f"Add version {ver} of {PKGID}",
              "head": f"thomas0x01:{branch}", "base": "master",
              "body": (f"Version bump for {PKGID} {ver}. Installer URL points "
                       "to the publisher-owned distribution site "
                       "(dl.thomas0x00.uk); SHA256 verified against the "
                       "signed release artifact.")})
    print("PR:", pr["html_url"])
    auto_answer(pr, ctx)


if __name__ == "__main__":
    main()
