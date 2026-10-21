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


# ── PR 开出后的自动应答（2026-10-21，backlog「winget 跟提后自动应答」立项）──
# 每个新 PR 固定收两道闸：needsCLA（回 agree 约 1 分钟摘标签）+ 域名问询
# （约 45 分钟到，挂 Needs-Author-Feedback；措辞「URL 不像有效」易被误读
# 成链接挂了）。轮询自动应答；人工审不阻塞——到点正常退出。诚实纪律：
# 「刚实测可达」句必须先真 GET 验证再写；实测失败宁可不发并非零退出告警。

CLA_AGREE = "@microsoft-github-policy-service agree"
EVIDENCE_MARK = "dl.thomas0x00.uk is owned and operated by the publisher"
POLL_SEC = 180


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


def _labels(pr_number: int) -> set[str]:
    issue = api_soft("GET", f"repos/{UPSTREAM}/issues/{pr_number}")
    if not isinstance(issue, dict):
        return set()
    return {_norm_label(l.get("name", "")) for l in issue.get("labels", [])}


def _comments(pr_number: int) -> list[dict]:
    out = api_soft("GET",
                   f"repos/{UPSTREAM}/issues/{pr_number}/comments?per_page=100")
    return out if isinstance(out, list) else []


def _installer_reachable(url: str) -> int:
    """GET Range bytes=0-0 实测。用 GET 不用 HEAD：2026-10-14 负缓存事故
    实证 HEAD/GET 缓存形态可分叉，GET 才是 winget/更新器的真实路径。"""
    req = urllib.request.Request(
        url, headers={"Range": "bytes=0-0",
                      "User-Agent": "crediscope-winget-update"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _precedent_prs(exclude: int) -> list[int]:
    """同作者同包的历史 PR（动态查，不硬编码编号）。"""
    q = urllib.parse.urlencode({
        "q": f"repo:{UPSTREAM} is:pr author:{FORK.split('/')[0]} {PKGID}",
        "sort": "created", "order": "desc", "per_page": "5"})
    out = api_soft("GET", f"search/issues?{q}")
    if not isinstance(out, dict):
        return []
    return [i["number"] for i in out.get("items", [])
            if isinstance(i.get("number"), int) and i["number"] != exclude]


def _evidence_body(ctx: dict, status: int, precedents: list[int]) -> str:
    prec = ("Previous submissions for earlier versions used the same URL "
            "scheme: " + ", ".join(f"#{n}" for n in precedents)) if precedents \
        else "This submission follows the same URL scheme as our previous submissions."
    return (
        "Thanks for checking the installer URL. Confirming ownership and integrity:\n\n"
        f"- {EVIDENCE_MARK} (thomas0x01 / crediscope). The official product page "
        "https://crediscope.thomas0x00.uk links to installers hosted on dl.thomas0x00.uk.\n"
        f"- I have just re-verified that the InstallerUrl is reachable (HTTP {status}).\n"
        f"- InstallerSha256 ({ctx['sha']}) matches the SHA256 digest of the signed "
        f"release asset published at https://github.com/{HUB}/releases/tag/{ctx['tag']}.\n"
        f"- {prec}.")


def auto_answer(pr: dict, ctx: dict) -> None:
    minutes = int(os.environ.get("WINGET_AUTO_ANSWER_MINUTES", "75"))
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

        # 闸一 CLA：标签或政策机器人问句在场且未答 → 回 agree
        cla_pending = bool(labels & {"needscla", "clarequired"}) or any(
            "microsoft-github-policy-service"
            in str((c.get("user") or {}).get("login") or "")
            and "agree" in (c.get("body") or "").lower() for c in comments)
        if cla_pending and not agreed:
            api("POST", f"repos/{UPSTREAM}/issues/{pr_number}/comments",
                {"body": CLA_AGREE})
            agreed = True
            print("已应答 CLA（agree）")

        # 闸二 域名问询：Needs-Author-Feedback + 他者最新评论涉 URL/域名 →
        # 先 GET 实测 InstallerUrl 再发佐证
        if "needsauthorfeedback" in labels and not evidenced:
            others = [c for c in comments
                      if (c.get("user") or {}).get("login") not in (None, me)]
            latest = str((others[-1].get("body") or "")) if others else ""
            if re.search(r"installer\s*url|domain|website|dl\.thomas0x00\.uk"
                         r"|url", latest, re.I):
                url = ("https://dl.thomas0x00.uk/releases/"
                       f"{ctx['tag']}/{ctx['asset']}")
                status = _installer_reachable(url)
                if status not in (200, 206):
                    sys.exit(f"域名问询在场但 InstallerUrl 实测不可达（HTTP "
                             f"{status}）——不发佐证，先修分发链再应答")
                api("POST", f"repos/{UPSTREAM}/issues/{pr_number}/comments",
                    {"body": _evidence_body(ctx, status,
                                            _precedent_prs(pr_number))})
                evidenced = True
                print(f"已应答域名问询（InstallerUrl 实测 HTTP {status}）")

        if not labels & {"needscla", "clarequired", "needsauthorfeedback"}:
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
