#!/usr/bin/env python3
"""Release asset identities shared by build, promote and winget."""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "candidate-manifest.json"
MANIFEST_MAX_BYTES = 65536
STAGE_NAME = "platform.json"
BASE_URL = "https://dl.thomas0x00.uk/releases"
MAC = "darwin-aarch64"
WIN = "windows-x86_64"
TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*))?")
SOURCE_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
BUILD_RE = re.compile(r"[1-9][0-9]*\.[1-9][0-9]*")
TARGETS = {MAC: "aarch64-apple-darwin", WIN: "x86_64-pc-windows-msvc"}
EXECUTABLES = {"desktop": "crediscope-desktop", "sidecar": "crediscope-server"}
APP_CONTENTS = PurePosixPath("Crediscope LLM.app/Contents")
PACKAGE_SUFFIXES = (".dmg", ".exe", ".msi", ".app.tar.gz", ".nsis.zip", ".sig")
DOWNLOAD_ROLES = {MAC: "macos-dmg", WIN: "windows-setup"}
UPDATE_ROLES = {MAC: "macos-updater", WIN: "windows-setup"}


def release_tag(tag, *, stable=False):
    match = TAG_RE.fullmatch(tag) if isinstance(tag, str) else None
    if not match or (stable and match[4]):
        raise ValueError(f"invalid {'stable ' if stable else ''}release tag: {tag!r}")
    if match[4] and any(part.isascii() and part.isdecimal() and part.startswith("0") and len(part) > 1
                        for part in match[4].split(".")):
        raise ValueError(f"invalid prerelease identifier: {tag!r}")
    return tag


def stable_version(tag):
    return tuple(int(component) for component in release_tag(tag, stable=True)[1:].split("."))


def source_sha(value):
    if not isinstance(value, str) or not SOURCE_RE.fullmatch(value):
        raise ValueError("source SHA must be a full lowercase git SHA")
    return value


def json_object(text):
    def parse_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"duplicate JSON key: {key}")
            obj[key] = value
        return obj

    data = json.loads(text, object_pairs_hook=parse_pairs)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def read_json(path):
    return json_object(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pinned_source(root):
    return source_sha(subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"],
                                              text=True).strip())


def asset_specs(tag):
    version = release_tag(tag)[1:]
    app = "Crediscope.LLM.app.tar.gz"
    setup = f"Crediscope.LLM_{version}_x64-setup.exe"
    return {
        "macos-dmg": {"platform": MAC, "kind": "dmg", "name": f"Crediscope.LLM_{version}_aarch64.dmg"},
        "macos-updater": {"platform": MAC, "kind": "app.tar.gz", "name": app,
                          "signature": "macos-signature"},
        "macos-signature": {"platform": MAC, "kind": "signature", "name": app + ".sig",
                            "signs": "macos-updater"},
        "windows-setup": {"platform": WIN, "kind": "nsis", "name": setup,
                          "signature": "windows-signature"},
        "windows-signature": {"platform": WIN, "kind": "signature", "name": setup + ".sig",
                              "signs": "windows-setup"},
    }


def candidate_id(body):
    wire = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def regular_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"missing, empty or nonregular file: {path.name}")
    return path


def file_identity(path):
    regular_file(path)
    return {"size": path.stat().st_size, "sha256": sha256(path)}


def _validate_assets(assets, specs):
    if not isinstance(assets, dict) or assets.keys() != specs.keys():
        raise ValueError("asset roles do not match the release inventory")
    for role, spec in specs.items():
        asset = assets[role]
        if (not isinstance(asset, dict) or asset.keys() != spec.keys() | {"size", "sha256"}
                or any(asset[key] != value for key, value in spec.items())
                or type(asset["size"]) is not int or asset["size"] <= 0
                or not isinstance(asset["sha256"], str) or not DIGEST_RE.fullmatch(asset["sha256"])):
            raise ValueError(f"invalid asset identity: {role}")
    if len({asset["sha256"] for asset in assets.values()}) != len(assets):
        raise ValueError("multiple asset roles have identical content")


def _validate_native(native, roles, tag, sha, build):
    specs = asset_specs(tag)
    if not isinstance(native, dict) or native.keys() != roles:
        raise ValueError("native package receipts do not match the release roles")
    for role, observed in native.items():
        expected = {"version": tag[1:], "target": TARGETS[specs[role]["platform"]],
                    "source_sha": sha, "build_id": build}
        if (not isinstance(observed, dict) or observed.keys() != expected.keys() | {"executables"}
                or any(observed[key] != value for key, value in expected.items())):
            raise ValueError(f"native package identity mismatch: {role}")
        executables = observed["executables"]
        if not isinstance(executables, dict) or executables.keys() != EXECUTABLES.keys():
            raise ValueError(f"native executable inventory mismatch: {role}")
        for item in executables.values():
            if (not isinstance(item, dict) or set(item) != {"sha256", "size"}
                    or type(item["size"]) is not int or item["size"] <= 0
                    or not isinstance(item["sha256"], str) or not DIGEST_RE.fullmatch(item["sha256"])):
                raise ValueError(f"invalid native executable digest: {role}")


def validate_candidate(data, tag=None):
    required = {"schema", "tag", "version", "source_sha", "build_id", "created_at", "notes", "pre_tag",
                "pre_candidate_id", "assets", "native", "candidate_id"}
    if set(data) != required:
        raise ValueError("invalid candidate manifest fields")
    actual_tag = release_tag(data["tag"])
    if (type(data["schema"]) is not int or data["schema"] != 2 or data["version"] != actual_tag[1:]
            or (tag is not None and actual_tag != tag) or not isinstance(data["notes"], str)):
        raise ValueError("candidate schema or version mismatch")
    source_sha(data["source_sha"])
    build_version_id(data["build_id"])
    timestamp(data["created_at"])
    validate_pre_tag(data["pre_tag"])
    pre_id = data["pre_candidate_id"]
    if ((data["pre_tag"] and (not isinstance(pre_id, str) or not DIGEST_RE.fullmatch(pre_id)))
            or (not data["pre_tag"] and pre_id is not None)):
        raise ValueError("invalid preselected beta candidate identity")
    _validate_assets(data["assets"], asset_specs(actual_tag))
    _validate_native(data["native"], {"macos-dmg", "macos-updater", "windows-setup"},
                     actual_tag, data["source_sha"], data["build_id"])
    if data["native"]["macos-dmg"] != data["native"]["macos-updater"]:
        raise ValueError("candidate macOS executables differ between DMG and updater")
    if data["candidate_id"] != candidate_id({key: value for key, value in data.items() if key != "candidate_id"}):
        raise ValueError("candidate manifest digest mismatch")
    return data


def validate_pre_tag(tag):
    if tag == "":
        return
    if "-" not in release_tag(tag):
        raise ValueError("pre_tag must be a prerelease tag")


def verify_files(candidate, directory):
    for role, asset in candidate["assets"].items():
        if file_identity(directory / asset["name"]) != {key: asset[key] for key in ("size", "sha256")}:
            raise ValueError(f"asset bytes differ from the candidate: {role}")


def verify_release_assets(candidate, release, manifest_bytes):
    if release.get("tag_name") != candidate["tag"]:
        raise ValueError("GitHub release tag does not match the candidate")
    expected = {asset["name"]: (asset["size"], asset["sha256"]) for asset in candidate["assets"].values()}
    expected[MANIFEST_NAME] = (len(manifest_bytes), hashlib.sha256(manifest_bytes).hexdigest())
    names = [asset.get("name") for asset in release["assets"]]
    if len(names) != len(set(names)) or set(names) != expected.keys():
        raise ValueError("GitHub release has missing, duplicate or unexpected assets")
    for asset in release["assets"]:
        size, digest = expected[asset["name"]]
        if (asset.get("state") != "uploaded" or asset.get("size") != size
                or asset.get("digest") != f"sha256:{digest}"):
            raise ValueError(f"GitHub asset digest or size mismatch: {asset['name']}")


def asset_key(candidate, role):
    return f"releases/{candidate['tag']}/{candidate['candidate_id']}/{candidate['assets'][role]['name']}"


def asset_url(candidate, role):
    return BASE_URL.removesuffix("/releases") + "/" + asset_key(candidate, role)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("expected timestamp with timezone")
    parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    if parsed.tzinfo is None:
        raise ValueError("expected timestamp with timezone")
    return parsed


def build_version_id(value):
    if not isinstance(value, str) or not BUILD_RE.fullmatch(value):
        raise ValueError("expected release workflow run ID and attempt")
    return value


def _native_identity(binary, platform, *, desktop=False):
    regular_file(binary)
    with binary.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if platform == MAC:
            if data[:8] != bytes.fromhex("cffaedfe0c000001"):
                raise ValueError(f"{binary.name} is not arm64 Mach-O")
        else:
            pe = int.from_bytes(data[60:64], "little")
            if len(data) < 64 or data[:2] != b"MZ" or data[pe:pe+6] != b"PE\0\0\x64\x86":
                raise ValueError(f"{binary.name} is not x64 PE")
        if desktop:
            matches = list(re.finditer(rb"CREDISCOPE-RELEASE-V1\|([^|]{1,100})\|([^|]{1,40})\|([a-f0-9]{40})\|([0-9.]{3,50})\|END", data))
            if len(matches) != 1:
                raise ValueError("desktop executable lacks a unique release identity")
            groups = [group.decode("ascii") for group in matches[0].groups()]
            return dict(zip(("version", "target", "source_sha", "build_id"), groups))
    return None


def _sidecar_identity(source, sidecar):
    command = [sys.executable, str(source / "desktop/scripts/build_provenance.py"),
               str(source / "crediscope-llm"), "--release-identity", str(sidecar)]
    return json_object(subprocess.check_output(command, text=True, timeout=120))


def _verify_payload(payload, source, platform, expected):
    desktop, sidecar = payload["desktop"], payload["sidecar"]
    observed = _native_identity(desktop, platform, desktop=True)
    if observed != expected:
        raise ValueError(f"packaged {platform} desktop version, target, source or build differs")
    _native_identity(sidecar, platform)
    expected_sidecar = {"git_head": expected["source_sha"], "release_build_id": expected["build_id"],
                        "release_version": expected["version"]}
    if _sidecar_identity(source, sidecar) != expected_sidecar:
        raise ValueError(f"packaged {platform} sidecar source, version or build differs")
    return {**observed, "executables": {name: file_identity(path) for name, path in payload.items()}}


def _mac_plist(path, version):
    if regular_file(path).stat().st_size > 256 * 1024:
        raise ValueError("macOS Info.plist is too large")
    info = plistlib.loads(path.read_bytes())
    if info.get("CFBundleShortVersionString") != version or info.get("CFBundleExecutable") != EXECUTABLES["desktop"]:
        raise ValueError("macOS bundle version or executable differs")


def _mac_app(contents, version):
    _mac_plist(contents / "Info.plist", version)
    return {role: contents / "MacOS" / name for role, name in EXECUTABLES.items()}


def _extract_mac_updater(archive_path, root):
    paths = {APP_CONTENTS / "Info.plist": root / "Info.plist"}
    paths.update({APP_CONTENTS / "MacOS" / name: root / "MacOS" / name for name in EXECUTABLES.values()})
    seen = set()
    with tarfile.open(archive_path, "r|gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name not in paths:
                continue
            if name in seen or not member.isfile() or not 0 < member.size <= 512 * 1024 * 1024:
                raise ValueError(f"invalid macOS updater entry: {name.name}")
            seen.add(name)
            path = paths[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream, path.open("xb") as target:
                shutil.copyfileobj(stream, target)
    if seen != paths.keys():
        raise ValueError("macOS updater lacks bundle metadata or executables")


def _extract_windows(setup, output):
    tool = shutil.which("7z") or shutil.which("7z.exe")
    if tool is None:
        tool = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe"
    selectors = [f"-ir!{name}.exe" for name in EXECUTABLES.values()]
    subprocess.run([str(tool), "x", "-tNsis", "-y", f"-o{output}", str(setup.resolve()), *selectors],
                   check=True, stdout=subprocess.DEVNULL, timeout=180)
    payload = {}
    for role, name in EXECUTABLES.items():
        entries = list(output.rglob(f"{name}.exe"))
        if len(entries) != 1:
            raise ValueError(f"Windows NSIS does not contain exactly one {name}.exe")
        payload[role] = regular_file(entries[0])
    return payload


def inspect_native(staged, assets, source, platform, version, sha, build):
    expected = {"version": version, "target": TARGETS[platform], "source_sha": sha, "build_id": build}
    path = lambda role: staged / assets[role]["name"]
    with tempfile.TemporaryDirectory(dir=staged.parent) as scratch:
        root = Path(scratch)
        if platform == WIN:
            inspected = _verify_payload(_extract_windows(path("windows-setup"), root), source, platform, expected)
            return {"windows-setup": inspected}
        unpacked = root / "updater"
        _extract_mac_updater(path("macos-updater"), unpacked)
        update = _verify_payload(_mac_app(unpacked, version), source, platform, expected)
        mount = root / "dmg"
        mount.mkdir()
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", str(mount),
                        str(path("macos-dmg").resolve())], check=True, stdout=subprocess.DEVNULL, timeout=120)
        try:
            dmg = _verify_payload(_mac_app(mount / APP_CONTENTS, version), source, platform, expected)
            if dmg != update:
                raise ValueError("macOS DMG and updater use different executables")
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=True, stdout=subprocess.DEVNULL, timeout=120)
        return {"macos-dmg": dmg, "macos-updater": update}


def verify_installer_header(path, role):
    with path.open("rb") as installer:
        if role == "macos-dmg":
            if path.stat().st_size < 512:
                raise ValueError("macOS installer is not a UDIF disk image")
            installer.seek(-512, 2)
            if installer.read(4) != b"koly":
                raise ValueError("macOS installer is not a UDIF disk image")
        elif role == "windows-setup":
            header = installer.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                raise ValueError("Windows setup is not a PE executable")
            installer.seek(int.from_bytes(header[60:], "little"))
            pe = installer.read(6)
            if pe[:4] != b"PE\0\0" or pe[4:] not in (b"\x4c\x01", b"\x64\x86"):
                raise ValueError("Windows setup is not an x86/x64 PE executable")


def stage_platform(tag, sha, build, platform, bundle, source, out):
    specs = {role: spec for role, spec in asset_specs(tag).items() if spec["platform"] == platform}
    source_sha(sha)
    build_version_id(build)
    subdirs = ("dmg", "macos") if platform == MAC else ("nsis",)
    found = {}
    for subdir in subdirs:
        for path in (bundle / subdir).iterdir():
            if path.name.lower().endswith(PACKAGE_SUFFIXES):
                name = path.name.replace(" ", ".")
                if name in found:
                    raise ValueError(f"duplicate platform artifact: {name}")
                found[name] = regular_file(path)
    if set(found) != {spec["name"] for spec in specs.values()}:
        raise ValueError(f"unexpected {platform} bundle contents: {sorted(found)}")
    if platform == MAC:
        if found[specs["macos-dmg"]["name"]].parent.name != "dmg" or any(
                found[specs[role]["name"]].parent.name != "macos" for role in ("macos-updater", "macos-signature")):
            raise ValueError("macOS artifacts in the wrong bundler directory")
    out.mkdir(parents=True, exist_ok=False)
    assets = {}
    for role, spec in specs.items():
        path = out / spec["name"]
        shutil.copyfile(found[path.name], path)
        if role in DOWNLOAD_ROLES.values():
            verify_installer_header(path, role)
        assets[role] = dict(spec, **file_identity(path))
    native = inspect_native(out, assets, source, platform, tag[1:], sha, build)
    verify_files({"assets": assets}, out)
    write_json(out / STAGE_NAME, {"tag": tag, "source_sha": sha, "build_id": build, "platform": platform,
                                 "read_at": datetime.now(timezone.utc).isoformat(), "assets": assets, "native": native})


def assemble_candidate(tag, sha, build, notes, pre_tag, pre_id, staged, out):
    specs = asset_specs(tag)
    assets, native, observed_times = {}, {}, []
    if staged.keys() != {MAC, WIN}:
        raise ValueError("both platform receipts are required")
    now = datetime.now(timezone.utc)
    for platform, directory in staged.items():
        receipt = read_json(regular_file(directory / STAGE_NAME))
        if tuple(receipt.get(key) for key in ("tag", "source_sha", "build_id", "platform")) != (tag, sha, build, platform):
            raise ValueError(f"platform receipt does not match build identity: {platform}")
        subset = {role: spec for role, spec in specs.items() if spec["platform"] == platform}
        _validate_assets(receipt.get("assets"), subset)
        _validate_native(receipt.get("native"), {role for role, spec in subset.items() if spec["kind"] != "signature"},
                         tag, sha, build)
        observed = timestamp(receipt.get("read_at"))
        if observed > now:
            raise ValueError(f"platform receipt timestamp is in the future: {platform}")
        observed_times.append(observed)
        if {path.name for path in directory.iterdir()} != {STAGE_NAME, *(spec["name"] for spec in subset.values())}:
            raise ValueError(f"unexpected staged files: {platform}")
        assets.update(receipt["assets"])
        native.update(receipt["native"])
    body = {"schema": 2, "tag": tag, "version": tag[1:], "source_sha": sha, "build_id": build,
            "created_at": max(observed_times).astimezone(timezone.utc).isoformat(), "notes": notes, "pre_tag": pre_tag,
            "pre_candidate_id": pre_id, "assets": assets, "native": native}
    candidate = validate_candidate(dict(body, candidate_id=candidate_id(body)), tag)
    out.mkdir(parents=True, exist_ok=False)
    for asset in assets.values():
        name = asset["name"]
        shutil.copyfile(regular_file(staged[asset["platform"]] / name), out / name)
    verify_files(candidate, out)
    manifest = out / MANIFEST_NAME
    write_json(manifest, candidate)
    if manifest.stat().st_size > MANIFEST_MAX_BYTES:
        raise ValueError("candidate manifest is too large")
    return candidate


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    validate = sub.add_parser("validate-input")
    validate.add_argument("--tag", required=True)
    validate.add_argument("--pre-tag", default="")
    stage = sub.add_parser("stage")
    stage.add_argument("--tag", required=True)
    stage.add_argument("--sha", required=True)
    stage.add_argument("--build-id", required=True)
    stage.add_argument("--source", type=Path, required=True)
    stage.add_argument("--platform", choices=(MAC, WIN), required=True)
    stage.add_argument("--bundle", type=Path, required=True)
    stage.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        release_tag(args.tag)
        if args.action == "validate-input":
            validate_pre_tag(args.pre_tag)
        else:
            if pinned_source(args.source) != source_sha(args.sha):
                raise ValueError("platform checkout differs from the tested source SHA")
            stage_platform(args.tag, args.sha, args.build_id, args.platform, args.bundle, args.source, args.out)
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(1, f"release candidate: {exc}\n")


if __name__ == "__main__":
    cli()
