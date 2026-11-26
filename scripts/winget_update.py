#!/usr/bin/env python3
r"""winget-pkgs 版本跟提脚本（hub winget.yml 手动触发，亦可本地跑）。

用法: python3 scripts/winget_update.py vX.Y.Z
  - 拒绝带后缀 tag（test/beta/rc 只走 prerelease，不进 winget）
  - 核对正式 Release 的候选清单，使用确切的 Windows setup URL 和 SHA-256
  - 渲染三件 manifest——InstallerUrl 一律 dl.thomas0x00.uk 直链口径
    （勿改 GitHub 直链：境内可达性是选 dl 站的原因；也因此不用第三方
    winget-releaser Action，它会把 URL 改写成 GitHub release 直链）
  - 使用 fork thomas0x01/winget-pkgs 的 thomas0x01.Crediscope-<ver> 分支。
    续跑先核对分支内容和已有 PR，仅补同一候选缺失的 manifest。
  - 开 PR 后自动应答看守（默认 75 分钟，WINGET_AUTO_ANSWER_MINUTES 调，
    0=关）：needsCLA 回 agree、域名问询先 GET 实测 InstallerUrl 再发佐证
    （实测不可达则不发，非零退出告警）；人工审不阻塞，到点正常退出

ARP 元数据（AppsAndFeaturesEntries）来自 2026-10-13 VM 注册表实测：
HKCU\...\Uninstall\Crediscope LLM（每用户安装，NSIS /S 静默可验）。
环境: WINGET_TOKEN（classic PAT，public_repo 权限）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import release_candidate as rc

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
  InstallerUrl: {url}
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


def api(method: str, path: str, body: dict | None = None, *, optional: bool = False):
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
        with urllib.request.urlopen(req, data, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if optional and e.code == 404:
            return None
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
                status = _installer_reachable(ctx["url"])
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


def installer_context(tag, release, manifest):
    rc.release_tag(tag, stable=True)
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("winget requires a promoted release")
    candidate = rc.validate_candidate(rc.json_object(manifest.decode("utf-8")), tag)
    rc.verify_release_assets(candidate, release, manifest)
    date = release["published_at"][:10]
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
        raise ValueError("invalid GitHub publication date")
    return {"pkgid": PKGID, "ver": candidate["version"], "tag": tag, "date": date,
            "sha": candidate["assets"]["windows-setup"]["sha256"].upper(),
            "url": rc.asset_url(candidate, "windows-setup"), "candidate_id": candidate["candidate_id"]}


def _branch_head(branch):
    ref = api("GET", f"repos/{FORK}/git/ref/heads/{branch}", optional=True)
    return rc.source_sha(ref["object"]["sha"]) if ref is not None else None


def _existing_pr(branch, marker):
    owner = FORK.split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "all", "head": f"{owner}:{branch}", "per_page": 100})
    pulls = api("GET", f"repos/{UPSTREAM}/pulls?{query}")
    if not isinstance(pulls, list) or len(pulls) > 1:
        raise ValueError("ambiguous PR history for this winget branch")
    if not pulls:
        return None
    pr = pulls[0]
    head, base = pr["head"], pr["base"]
    markers = [line for line in (pr.get("body") or "").splitlines() if line.startswith("Candidate: ")]
    if ((head.get("repo") or {}).get("full_name") != FORK or head.get("ref") != branch
            or (base.get("repo") or {}).get("full_name") != UPSTREAM or base.get("ref") != "master"
            or (markers and markers != [marker])):
        raise ValueError("existing winget PR belongs to a different branch, base or candidate")
    if pr["state"] != "open" and not (pr["state"] == "closed" and pr.get("merged_at")):
        raise ValueError(f"existing winget PR was closed without merging: {pr['html_url']}")
    return pr


def _branch_files(head, base, directory, files):
    upstream_owner, fork_owner = (repo.split("/", 1)[0] for repo in (UPSTREAM, FORK))
    comparison = api("GET", f"repos/{UPSTREAM}/compare/{upstream_owner}:{base}...{fork_owner}:{head}?per_page=1")
    changes = comparison["files"]
    if any(row.get("status") != "added" or row.get("filename") not in files for row in changes):
        raise ValueError("winget branch has changes outside the expected new candidate manifests")
    entries = api("GET", f"repos/{FORK}/contents/{directory}?ref={head}", optional=True)
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ValueError("winget manifest directory is not a directory")
    present = set()
    for entry in entries:
        path = entry["path"]
        if path not in files or entry["type"] != "file" or path in present:
            raise ValueError("winget branch contains unexpected candidate files")
        blob = api("GET", f"repos/{FORK}/contents/{path}?ref={head}")
        if blob.get("encoding") != "base64" or base64.b64decode("".join(blob["content"].split()), validate=True) != files[path].encode():
            raise ValueError(f"winget manifest differs from this candidate: {path}")
        present.add(path)
    if not {row["filename"] for row in changes} <= present:
        raise ValueError("winget branch comparison disagrees with candidate files")
    if present and f"{directory}/{PKGID}.installer.yaml" not in present:
        raise ValueError("partial winget branch has no installer binding it to this candidate")
    return present


def _add_missing_files(branch, head, files, missing, marker):
    response = api("POST", "graphql", {
        "query": "mutation ($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }",
        "variables": {"input": {
            "branch": {"repositoryNameWithOwner": FORK, "branchName": branch},
            "expectedHeadOid": head,
            "message": {"headline": f"Add {branch} manifests", "body": marker},
            "fileChanges": {"additions": [{"path": path, "contents": base64.b64encode(text.encode()).decode()}
                                          for path, text in files.items() if path in missing]},
        }},
    })
    if response.get("errors"):
        raise ValueError("winget could not commit to the checked branch head: " + response["errors"][0]["message"])
    return rc.source_sha(response["data"]["createCommitOnBranch"]["commit"]["oid"])


def _submit(ctx):
    ver = ctx["ver"]
    branch = f"{PKGID}-{ver}"
    directory = f"manifests/t/thomas0x01/Crediscope/{ver}"
    files = {f"{directory}/{PKGID}{suffix}": template.format(**ctx) for suffix, template in (
        (".installer.yaml", INSTALLER_TMPL), (".locale.en-US.yaml", LOCALE_TMPL), (".yaml", VERSION_TMPL),
    )}
    marker = f"Candidate: `{ctx['candidate_id']}`."
    pr = _existing_pr(branch, marker)
    head = _branch_head(branch)
    upstream_head = rc.source_sha(api("GET", f"repos/{UPSTREAM}/branches/master")["commit"]["sha"])
    if pr is not None:
        pr_head = rc.source_sha(pr["head"]["sha"])
        if (head != pr_head and (head is not None or pr["state"] == "open")
                or _branch_files(pr_head, upstream_head, directory, files) != files.keys()):
            raise ValueError("existing winget PR does not point to the complete candidate manifests")
        return pr

    if api("GET", f"repos/{UPSTREAM}/contents/{directory}?ref={upstream_head}", optional=True) is not None:
        raise ValueError("upstream already contains this winget version")
    if head is None:
        head = rc.source_sha(api("GET", f"repos/{FORK}/branches/master")["commit"]["sha"])
        if _branch_files(head, upstream_head, directory, files):
            raise ValueError("fork master already contains this winget version")
        api("POST", f"repos/{FORK}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
        present = set()
    else:
        present = _branch_files(head, upstream_head, directory, files)
    if files.keys() - present:
        head = _add_missing_files(branch, head, files, files.keys() - present, marker)
    if (_branch_files(head, upstream_head, directory, files) != files.keys() or _branch_head(branch) != head):
        raise ValueError("winget branch does not contain the complete candidate manifests")
    pr = api("POST", f"repos/{UPSTREAM}/pulls", {
        "title": f"Add version {ver} of {PKGID}", "head": f"{FORK.split('/', 1)[0]}:{branch}", "base": "master",
        "body": (f"Version bump for {PKGID} {ver}. Installer URL points to the publisher-owned distribution "
                 f"site (dl.thomas0x00.uk). SHA256 verified against the signed release artifact.\n\n{marker}"),
    })
    if pr["head"]["sha"] != head:
        raise ValueError(f"winget PR head changed during creation: {pr['html_url']}")
    return pr


def main() -> None:
    try:
        tag = rc.release_tag(sys.argv[1] if len(sys.argv) > 1 else "", stable=True)
        rel = api("GET", f"repos/{HUB}/releases/tags/{tag}")
        if rel.get("draft") is not False or rel.get("prerelease") is not False:
            raise ValueError("winget requires a promoted release")
        assets = [asset for asset in rel["assets"] if asset.get("name") == rc.MANIFEST_NAME]
        if len(assets) != 1 or assets[0].get("state") != "uploaded":
            raise ValueError("winget requires a complete candidate manifest; historical releases retain their published links")
        asset_url = f"https://github.com/{HUB}/releases/download/{tag}/{rc.MANIFEST_NAME}"
        with urllib.request.urlopen(asset_url, timeout=30) as response:
            manifest = response.read(rc.MANIFEST_MAX_BYTES + 1)
        if len(manifest) > rc.MANIFEST_MAX_BYTES:
            raise ValueError("candidate manifest is too large")
        ctx = installer_context(tag, rel, manifest)
        pr = _submit(ctx)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        sys.exit(f"winget: {exc}")
    print("PR:", pr["html_url"])
    if pr.get("state") == "open":
        auto_answer(pr, ctx)


if __name__ == "__main__":
    main()
