#!/usr/bin/env python3
"""winget-pkgs 版本跟提脚本（hub winget.yml 手动触发，亦可本地跑）。

用法: python3 scripts/winget_update.py vX.Y.Z
  - 拒绝带后缀 tag（test/beta/rc 只走 prerelease，不进 winget）
  - 从本仓 Release 取 x64-setup.exe 资产的 sha256 digest
  - 渲染三件 manifest——InstallerUrl 一律 dl.thomas0x00.uk 直链口径
    （勿改 GitHub 直链：境内可达性是选 dl 站的原因；也因此不用第三方
    winget-releaser Action，它会把 URL 改写成 GitHub release 直链）
  - 推分支 thomas0x01.Crediscope-<ver> 到 fork thomas0x01/winget-pkgs，
    向 microsoft/winget-pkgs 开 PR

ARP 元数据（AppsAndFeaturesEntries）来自 2026-10-13 VM 注册表实测：
HKCU\...\Uninstall\Crediscope LLM（每用户安装，NSIS /S 静默可验）。
环境: WINGET_TOKEN（classic PAT，public_repo 权限）。
"""
import base64
import json
import os
import re
import sys
import urllib.error
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
        f"{PKGID}.version.yaml": VERSION_TMPL.format(**ctx),
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
                       "signed release artifact."})})
    print("PR:", pr["html_url"])


if __name__ == "__main__":
    main()
