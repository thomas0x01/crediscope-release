import base64
import copy
import functools
import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_candidate as rc
import release_publish as rp
import winget_update as winget

STAMP = "2026-01-02T03:04:05Z"


def asset_row(name, data):
    return {"name": name, "state": "uploaded", "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "created_at": STAMP}


class OfflineHub:
    def __init__(self, tag):
        self.latest = tag
        self.events = []
        self.releases = {}
        self.assets = {}
        self.r2 = {}
        self.mirror = None
        self.fail_updates = False
        self.fail_beta_pointer = False
        self.hide_latest = False
        self.fail_r2_suffix = None
        self.fail_github_upload = None
        self.fail_github_delete = None
        self.asset_id = 100
        self.asset_stamp = STAMP
        self.add_release(tag, {spec["name"]: name.encode() for name, spec in rc.asset_specs(tag).items()}, False)
        self.updates = self.r2["latest.json"] = json.dumps({"version": tag[1:]}).encode()
        self.r2["index.html"] = b"previous stable page"

    def add_release(self, tag, assets, prerelease):
        old_assets = self.assets.get(tag, {})
        old_rows = {row["name"]: row for row in self.releases.get(tag, {}).get("assets", [])}
        rows = []
        for name, data in assets.items():
            if name in old_rows and old_assets[name] == data:
                rows.append(old_rows[name])
            else:
                self.asset_id += 1
                rows.append(dict(asset_row(name, data), id=self.asset_id, created_at=self.asset_stamp))
        self.assets[tag] = assets
        self.releases[tag] = {"tag_name": tag, "published_at": STAMP, "prerelease": prerelease,
                              "draft": False, "assets": rows}

    def release(self, tag):
        return copy.deepcopy(self.releases.get(("v9.4.0" if self.hide_latest else self.latest) if tag == "latest" else tag))

    def download_release(self, tag, name, directory):
        (directory / name).write_bytes(self.assets[tag][name])

    def read_updates_bytes(self):
        return self.updates

    def read_updates(self):
        return rc.json_object(self.updates.decode()) if self.updates else None

    def read_r2_bytes(self, key):
        return self.r2.get(key)

    def read_r2_json(self, key):
        return rc.json_object(self.r2[key].decode()) if key in self.r2 else None

    def download_r2(self, key, path):
        path.write_bytes(self.r2[key])

    def put_r2(self, path, key):
        if key == rp.BETA_STATE and self.fail_beta_pointer:
            raise OSError("synthetic beta mirror failure")
        self.events.append(("r2", key))
        self.r2[key] = path.read_bytes()

    def publish_candidate(self, candidate, directory, *, exists):
        tag = candidate["tag"]
        self.events.append(("candidate", tag))
        files = [asset["name"] for asset in candidate["assets"].values()] + [rc.MANIFEST_NAME]
        self.add_release(tag, {name: (directory / name).read_bytes() for name in files}, True)

    def publish_updates(self, latest):
        if self.fail_updates:
            self.updates = None
            raise ValueError("synthetic updates failure after removing the previous mirror")
        self.events.append(("updates", "latest.json"))
        self.updates = latest.read_bytes()

    def mark_latest(self, tag):
        self.events.append(("latest", tag))
        self.latest = tag
        self.releases[tag]["prerelease"] = False

    def legacy_notes(self, tag):
        assert tag == self.latest
        return "legacy formal notes"

    def mirror_stable(self, candidate, latest):
        self.events.append(("mirror", candidate["tag"]))
        self.mirror = latest.read_bytes()


class OfflineWinget:
    BASE = "0" * 40

    def __init__(self, hub, tag):
        self.hub, self.tag = hub, tag
        self.branch = f"{winget.PKGID}-{tag[1:]}"
        self.directory = f"manifests/t/thomas0x01/Crediscope/{tag[1:]}"
        self.installer = f"{self.directory}/{winget.PKGID}.installer.yaml"
        self.head = None
        self.pr = None
        self.contents = {self.BASE: {}}
        self.parents = {}
        self.writes = []
        self.fail_before_commit = False
        self.fail_after_pr = False
        self.competing_commit = False

    def commit(self, content, parent):
        sha = f"{len(self.contents):040x}"
        self.contents[sha] = content
        self.parents[sha] = parent
        return sha

    @staticmethod
    def error(url, code):
        raise urllib.error.HTTPError(url, code, "synthetic", None, io.BytesIO(b"synthetic response"))

    def urlopen(self, request, data=None, timeout=30):
        if isinstance(request, str):
            assert request == f"https://github.com/{winget.HUB}/releases/download/{self.tag}/{rc.MANIFEST_NAME}"
            return io.BytesIO(self.hub.assets[self.tag][rc.MANIFEST_NAME])
        method, url = request.get_method(), request.full_url
        assert url.startswith(winget.API + "/")
        split = urllib.parse.urlsplit(url)
        path, query = split.path, urllib.parse.parse_qs(split.query)
        fork, upstream = f"/repos/{winget.FORK}", f"/repos/{winget.UPSTREAM}"
        branch_ref = f"{fork}/git/ref/heads/{self.branch}"
        body = json.loads(data) if data else None
        if method != "GET":
            self.writes.append((method, path, body))
        if path == f"/repos/{winget.HUB}/releases/tags/{self.tag}":
            result = self.hub.release(self.tag)
        elif method == "GET" and path == branch_ref:
            if self.head is None:
                self.error(url, 404)
            result = {"object": {"sha": self.head}}
        elif method == "GET" and path in {f"{fork}/branches/master", f"{upstream}/branches/master"}:
            result = {"commit": {"sha": self.BASE}}
        elif method == "GET" and path.startswith(f"{upstream}/compare/"):
            assert path.startswith(f"{upstream}/compare/microsoft:{self.BASE}...thomas0x01:")
            head = path.rsplit(":", 1)[1]
            result = {"files": [{"filename": name, "status": "added"} for name in self.contents[head]]}
        elif method == "GET" and path == f"{upstream}/pulls":
            assert query["head"] == [f"thomas0x01:{self.branch}"] and query["state"] == ["all"]
            result = [self.pr] if self.pr else []
        elif method == "GET" and path == f"{upstream}/contents/{self.directory}":
            self.error(url, 404)
        elif method == "GET" and path.startswith(f"{fork}/contents/{self.directory}"):
            contents = self.contents[query["ref"][0]]
            file = path.removeprefix(f"{fork}/contents/")
            if file == self.directory:
                result = [{"path": name, "type": "file"} for name in contents if name.startswith(self.directory + "/")]
                if not result:
                    self.error(url, 404)
            else:
                result = {"encoding": "base64", "content": base64.b64encode(contents[file].encode()).decode()}
        elif method == "POST" and path == f"{fork}/git/refs":
            if self.head is not None:
                self.error(url, 422)
            assert body["ref"] == f"refs/heads/{self.branch}"
            self.head = body["sha"]
            result = {}
        elif method == "POST" and path == "/graphql":
            assert "createCommitOnBranch" in body["query"]
            inputs = body["variables"]["input"]
            assert inputs["branch"] == {"repositoryNameWithOwner": winget.FORK, "branchName": self.branch}
            if self.fail_before_commit:
                raise OSError("synthetic interruption after creating the branch")
            if self.competing_commit:
                self.head = self.commit({**self.contents[self.head], "other.yaml": "foreign edit"}, self.head)
            if inputs["expectedHeadOid"] != self.head:
                result = {"errors": [{"message": "synthetic branch moved"}]}
            else:
                tree = dict(self.contents[self.head])
                for row in inputs["fileChanges"]["additions"]:
                    assert row["path"] not in tree
                    tree[row["path"]] = base64.b64decode(row["contents"], validate=True).decode()
                self.head = self.commit(tree, self.head)
                result = {"data": {"createCommitOnBranch": {"commit": {"oid": self.head}}}}
        elif method == "POST" and path == f"{upstream}/pulls":
            assert self.pr is None and body["head"] == f"thomas0x01:{self.branch}" and body["base"] == "master"
            self.pr = {"number": 42, "state": "open", "html_url": "https://example.invalid/pr/42", "body": body["body"],
                       "head": {"sha": self.head, "ref": self.branch, "repo": {"full_name": winget.FORK}},
                       "base": {"ref": "master", "repo": {"full_name": winget.UPSTREAM}}}
            if self.fail_after_pr:
                raise OSError("synthetic lost response after accepting the PR")
            result = self.pr
        else:
            raise AssertionError((method, path, query, body))
        return io.BytesIO(json.dumps(result).encode())

    def run(self):
        with (patch.object(sys, "argv", ["winget_update.py", self.tag]),
              patch.dict(os.environ, {"WINGET_TOKEN": "synthetic", "WINGET_AUTO_ANSWER_MINUTES": "0"}),
              patch.object(winget.urllib.request, "urlopen", side_effect=self.urlopen)):
            winget.main()


def evidence(candidate):
    assets = candidate["assets"]
    now = datetime.now(timezone.utc).isoformat()
    ids = {"tag": candidate["tag"], "evidence_schema": 2, "candidate_id": candidate["candidate_id"],
           "build_id": candidate["build_id"], "candidate_created_at": candidate["created_at"]}
    hashes = rp.executable_hashes(candidate, "windows-setup")
    details = [{"name": name, "pass": True, "details": ["✓ synthetic observation"]} for name in rp.WINDOWS_JOURNEYS]
    next(row for row in details if row["name"] == "J9_upgrade_path").update(
        verified_setup_sha256=assets["windows-setup"]["sha256"], running_executables=hashes,
        from_version="9.4.0", observed_version=candidate["version"])
    win = {**ids, "platform": rc.WIN, "app_version": candidate["version"], "source_ref": candidate["source_sha"],
           "artifact_name": assets["windows-setup"]["name"], "artifact_sha256": assets["windows-setup"]["sha256"],
           "required": sorted(rp.WINDOWS_JOURNEYS), "executed": sorted(rp.WINDOWS_JOURNEYS),
           "journeys": dict.fromkeys(rp.WINDOWS_JOURNEYS, "pass"), "skipped": [], "result": "pass",
           "verified_setups": dict.fromkeys(("initial", "upgrade"), assets["windows-setup"]["sha256"]),
           "runtime_webview2": "157.0.1.2", "details": details, "attempt": "a" * 32,
           "started_at": now, "timestamp": now, "expected_executables": hashes,
           "running_executables": dict.fromkeys(("initial", "upgrade"), hashes)}
    mac = {**ids, "platform": rc.MAC, "source_sha": candidate["source_sha"], "receipts": {}}
    for kind in ("dmg", "updater"):
        asset = assets[f"macos-{kind}"]
        mac["receipts"][kind] = {"artifact_name": asset["name"], "artifact_sha256": asset["sha256"],
                                  "observed_version": candidate["version"], "result": "pass",
                                  "observed_arch": "arm64", "observation": "synthetic transcript", "tested_at": now,
                                  "running_executables": rp.executable_hashes(candidate, f"macos-{kind}")}
    mac["receipts"]["dmg"].update(mounted=True, installed=True, launched=True)
    mac["receipts"]["updater"].update(native_updater=True, restarted=True, from_version="9.4.0",
                                     signature_sha256=assets["macos-signature"]["sha256"])
    return win, mac


class CandidateTests(unittest.TestCase):
    def test_renamed_installer_headers_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = Path(directory) / rc.asset_specs("v9.4.1")["windows-setup"]["name"]
            installer.write_bytes(b"koly" + bytes(508))
            with self.assertRaisesRegex(ValueError, "not a PE"):
                rc.verify_installer_header(installer, "windows-setup")
            installer.write_bytes(b"MZ" + bytes(512))
            with self.assertRaisesRegex(ValueError, "not a UDIF"):
                rc.verify_installer_header(installer, "macos-dmg")

    def test_rehashed_wrong_platform_or_signer_role_still_fails(self):
        tag = "v9.4.1"
        assets = {role: dict(spec, size=1, sha256=f"{i + 1:064x}")
                  for i, (role, spec) in enumerate(rc.asset_specs(tag).items())}
        native = {role: {"version": tag[1:], "target": rc.TARGETS[assets[role]["platform"]],
                         "source_sha": "a" * 40, "build_id": "1.1",
                         "executables": dict.fromkeys(rc.EXECUTABLES, {"size": 1, "sha256": "0" * 64})}
                  for role in ("macos-dmg", "macos-updater", "windows-setup")}
        body = {"schema": 2, "tag": tag, "version": tag[1:], "source_sha": "a" * 40, "build_id": "1.1",
                "created_at": STAMP, "notes": "", "pre_tag": "", "pre_candidate_id": None,
                "assets": assets, "native": native}
        valid = dict(body, candidate_id=rc.candidate_id(body))
        rc.validate_candidate(valid)
        for role, field, value in [("macos-dmg", "platform", rc.WIN), ("windows-signature", "signs", "macos-updater"),
                                  ("windows-setup", "name", "wrong_9.4.0_x64-setup.exe")]:
            with self.subTest(role=role, field=field):
                broken = copy.deepcopy(body)
                broken["assets"][role][field] = value
                with self.assertRaisesRegex(ValueError, "asset identity"):
                    rc.validate_candidate(dict(broken, candidate_id=rc.candidate_id(broken)))


@unittest.skipUnless(os.environ.get("RELEASE_TEST_APP"), "set RELEASE_TEST_APP to the application worktree")
class OfflinePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(os.environ["RELEASE_TEST_APP"]).resolve()
        cls.sha = rc.pinned_source(cls.source)
        cls.sign_policy = cls.source / "desktop/scripts/policy/sign_policy.py"
        cls.seed = hashlib.sha256(b"synthetic release regression fixture").hexdigest()
        cls.pubkey = subprocess.check_output([sys.executable, str(cls.sign_policy), "keygen", "--seed-hex", cls.seed],
                                             text=True).strip()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        conf = self.root / "synthetic-pubkey.json"
        rc.write_json(conf, {"plugins": {"updater": {"pubkey": self.pubkey}}})
        self.real_verify_signatures = rp.verify_signatures
        patcher = patch.object(rp, "verify_signatures", functools.partial(self.real_verify_signatures, pubkey_conf=conf))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hub = OfflineHub("v9.4.0")
        self.addCleanup(patch.stopall)
        self._native_transports = {}
        self._next_build = 0
        self._build_ids = {}
        self._command = subprocess.run
        patch.object(subprocess, "run", side_effect=self.run_command).start()
        patch.object(rc.shutil, "which", side_effect=self.fake_which).start()
        patch.object(rp.time, "sleep", return_value=None).start()

    def fake_which(self, name):
        if name == "7z":
            return "7z"
        raise AssertionError(name)

    def run_command(self, command, *args, **kwargs):
        tool = command[0]
        if tool == "hdiutil":
            if command[1] == "attach":
                image = Path(command[-1]).read_bytes()
                mount = Path(command[command.index("-mountpoint") + 1])
                self._put_mac_files(mount / rc.APP_CONTENTS, self._native_transports[image])
            return subprocess.CompletedProcess(command, 0)
        if tool == "7z":
            image = Path(command[5]).read_bytes()
            output = Path(command[4][2:])
            for name, data in self._native_transports[image].items():
                (output / name).write_bytes(data)
            return subprocess.CompletedProcess(command, 0)
        if tool == "git" and "archive" in command:
            assert self.sha in command
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for name in rp.CANDIDATE_SOURCE_FILES:
                    data = self.archive_source_data(name)
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    archive.addfile(entry, io.BytesIO(data))
            return subprocess.CompletedProcess(command, 0, stdout=stream.getvalue())
        if len(command) > 3 and "--release-identity" in command:
            sidecar = Path(command[-1]).read_bytes()
            return subprocess.CompletedProcess(command, 0, stdout=sidecar.split(b"\n", 1)[1].decode())
        if tool == "gh":
            return self.fake_gh(command)
        if tool == "aws":
            return self.fake_aws(command)
        if tool == "git" and command[1] in ("config", "fetch", "switch", "add", "commit", "push", "diff", "show"):
            return self.fake_state_git(command, kwargs)
        return self._command(command, *args, **kwargs)

    def fake_gh(self, command):
        if command[1:4] == ["api", "--method", "DELETE"]:
            asset_id = int(command[4].rsplit("/", 1)[-1])
            if asset_id == 42:
                assert self.hub.starter
                self.hub.starter = False
                self.hub.events.append(("updates", "delete starter"))
            else:
                tag, name = next((tag, row["name"]) for tag, release in self.hub.releases.items()
                                 for row in release["assets"] if row["id"] == asset_id)
                if name == self.hub.fail_github_delete:
                    raise subprocess.CalledProcessError(1, command, stderr="synthetic attachment removal interrupted")
                assets = dict(self.hub.assets[tag])
                del assets[name]
                self.hub.add_release(tag, assets, True)
                self.hub.events.append(("delete", tag, name))
            return subprocess.CompletedProcess(command, 0)
        if command[1] == "api":
            tag = command[2].rsplit("/", 1)[-1]
            if tag == "updates":
                if getattr(self.hub, "starter", False):
                    assets = [{"id": 42, "name": "latest.json", "state": "starter", "size": 0, "digest": None}]
                else:
                    assets = [asset_row("latest.json", self.hub.updates)] if self.hub.updates else []
                release = {"assets": assets}
            else:
                release = self.hub.release(tag)
            if release is None:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="(HTTP 404)")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(release))
        assert command[1] == "release"
        action, tag = command[2:4]
        if action == "download":
            path = Path(command[command.index("--dir") + 1])
            name = command[command.index("--pattern") + 1]
            if tag == "updates":
                assert not getattr(self.hub, "starter", False), "starter attachment must not be downloaded"
                (path / name).write_bytes(self.hub.updates)
            else:
                self.hub.download_release(tag, name, path)
        elif action == "upload" and tag == "updates":
            assert not getattr(self.hub, "starter", False)
            self.hub.publish_updates(Path(command[4]))
        elif action == "edit":
            self.hub.mark_latest(tag)
        elif action == "create":
            self.hub.add_release(tag, {}, True)
        elif action == "upload":
            for item in command[4:command.index("-R")]:
                path = Path(item)
                if path.name == self.hub.fail_github_upload:
                    raise subprocess.CalledProcessError(1, command, stderr="synthetic attachment upload interrupted")
                assets = dict(self.hub.assets[tag])
                assert path.name not in assets or "--clobber" in command
                assets[path.name] = path.read_bytes()
                self.hub.add_release(tag, assets, True)
                self.hub.events.append(("upload", tag, path.name))
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0)

    def fake_aws(self, command):
        assert command[1:3] == ["s3", "cp"]
        source, target = command[3:5]
        if source.startswith("s3://offline/"):
            key = source.removeprefix("s3://offline/")
            content = self.hub.read_r2_bytes(key)
            if content is None:
                return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"(404)")
            if target == "-":
                return subprocess.CompletedProcess(command, 0, stdout=content)
            Path(target).write_bytes(content)
        else:
            assert target.startswith("s3://offline/")
            key = target.removeprefix("s3://offline/")
            if self.hub.fail_r2_suffix and key.endswith(self.hub.fail_r2_suffix):
                raise subprocess.CalledProcessError(1, command, stderr="synthetic R2 upload interrupted")
            self.hub.put_r2(Path(source), key)
        return subprocess.CompletedProcess(command, 0)

    def fake_state_git(self, command, kwargs):
        action = command[1]
        if action == "show":
            target = command[-1].split(":", 1)[1]
            content = self._git_files.get(target)
            if content is None:
                return subprocess.CompletedProcess(command, 1, stdout="if missing")
            return subprocess.CompletedProcess(command, 0, stdout=content.decode() if kwargs.get("text") else content)
        if action == "diff":
            return subprocess.CompletedProcess(command, int(any(Path(path).read_bytes() != self._git_files.get(path)
                                                               for path in ("page_state.json", "latest.json"))))
        if action == "push":
            for path in ("page_state.json", "latest.json"):
                self._git_files[path] = Path(path).read_bytes()
            self.hub.mirror = self._git_files["latest.json"]
            self.hub.events.append(("mirror", self.hub.latest))
        return subprocess.CompletedProcess(command, 0)

    def cli(self, args):
        self._git_files = getattr(self, "_git_files", {})
        cwd = Path.cwd()
        scratch = self.path()
        try:
            os.chdir(scratch)
            with (patch.dict(os.environ, {"R2_BUCKET": "offline", "R2_ACCOUNT_ID": "synthetic"}),
                  patch.object(sys, "argv", ["release_publish.py", *args])):
                rp.main()
        finally:
            os.chdir(cwd)

    def cli_promote(self, candidate, records, evidence_source=None):
        source = evidence_source or self.path()
        directory = source / "docs/testing/golden-runs"
        directory.mkdir(parents=True, exist_ok=True)
        refs = [f"{candidate['tag']}-{kind}.json" for kind in ("windows", "macos")]
        for ref, record in zip(refs, records):
            rc.write_json(directory / ref, record)
        self.cli(["promote", "--source", str(source), "--tag", candidate["tag"],
                  "--windows-evidence", refs[0], "--macos-evidence", refs[1]])

    def archive_source_data(self, name):
        if name == "desktop/src-tauri/tauri.conf.json":
            return json.dumps({"plugins": {"updater": {"pubkey": self.pubkey}}}).encode()
        return (self.source / name).read_bytes()

    @staticmethod
    def _put_mac_files(contents, files):
        for name, data in files.items():
            path = contents / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    @staticmethod
    def _pe(machine):
        return b"MZ" + bytes(58) + (64).to_bytes(4, "little") + b"PE\0\0" + machine

    def synthetic_payload(self, version, platform, build, revision, *, arm64=True, sidecar_build=None):
        header = (bytes.fromhex("cffaedfe0c000001" if arm64 else "cffaedfe07000001") if platform == rc.MAC
                  else self._pe(b"\x64\x86"))
        desktop = header + f"CREDISCOPE-RELEASE-V1|{version}|{rc.TARGETS[platform]}|{self.sha}|{build}|END".encode()
        sidecar = header + b"\n" + json.dumps({"git_head": self.sha, "release_version": version,
                                               "release_build_id": sidecar_build or build}).encode()
        if platform == rc.WIN:
            return {"crediscope-desktop.exe": desktop + revision.encode(), "crediscope-server.exe": sidecar}
        return {"Info.plist": plistlib.dumps({"CFBundleShortVersionString": version,
                                              "CFBundleExecutable": "crediscope-desktop"}),
                "MacOS/crediscope-desktop": desktop + revision.encode(), "MacOS/crediscope-server": sidecar}

    def path(self):
        return Path(tempfile.mkdtemp(dir=self.root))

    def staged(self, tag, revision="one", *, swap_signatures=False, tar_version=None, tar_arm64=True,
               windows_version=None, dmg_version=None, wrong_sidecar=False, build_id=None):
        root = self.path()
        self._next_build += 1
        build_id = build_id or f"1.{self._next_build}"
        bundle = root / "bundle"
        for folder in ("dmg", "macos", "nsis"):
            (bundle / folder).mkdir(parents=True)
        specs = rc.asset_specs(tag)
        dmg = bundle / "dmg" / specs["macos-dmg"]["name"].replace("Crediscope.LLM", "Crediscope LLM")
        dmg.write_bytes(f"synthetic DMG {tag} {revision} {build_id}".encode() + b"koly" + bytes(508))
        mac_update_files = self.synthetic_payload(tar_version or tag[1:], rc.MAC, build_id, revision, arm64=tar_arm64)
        self._native_transports[dmg.read_bytes()] = (self.synthetic_payload(dmg_version, rc.MAC, build_id, revision)
                                                      if dmg_version else mac_update_files)
        setup = bundle / "nsis" / specs["windows-setup"]["name"].replace("Crediscope.LLM", "Crediscope LLM")
        setup.write_bytes(self._pe(b"\x4c\x01") + f"{tag} {revision} {build_id}".encode())
        self._native_transports[setup.read_bytes()] = self.synthetic_payload(
            windows_version or tag[1:], rc.WIN, build_id, revision, sidecar_build="9.9" if wrong_sidecar else None)
        updater = bundle / "macos" / "Crediscope LLM.app.tar.gz"
        with tarfile.open(updater, "w:gz") as archive:
            for member, data in mac_update_files.items():
                entry = tarfile.TarInfo(f"{rc.APP_CONTENTS}/{member}")
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        signatures = [p.with_name(p.name + ".sig") for p in (updater, setup)]
        for path, sig in zip((updater, setup), reversed(signatures) if swap_signatures else signatures):
            subprocess.run([sys.executable, str(self.sign_policy), "sign", "--in", str(path), "--out", str(sig),
                            "--seed-hex", self.seed], stdout=subprocess.DEVNULL, check=True)
        staged = {rc.MAC: root / "mac", rc.WIN: root / "windows"}
        for platform, out in staged.items():
            rc.stage_platform(tag, self.sha, build_id, platform, bundle, self.source, out)
        self._build_ids[staged[rc.MAC]] = build_id
        return staged

    def build(self, tag, staged=None, pre_tag="", **kwargs):
        staged = staged or self.staged(tag, **kwargs)
        return rp.publish_build(self.hub, self.source, self.path(), tag, self.sha, self._build_ids[staged[rc.MAC]],
                                "one;two", pre_tag, staged)

    def promote(self, tag, receipts):
        return rp.promote(self.hub, self.source, self.path(), tag, *receipts)

    def cli_build(self, tag, staged, pre_tag=""):
        self.cli(["build", "--source", str(self.source), "--sha", self.sha,
                  "--build-id", self._build_ids[staged[rc.MAC]], "--tag", tag, "--notes", "one;two",
                  "--mac", str(staged[rc.MAC]), "--windows", str(staged[rc.WIN]), "--pre-tag", pre_tag])
        return rc.json_object(self.hub.assets[tag][rc.MANIFEST_NAME].decode())

    def test_cli_delayed_old_batch_does_not_replace_new_candidate_or_beta(self):
        """R01–R03: the release tag rejects older runs and attempts numerically, before writes."""
        for tag, before, after in (("v9.4.1", "99.12", "102.1"), ("v9.4.2-beta.1", "102.9", "102.10")):
            with self.subTest(tag=tag):
                old, new = (self.staged(tag, build_id=build) for build in (before, after))
                self.hub.fail_r2_suffix = ".dmg"
                with self.assertRaises(SystemExit):
                    self.cli_build(tag, old)
                self.hub.fail_r2_suffix = None
                self.assertIsNone(self.hub.release(tag))
                candidate = self.cli_build(tag, new)
                origin, attachments = copy.deepcopy((self.hub.r2, self.hub.assets[tag]))
                self.hub.events.clear()
                with self.assertRaises(SystemExit):
                    self.cli_build(tag, old)
                self.assertEqual(self.hub.events, [])
                self.assertEqual((self.hub.r2, self.hub.assets[tag]), (origin, attachments))
                if "-" in tag:
                    self.assertIn(rc.asset_url(candidate, "windows-setup").encode(), self.hub.r2["index.html"])
                else:
                    self.cli_promote(candidate, evidence(candidate))

    def test_cli_partial_attachment_switch_pauses_until_original_batch_finishes(self):
        """R03: an interrupted same-tag upload cannot pair an old manifest with new files."""
        tag = "v9.4.1"
        old = self.cli_build(tag, self.staged(tag))
        staged = self.staged(tag)
        dmg = rc.asset_specs(tag)["macos-dmg"]["name"]
        self.hub.fail_github_upload = rc.asset_specs(tag)["macos-updater"]["name"]
        with self.assertRaises(SystemExit):
            self.cli_build(tag, staged)
        self.assertNotIn(rc.MANIFEST_NAME, self.hub.assets[tag])
        self.assertIn("candidate-publication-pending.json", self.hub.assets[tag])
        self.assertNotEqual(self.hub.assets[tag][dmg], self.hub.r2[rc.asset_key(old, "macos-dmg")])
        self.hub.events.clear()
        with self.assertRaises(SystemExit):
            self.cli_promote(old, evidence(old))
        self.assertEqual(self.hub.events, [])
        self.hub.fail_github_upload = None
        self.hub.fail_github_delete = rp.PENDING_NAME
        with self.assertRaises(SystemExit):
            self.cli_build(tag, staged)
        self.assertNotIn(("upload", tag, dmg), self.hub.events)
        self.assertIn(rp.PENDING_NAME, self.hub.assets[tag])
        new_manifest = self.hub.assets[tag][rc.MANIFEST_NAME]
        new = rc.json_object(new_manifest.decode())
        original_row = next(row for row in self.hub.release(tag)["assets"] if row["name"] == rc.MANIFEST_NAME)
        self.hub.events.clear()
        with self.assertRaises(SystemExit):
            self.cli_promote(new, evidence(new))
        self.assertEqual(self.hub.events, [])

        self.hub.fail_github_delete = None
        self.hub.asset_stamp = "2026-01-03T00:00:00Z"
        self.assertEqual(self.cli_build(tag, staged), new)
        self.assertNotIn(rp.PENDING_NAME, self.hub.assets[tag])
        self.assertNotIn(("upload", tag, rc.MANIFEST_NAME), self.hub.events)
        release = self.hub.release(tag)
        self.assertIn(original_row, release["assets"])
        rc.verify_release_assets(new, release, new_manifest)
        self.cli_promote(new, evidence(new))

    def test_cli_same_batch_uses_pinned_beta_after_rebuild_and_release_cleanup(self):
        """R01: a published candidate's explicit beta remains fixed across publish retries."""
        beta_tag, tag = "v9.4.1-beta.1", "v9.4.1"
        beta = self.cli_build(beta_tag, self.staged(beta_tag))
        staged = self.staged(tag)
        self.hub.fail_r2_suffix = ".dmg"
        with self.assertRaises(SystemExit):
            self.cli_build(tag, staged, beta_tag)
        self.hub.fail_r2_suffix = None
        accepted = self.hub.read_r2_json(rp._publication_key(tag))
        self.assertEqual(accepted["status"], "pending")
        self.assertEqual(accepted["candidate"]["pre_candidate_id"], beta["candidate_id"])
        self.cli_build(beta_tag, self.staged(beta_tag))
        candidate = self.cli_build(tag, staged, beta_tag)
        self.assertEqual(candidate, accepted["candidate"])
        receipts = evidence(candidate)
        self.hub.events.clear()
        with self.assertRaises(SystemExit):
            self.cli_build(tag, staged)
        self.assertEqual(self.hub.events, [])
        for remove in (False, True):
            if remove:
                del self.hub.releases[beta_tag]
            self.hub.events.clear()
            self.assertEqual(self.cli_build(tag, staged, beta_tag), candidate)
            self.assertEqual(self.hub.events, [])
        self.cli_promote(candidate, receipts)
        self.assertIn(rc.asset_url(beta, "windows-setup").encode(), self.hub.r2["index.html"])

    def test_cli_promote_retains_well_formed_legacy_beta_and_rejects_malformed_links(self):
        """R01: the live legacy beta keeps its version and exact unversioned-by-ID links."""
        tag, beta_tag = "v9.4.1", "v9.4.2-test.1"
        candidate = self.cli_build(tag, self.staged(tag))
        receipts = evidence(candidate)
        old_page = self.path()
        subprocess.run([sys.executable, str(self.source / "desktop/scripts/gen_release_page.py"),
                        "--release-tag", "v9.4.0", "--pre-tag", beta_tag,
                        "--state", str(old_page / "state.json"), "--out", str(old_page)], check=True)
        page = (old_page / "index.html").read_bytes()
        links = [f"{rc.BASE_URL}/{beta_tag}/{rc.asset_specs(beta_tag)[role]['name']}".encode()
                 for role in rc.DOWNLOAD_ROLES.values()]
        malformed = page.replace(links[0], b"https://invalid.example/wrong.dmg")
        self.hub.r2["index.html"] = malformed
        self.hub.events.clear()
        with self.assertRaises(SystemExit):
            self.cli_promote(candidate, receipts)
        self.assertEqual(self.hub.r2["index.html"], malformed)
        self.assertEqual(self.hub.events, [])
        self.hub.r2["index.html"] = page
        self.cli_promote(candidate, receipts)
        for link in links:
            self.assertIn(link, self.hub.r2["index.html"])
        self.assertEqual(self.hub.read_r2_json(rp.BETA_STATE), {"tag": beta_tag, "candidate_id": None})

    def test_cli_future_platform_receipt_does_not_publish(self):
        """R03: a future stage timestamp cannot start a candidate publication."""
        tag = "v9.4.1"
        staged = self.staged(tag)
        receipt = staged[rc.WIN] / rc.STAGE_NAME
        rc.write_json(receipt, dict(rc.read_json(receipt), read_at="2099-01-01T00:00:00+00:00"))
        with self.assertRaises(SystemExit):
            self.cli_build(tag, staged)
        self.assertEqual(self.hub.events, [])
        self.assertIsNone(self.hub.release(tag))

    def test_candidate_beta_promote_and_late_beta(self):
        """R01: build B, publish C, promote B, then a beta staged before promotion writes."""
        original = dict(self.hub.r2)
        b = self.build("v9.4.1")
        self.assertEqual(self.hub.latest, "v9.4.0")
        self.assertTrue(self.hub.release(b["tag"])["prerelease"])
        for key in ("latest.json", "index.html"):
            self.assertEqual(self.hub.r2[key], original[key])
        self.assertEqual(self.hub.updates, original["latest.json"])
        self.assertIsNone(self.hub.mirror)

        c = self.build("v9.4.2-beta.1")
        page = self.hub.r2["index.html"].decode()
        self.assertIn("/releases/v9.4.0/Crediscope.LLM_9.4.0_aarch64.dmg", page)
        self.assertIn(rc.asset_url(c, "windows-setup"), page)
        self.assertNotIn(rc.asset_url(b, "macos-dmg"), page)
        self.assertEqual(self.hub.r2["latest.json"], original["latest.json"])
        late = self.staged("v9.4.0-test.1")

        self.promote(b["tag"], evidence(b))
        self.assertEqual(self.hub.latest, b["tag"])
        page = self.hub.r2["index.html"].decode()
        self.assertIn(rc.asset_url(b, "macos-dmg"), page)
        self.assertIn(rc.asset_url(c, "macos-dmg"), page)
        self.assertNotIn(rc.asset_url(b, "macos-updater"), page)
        update = self.hub.read_updates()
        self.assertEqual(self.hub.updates, self.hub.r2["latest.json"])
        self.assertEqual(self.hub.updates, self.hub.mirror)
        for platform, role in rc.UPDATE_ROLES.items():
            self.assertEqual(update["platforms"][platform]["url"], rc.asset_url(b, role))
            sig = b["assets"][b["assets"][role]["signature"]]["name"]
            self.assertEqual(update["platforms"][platform]["signature"], self.hub.assets[b["tag"]][sig].decode().strip())
        context = winget.installer_context(b["tag"], self.hub.release(b["tag"]), self.hub.assets[b["tag"]][rc.MANIFEST_NAME])
        self.assertEqual(context["url"], rc.asset_url(b, "windows-setup"))
        self.assertEqual(context["sha"], b["assets"]["windows-setup"]["sha256"].upper())

        delayed = self.build("v9.4.0-test.1", late)
        page = self.hub.r2["index.html"].decode()
        self.assertIn(rc.asset_url(b, "macos-dmg"), page)
        self.assertIn(rc.asset_url(delayed, "windows-setup"), page)
        self.assertNotIn("/releases/v9.4.0/Crediscope", page)
        self.assertEqual(self.hub.read_updates(), update)
        self.hub.events.clear()
        with self.assertRaises(ValueError):
            self.build(b["tag"])
        self.assertEqual(self.hub.events, [])

    def test_wrong_nine_and_same_tag_rebuild_invalidate_receipts(self):
        """R02: equal journey count and old evidence cannot open the promotion gate."""
        old = self.build("v9.4.1")
        win, mac = evidence(old)
        bogus = copy.deepcopy(win)
        wrong = "J11_onboarding_fresh"
        real = bogus["executed"][0]
        bogus["required"][0] = bogus["executed"][0] = wrong
        bogus["journeys"][wrong] = bogus["journeys"].pop(real)
        self.hub.events.clear()
        with self.assertRaisesRegex(ValueError, "exact nine"):
            self.promote(old["tag"], (bogus, mac))
        self.assertEqual(self.hub.events, [])
        wrong_guest = dict(win, verified_setups={"initial": win["artifact_sha256"], "upgrade": None})
        with self.assertRaisesRegex(ValueError, "guest digests"):
            self.promote(old["tag"], (wrong_guest, mac))
        self.assertEqual(self.hub.events, [])
        new = self.build(old["tag"], revision="two")
        self.assertNotEqual(old["candidate_id"], new["candidate_id"])
        self.assertNotEqual(old["assets"]["windows-setup"]["sha256"], new["assets"]["windows-setup"]["sha256"])
        self.assertIn(rc.asset_key(old, "windows-setup"), self.hub.r2)
        new_win, new_mac = evidence(new)
        self.hub.events.clear()
        for records, error in [((win, new_mac), "Windows evidence"), ((new_win, mac), "macOS evidence")]:
            with self.assertRaisesRegex(ValueError, error):
                self.promote(new["tag"], records)
            self.assertEqual(self.hub.events, [])
        for key in ("dmg", "updater"):
            broken = copy.deepcopy(new_mac)
            broken["receipts"][key]["artifact_sha256"] = old["assets"][f"macos-{key}"]["sha256"]
            with self.assertRaisesRegex(ValueError, key):
                self.promote(new["tag"], (new_win, broken))
        self.assertEqual(self.hub.events, [])
        self.promote(new["tag"], (new_win, new_mac))

    def test_bad_platform_or_signatures_never_publish(self):
        tag = "v9.4.1"
        with self.assertRaisesRegex(ValueError, "bundle version"):
            self.staged(tag, tar_version="9.4.0")
        with self.assertRaisesRegex(ValueError, "arm64 Mach-O"):
            self.staged(tag, tar_arm64=False)
        staged = self.staged(tag)
        mac_receipt = staged[rc.MAC] / rc.STAGE_NAME
        receipt = rc.read_json(mac_receipt)
        receipt["source_sha"] = "a" * 40 if self.sha != "a" * 40 else "b" * 40
        rc.write_json(mac_receipt, receipt)
        with self.assertRaisesRegex(ValueError, "platform receipt"):
            self.build(tag, staged)
        with self.assertRaisesRegex(ValueError, "signed filename"):
            self.build(tag, swap_signatures=True)
        self.assertEqual(self.hub.events, [])

    def test_r2_readback_precedes_github_candidate_visibility(self):
        staged = self.staged("v9.4.1")
        download = self.hub.download_r2

        def corrupt(key, path):
            download(key, path)
            if key.endswith(".dmg"):
                path.write_bytes(b"origin returned different staged bytes")

        with patch.object(self.hub, "download_r2", side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, "write not visible: R2 candidate asset macos-dmg"):
                self.build("v9.4.1", staged)
        self.assertIsNone(self.hub.release("v9.4.1"))
        self.assertEqual(self.hub.r2["index.html"], b"previous stable page")
        candidate = self.build("v9.4.1", staged)
        self.assertTrue(self.hub.release(candidate["tag"])["prerelease"])

    def test_origin_tampering_or_render_failure_never_promotes(self):
        candidate = self.build("v9.4.1")
        receipts = evidence(candidate)
        key = rc.asset_key(candidate, "macos-dmg")
        original = self.hub.r2[key]
        self.hub.r2[key] = b"tampered DMG"
        self.hub.events.clear()
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            self.promote(candidate["tag"], receipts)
        self.hub.r2[key] = original
        with patch.object(rp, "render_outputs", side_effect=ValueError("synthetic renderer failure")):
            with self.assertRaisesRegex(ValueError, "renderer failure"):
                self.promote(candidate["tag"], receipts)
        self.assertEqual(self.hub.events, [])

    def test_interrupted_promotion_cannot_have_stale_beta_write(self):
        candidate = self.build("v9.4.1")
        self.hub.fail_updates = True
        with self.assertRaisesRegex(ValueError, "synthetic updates failure"):
            self.promote(candidate["tag"], evidence(candidate))
        self.assertEqual(self.hub.latest, "v9.4.0")
        self.hub.events.clear()
        staged = self.staged("v9.4.2-beta.1")
        with self.assertRaisesRegex(ValueError, "out of sync"):
            self.build("v9.4.2-beta.1", staged)
        with self.assertRaisesRegex(ValueError, "out of sync"):
            self.build(candidate["tag"], revision="replacement")
        self.assertEqual(self.hub.events, [])
        self.hub.fail_updates = False
        self.promote(candidate["tag"], evidence(candidate))
        self.build("v9.4.2-beta.1", staged)
        self.assertIn(rc.asset_url(candidate, "windows-setup"), self.hub.r2["index.html"].decode())

    def test_macos_test_fixture_keeps_real_observations_pending(self):
        candidate = self.build("v9.4.1")
        assets = self.path()
        for name, data in self.hub.assets[candidate["tag"]].items():
            (assets / name).write_bytes(data)
        output = self.path() / "evidence"
        rp.prepare_evidence(self.source, assets, output)
        preview = rc.read_json(output / "latest.json")
        self.assertEqual(preview["platforms"][rc.MAC]["url"], rc.asset_url(candidate, "macos-updater"))
        draft = rc.read_json(output / "macos-evidence.json")
        self.assertEqual(draft["candidate_id"], candidate["candidate_id"])
        for kind, receipt in draft["receipts"].items():
            self.assertEqual(receipt["artifact_sha256"], candidate["assets"][f"macos-{kind}"]["sha256"])
            self.assertEqual(receipt["result"], "pending")
        with self.assertRaisesRegex(ValueError, "macOS"):
            rp.verify_evidence(candidate, evidence(candidate)[0], draft)

    def test_native_payload_version_or_sidecar_build_cannot_be_renamed_into_release(self):
        for kwargs, reason in [({"windows_version": "9.4.0"}, "desktop version"),
                               ({"dmg_version": "9.4.0"}, "bundle version"),
                               ({"wrong_sidecar": True}, "sidecar source")]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, reason):
                self.build("v9.4.1", **kwargs)
            self.assertEqual(self.hub.events, [])

    def test_cross_attempt_or_changed_staged_asset_cannot_publish(self):
        tag = "v9.4.1"
        first = self.staged(tag)
        second = self.staged(tag)
        with self.assertRaisesRegex(ValueError, "platform receipt"):
            self.build(tag, {rc.MAC: first[rc.MAC], rc.WIN: second[rc.WIN]})
        setup = first[rc.WIN] / rc.asset_specs(tag)["windows-setup"]["name"]
        setup.write_bytes(setup.read_bytes() + b"changed after native verification")
        with self.assertRaisesRegex(ValueError, "asset bytes differ"):
            self.build(tag, first)
        self.assertEqual(self.hub.events, [])

    def test_actual_promote_cli_uses_candidate_history_and_recovers_starter(self):
        candidate = self.build("v9.4.1")
        records = evidence(candidate)
        main = self.path()
        latest_conf = main / "desktop/src-tauri/tauri.conf.json"
        latest_conf.parent.mkdir(parents=True)
        rotated_seed = hashlib.sha256(b"later main synthetic rotation").hexdigest()
        rotated = subprocess.check_output([sys.executable, str(self.sign_policy), "keygen", "--seed-hex", rotated_seed],
                                          text=True).strip()
        rc.write_json(latest_conf, {"plugins": {"updater": {"pubkey": rotated}}})
        # Give the verify call its production behavior. Build used the fixture key through an explicit test override.
        rp.verify_signatures = self.real_verify_signatures
        self.hub.fail_updates = True
        with self.assertRaises(SystemExit):
            self.cli_promote(candidate, records, main)
        self.assertEqual(self.hub.latest, "v9.4.0")
        self.assertEqual(self.hub.read_r2_json("latest.json")["version"], "9.4.1")
        self.hub.fail_updates = False
        self.hub.starter = True
        self.hub.events.clear()
        self.cli_promote(candidate, records, main)
        self.assertIn(("updates", "delete starter"), self.hub.events)
        self.assertEqual(self.hub.latest, candidate["tag"])
        self.assertEqual(self.hub.updates, self.hub.r2["latest.json"])
        self.assertEqual(self.hub.mirror, self.hub.updates)

    def test_cli_rejects_contradictory_details_and_prebuild_observations_without_writes(self):
        candidate = self.build("v9.4.1")
        win, mac = evidence(candidate)
        bad_detail, bad_windows_time, bad_mac_time = copy.deepcopy(win), dict(win, started_at=STAMP), copy.deepcopy(mac)
        next(row for row in bad_detail["details"] if row["name"] == "J9_upgrade_path")["pass"] = False
        bad_mac_time["receipts"]["dmg"]["tested_at"] = STAMP
        for records in ((bad_detail, mac), (bad_windows_time, mac), (win, bad_mac_time)):
            self.hub.events.clear()
            with self.assertRaises(SystemExit) as exc:
                self.cli_promote(candidate, records)
            self.assertEqual(exc.exception.code, 1)
            self.assertEqual(self.hub.events, [])

    def test_homepage_beats_stale_beta_pointer_and_pinned_beta_survives_release_cleanup(self):
        c = self.build("v9.4.1-test.1")
        pinned = self.build("v9.4.1", pre_tag=c["tag"])
        del self.hub.releases[c["tag"]]
        self.promote(pinned["tag"], evidence(pinned))
        self.assertIn(rc.asset_url(c, "windows-setup"), self.hub.r2["index.html"].decode())
        self.assertEqual(self.hub.read_r2_json(rp.BETA_STATE)["candidate_id"], c["candidate_id"])
        self.hub.fail_beta_pointer = True
        late_tag = "v9.4.2-test.1"
        with self.assertRaisesRegex(OSError, "beta mirror failure"):
            self.build(late_tag)
        self.hub.fail_beta_pointer = False
        self.assertEqual(self.hub.read_r2_json(rp.BETA_STATE)["tag"], c["tag"])
        self.promote(pinned["tag"], evidence(pinned))
        self.assertIn(f"/releases/{late_tag}/", self.hub.r2["index.html"].decode())
        self.assertEqual(self.hub.read_r2_json(rp.BETA_STATE)["tag"], late_tag)

    def test_writes_must_be_visible_and_same_version_wrong_signature_can_be_repaired(self):
        candidate = self.build("v9.4.1")
        records = evidence(candidate)
        self.hub.hide_latest = True
        old_page = self.hub.r2["index.html"]
        with self.assertRaisesRegex(ValueError, "write not visible: GitHub Latest"):
            self.promote(candidate["tag"], records)
        self.assertEqual(self.hub.r2["index.html"], old_page)
        self.assertIsNone(self.hub.mirror)
        self.hub.hide_latest = False
        self.promote(candidate["tag"], records)
        complete = self.hub.updates
        changed = self.hub.read_updates()
        changed["platforms"][rc.WIN]["signature"] = changed["platforms"][rc.MAC]["signature"]
        self.hub.updates = json.dumps(changed).encode()
        self.hub.events.clear()
        with self.assertRaisesRegex(ValueError, "out of sync"):
            self.build("v9.4.2-beta.1")
        self.assertEqual(self.hub.events, [])
        self.promote(candidate["tag"], records)
        self.assertEqual(self.hub.updates, complete)
        self.hub.r2["latest.json"] = json.dumps(changed).encode()
        with self.assertRaisesRegex(ValueError, "out of sync"):
            self.build("v9.4.2-beta.1")
        self.promote(candidate["tag"], records)
        self.assertEqual(self.hub.r2["latest.json"], complete)

    def test_cli_release_date_survives_cross_day_promotion_retry_and_later_beta(self):
        """R01: the actual renderer uses the manifest attachment timestamp instead of runner time."""
        tag = "v9.4.1"
        self.hub.asset_stamp = "2026-01-02T23:04:05-08:00"
        candidate = self.cli_build(tag, self.staged(tag))
        receipts = evidence(candidate)
        real_run = self.run_command
        clock = """
import datetime, runpy, sys
class Clock(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat(sys.argv[1]).replace(tzinfo=tz)
datetime.datetime = Clock
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""

        def simulate(day):
            def run(command, *args, **kwargs):
                if command[0] == sys.executable and command[1].endswith("/gen_release_page.py"):
                    return real_run([command[0], "-c", clock, day, *command[1:]], *args, **kwargs)
                return real_run(command, *args, **kwargs)
            return patch.object(subprocess, "run", side_effect=run)

        with simulate("2026-11-20"):
            self.cli_promote(candidate, receipts)
        page = self.hub.r2["index.html"]
        published = b'<time class="release-date" datetime="2026-01-03">2026-01-03</time>'
        self.assertIn(published, page)
        self.assertIn(rc.asset_url(candidate, "macos-dmg").encode(), page)
        self.hub.events.clear()
        with simulate("2026-11-21"):
            self.cli_promote(candidate, receipts)
            self.assertEqual(self.hub.r2["index.html"], page)
            self.assertNotIn(("updates", "latest.json"), self.hub.events)
            self.assertNotIn(("latest", tag), self.hub.events)
            beta_tag = "v9.4.2-beta.1"
            beta = self.cli_build(beta_tag, self.staged(beta_tag))
        self.assertIn(published, self.hub.r2["index.html"])
        self.assertIn(rc.asset_url(beta, "windows-setup").encode(), self.hub.r2["index.html"])

    def test_actual_renderer_bad_version_badge_stops_before_remote_writes(self):
        candidate = self.build("v9.4.1")
        source_data = self.archive_source_data
        def wrong_badge(name):
            data = source_data(name)
            if name == "desktop/scripts/release_page/index.html":
                return data.replace(b"v{{VERSION}}", b"v9.4.0")
            return data
        self.hub.events.clear()
        with patch.object(self, "archive_source_data", side_effect=wrong_badge):
            with self.assertRaisesRegex(ValueError, "links and labels"):
                self.promote(candidate["tag"], evidence(candidate))
        self.assertEqual(self.hub.events, [])

    def _winget_fixture(self):
        candidate = self.build("v9.4.1")
        tag = candidate["tag"]
        self.promote(tag, evidence(candidate))
        transport = OfflineWinget(self.hub, tag)
        ctx = winget.installer_context(tag, self.hub.release(tag), self.hub.assets[tag][rc.MANIFEST_NAME])
        return transport, ctx

    def test_winget_cli_creates_once_and_reuses_an_accepted_pr_after_lost_response(self):
        """R01: an accepted PR survives a lost response, and merged history is reusable."""
        transport, ctx = self._winget_fixture()
        transport.fail_before_commit = True
        with self.assertRaisesRegex(SystemExit, "interruption after creating the branch"):
            transport.run()
        self.assertEqual(transport.head, transport.BASE)
        self.assertEqual(transport.contents[transport.head], {})
        self.assertIsNone(transport.pr)
        transport.fail_before_commit = False
        transport.fail_after_pr = True
        with self.assertRaisesRegex(SystemExit, "lost response"):
            transport.run()
        self.assertEqual(len(transport.contents[transport.head]), 3)
        self.assertEqual(transport.contents[transport.head][transport.installer], winget.INSTALLER_TMPL.format(**ctx))
        self.assertIn(ctx["candidate_id"], transport.pr["body"])
        self.assertEqual([(method, path) for method, path, _ in transport.writes], [
            ("POST", f"/repos/{winget.FORK}/git/refs"), ("POST", "/graphql"),
            ("POST", "/graphql"), ("POST", f"/repos/{winget.UPSTREAM}/pulls"),
        ])
        transport.writes.clear()
        transport.run()
        transport.pr.update(state="closed", merged_at=STAMP)
        transport.head = None
        transport.run()
        self.assertEqual(transport.pr["number"], 42)
        self.assertEqual(transport.writes, [])

    def test_winget_cli_appends_only_missing_manifests_on_partial_same_candidate_branch(self):
        """R01: a previously completed installer manifest survives a partial branch retry."""
        transport, ctx = self._winget_fixture()
        complete_installer = winget.INSTALLER_TMPL.format(**ctx)
        old_head = transport.head = transport.commit({transport.installer: complete_installer}, transport.BASE)
        transport.run()
        self.assertEqual(transport.parents[transport.head], old_head)
        self.assertEqual(len(transport.contents[transport.head]), 3)
        method, path, body = transport.writes[0]
        self.assertEqual((method, path), ("POST", "/graphql"))
        inputs = body["variables"]["input"]
        self.assertEqual(inputs["expectedHeadOid"], old_head)
        additions = inputs["fileChanges"]["additions"]
        self.assertEqual(len(additions), 2)
        self.assertNotIn(transport.installer, {row["path"] for row in additions})
        self.assertEqual(transport.contents[transport.head][transport.installer], complete_installer)
        self.assertEqual(transport.pr["head"]["sha"], transport.head)

    def test_winget_cli_refuses_foreign_branch_content_or_pr_without_writes(self):
        """R01: same-version conflicts, unrelated edits and ambiguous PR ownership fail closed."""
        transport, ctx = self._winget_fixture()
        installer = winget.INSTALLER_TMPL.format(**ctx)
        for contents, error in (({transport.installer: installer.replace(ctx["sha"], "A" * 64)}, "differs"),
                                ({transport.installer: installer, "extra.txt": "manual"}, "outside")):
            transport.head = transport.commit(contents, transport.BASE)
            with self.subTest(error=error), self.assertRaisesRegex(SystemExit, error):
                transport.run()
            self.assertEqual(transport.writes, [])
            self.assertEqual(transport.contents[transport.head], contents)
        transport.head = None
        transport.run()
        transport.writes.clear()
        original_body = transport.pr["body"]
        transport.pr["body"] = "Candidate: `" + "f" * 64 + "`."
        with self.assertRaisesRegex(SystemExit, "different branch, base or candidate"):
            transport.run()
        transport.pr.update(body=original_body, state="closed", merged_at=None)
        with self.assertRaisesRegex(SystemExit, "closed without merging"):
            transport.run()
        self.assertEqual(transport.writes, [])

    def test_winget_cli_cannot_replace_concurrently_changed_branch(self):
        """R01: even a change between preflight and ref update cannot be overwritten."""
        transport, ctx = self._winget_fixture()
        installer = winget.INSTALLER_TMPL.format(**ctx)
        old_head = transport.head = transport.commit({transport.installer: installer}, transport.BASE)
        transport.competing_commit = True
        with self.assertRaisesRegex(SystemExit, "synthetic branch moved"):
            transport.run()
        self.assertEqual(transport.parents[transport.head], old_head)
        self.assertEqual(transport.contents[transport.head], {transport.installer: installer, "other.yaml": "foreign edit"})
        self.assertIsNone(transport.pr)
        transport.writes.clear()
        with self.assertRaisesRegex(SystemExit, "outside"):
            transport.run()
        self.assertEqual(transport.writes, [])

    def test_winget_entry_keeps_historical_release_out_of_new_submission(self):
        tag = "v9.4.0"
        release = self.hub.release(tag)
        with (patch.object(sys, "argv", ["winget_update.py", tag]),
              patch.object(winget, "api", return_value=release) as api,
              patch.object(winget.urllib.request, "urlopen") as download,
              self.assertRaisesRegex(SystemExit, "historical releases retain their published links")):
            winget.main()
        api.assert_called_once_with("GET", f"repos/{winget.HUB}/releases/tags/{tag}")
        download.assert_not_called()

    def test_winget_refuses_candidate_and_inconsistent_setup(self):
        candidate = self.build("v9.4.1")
        manifest = self.hub.assets[candidate["tag"]][rc.MANIFEST_NAME]
        with self.assertRaisesRegex(ValueError, "promoted"):
            winget.installer_context(candidate["tag"], self.hub.release(candidate["tag"]), manifest)
        release = self.hub.release(candidate["tag"])
        release["prerelease"] = False
        next(asset for asset in release["assets"] if asset["name"].endswith("setup.exe"))["digest"] = "sha256:" + "a" * 64
        with self.assertRaisesRegex(ValueError, "asset digest"):
            winget.installer_context(candidate["tag"], release, manifest)
