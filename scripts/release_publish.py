#!/usr/bin/env python3
"""Prepare and publish one release while the workflow holds release-publication."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import release_candidate as rc

HUB = "thomas0x01/crediscope-release"
BETA_STATE = "release-beta.json"
PENDING_NAME = "candidate-publication-pending.json"
CANDIDATE_SOURCE_FILES = (
    "desktop/scripts/gen_release_page.py", "desktop/scripts/release_page/index.html",
    "desktop/scripts/gen_latest_json.py", "desktop/scripts/policy/sign_policy.py",
    "desktop/src-tauri/tauri.conf.json", "crediscope-llm/static/logo.png",
    "crediscope-llm/static/img/feishu-group-qr.png",
)


class IncompleteUpdates(ValueError):
    pass
WINDOWS_JOURNEYS = frozenset({
    "J1_install_first_start", "J2_menu_create_project", "J3_assistant_create_project",
    "J4_template_intake", "J5_download_simplified", "J6_offline_charts",
    "J7_upgrade_banner", "J8_nonlisted_reject", "J9_upgrade_path",
})


def _time_in_window(value, earliest, latest):
    observed = rc.timestamp(value)
    if not earliest <= observed <= latest:
        raise ValueError("evidence time must follow candidate publication and precede promotion")
    return observed


def executable_hashes(candidate, role):
    return {name: data["sha256"] for name, data in candidate["native"][role]["executables"].items()}


def _windows_evidence(candidate, record, earliest, latest):
    setup = candidate["assets"]["windows-setup"]
    expected = {"evidence_schema": 2, "platform": rc.WIN, "tag": candidate["tag"],
                "source_ref": candidate["source_sha"], "candidate_id": candidate["candidate_id"],
                "build_id": candidate["build_id"], "candidate_created_at": candidate["created_at"],
                "app_version": candidate["version"], "artifact_name": setup["name"],
                "artifact_sha256": setup["sha256"], "result": "pass", "skipped": []}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Windows evidence does not match this candidate setup, source and result")
    started = _time_in_window(record.get("started_at"), earliest, latest)
    _time_in_window(record.get("timestamp"), started, latest)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", str(record.get("runtime_webview2", ""))):
        raise ValueError("Windows evidence lacks an observed WebView2 version")
    if not re.fullmatch(r"[0-9a-f]{32}", str(record.get("attempt", ""))):
        raise ValueError("Windows evidence lacks a unique VM attempt")
    if record.get("journeys") != dict.fromkeys(WINDOWS_JOURNEYS, "pass"):
        raise ValueError("Windows evidence needs the exact nine passing journeys")
    if record.get("verified_setups") != {"initial": setup["sha256"], "upgrade": setup["sha256"]}:
        raise ValueError("Windows evidence needs guest digests for both tested installations")
    hashes = executable_hashes(candidate, "windows-setup")
    if record.get("expected_executables") != hashes or record.get("running_executables") != dict.fromkeys(("initial", "upgrade"), hashes):
        raise ValueError("Windows evidence needs both installed executable matches")
    for key in ("required", "executed"):
        rows = record.get(key)
        if not isinstance(rows, list) or len(rows) != len(WINDOWS_JOURNEYS) or sorted(rows) != sorted(WINDOWS_JOURNEYS):
            raise ValueError(f"Windows evidence has the wrong nine {key} journeys")
    details = record.get("details")
    if not isinstance(details, list) or len(details) != len(WINDOWS_JOURNEYS):
        raise ValueError("Windows evidence lacks nine detailed results")
    by_name = {}
    for row in details:
        if (not isinstance(row, dict) or row.get("name") not in WINDOWS_JOURNEYS or row.get("pass") is not True
                or row.get("status", "pass") != "pass" or row["name"] in by_name
                or not isinstance(row.get("details"), list) or not row["details"]
                or any(not isinstance(line, str) or line.lstrip().startswith("✗") for line in row["details"])):
            raise ValueError("Windows journey detail contradicts its summary")
        by_name[row["name"]] = row
    upgrade = by_name["J9_upgrade_path"]
    if (upgrade.get("verified_setup_sha256") != setup["sha256"] or upgrade.get("running_executables") != hashes
            or upgrade.get("observed_version") != candidate["version"]
            or rc.stable_version(f"v{upgrade.get('from_version')}") >= rc.stable_version(candidate["tag"])):
        raise ValueError("Windows J9 detail does not prove the candidate upgrade")


def _macos_evidence(candidate, record, earliest, latest):
    expected = {"evidence_schema": 2, "tag": candidate["tag"], "source_sha": candidate["source_sha"],
                "candidate_id": candidate["candidate_id"], "platform": rc.MAC,
                "build_id": candidate["build_id"], "candidate_created_at": candidate["created_at"]}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("macOS evidence does not match this candidate source and platform")
    receipts = record.get("receipts")
    if not isinstance(receipts, dict) or set(receipts) != {"dmg", "updater"}:
        raise ValueError("macOS requires separate DMG and native updater receipts")
    for kind, receipt in receipts.items():
        role = f"macos-{kind}"
        asset = candidate["assets"][role]
        required = {"artifact_name": asset["name"], "artifact_sha256": asset["sha256"],
                    "observed_version": candidate["version"], "observed_arch": "arm64", "result": "pass",
                    "running_executables": executable_hashes(candidate, role)}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in required.items()):
            raise ValueError(f"macOS {kind} receipt does not match the tested artifact and runtime")
        if not isinstance(receipt.get("observation"), str) or not receipt["observation"].strip():
            raise ValueError(f"macOS {kind} receipt needs a reviewable observation")
        _time_in_window(receipt.get("tested_at"), earliest, latest)
    dmg, update = receipts["dmg"], receipts["updater"]
    if any(dmg.get(key) is not True for key in ("mounted", "installed", "launched")):
        raise ValueError("macOS DMG mount, installation and launch must pass")
    if any(update.get(key) is not True for key in ("native_updater", "restarted")):
        raise ValueError("macOS native updater installation and restart must pass")
    if rc.stable_version(f"v{update.get('from_version')}") >= rc.stable_version(candidate["tag"]):
        raise ValueError("macOS native updater must start on an older version")
    if update.get("signature_sha256") != candidate["assets"]["macos-signature"]["sha256"]:
        raise ValueError("macOS updater receipt does not match the candidate signature")


def verify_evidence(candidate, windows, macos, *, published_at=None):
    earliest = rc.timestamp(candidate["created_at"])
    if published_at:
        earliest = max(earliest, rc.timestamp(published_at))
    latest = datetime.now(timezone.utc) + timedelta(minutes=2)
    _windows_evidence(candidate, windows, earliest, latest)
    _macos_evidence(candidate, macos, earliest, latest)


def _utc_date(stamp):
    return rc.timestamp(stamp).astimezone(timezone.utc).date().isoformat()


def download_entry(candidate, stamp=None):
    return {"tag": candidate["tag"], "notes": candidate["notes"], "candidate_id": candidate["candidate_id"],
            "release_date": _utc_date(stamp if stamp is not None else candidate["created_at"]),
            "downloads": {platform: rc.asset_url(candidate, role) for platform, role in rc.DOWNLOAD_ROLES.items()}}


def update_entry(candidate, directory, pub_date):
    platforms = {}
    for platform, role in rc.UPDATE_ROLES.items():
        signature = candidate["assets"][candidate["assets"][role]["signature"]]
        platforms[platform] = {"url": rc.asset_url(candidate, role),
                               "signature": (directory / signature["name"]).read_text(encoding="ascii").strip()}
    return {"tag": candidate["tag"], "notes": candidate["notes"], "pub_date": pub_date, "platforms": platforms}


class _PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sections = []
        self.links = {"dl-row": [], "pre-block": [], "outside": []}
        self.labels = {"ver-badge": [], "pre-tag": []}
        self.label = None
        self.dates = []
        self.in_date = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "time" and attributes.get("class") == "release-date":
            self.dates.append([attributes.get("datetime"), ""])
            self.in_date = True
        if tag == "div":
            classes = (attributes.get("class") or "").split()
            section = next((name for name in ("dl-row", "pre-block") if name in classes), None)
            self.sections.append(section or (self.sections[-1] if self.sections else "outside"))
        if tag == "span":
            for label in self.labels:
                if label in (attributes.get("class") or "").split():
                    self.label = label
                    self.labels[label].append("")
        if tag == "a":
            href = attributes.get("href") or ""
            section = self.sections[-1] if self.sections else "outside"
            if section == "pre-block" or href.startswith(rc.BASE_URL + "/"):
                self.links[section].append(href)

    def handle_data(self, text):
        if self.label is not None:
            self.labels[self.label][-1] += text
        if self.in_date:
            self.dates[-1][1] += text

    def handle_endtag(self, tag):
        if tag == "time":
            self.in_date = False
        if tag == "span":
            self.label = None
        if tag == "div" and self.sections:
            self.sections.pop()


def render_outputs(source, output, formal, beta, update):
    scripts = source / "desktop" / "scripts"
    output.mkdir()
    page_input, update_input = output / "page-input.json", output / "updater-input.json"
    rc.write_json(page_input, {"formal": formal, "beta": beta})
    rc.write_json(update_input, update)
    for name, inputs, target in [("gen_release_page.py", page_input, output / "page"),
                                 ("gen_latest_json.py", update_input, output / "latest.json")]:
        subprocess.run([sys.executable, str(scripts / name), "--render-input", str(inputs),
                        "--out", str(target)], check=True)
    for name in ("index.html", "logo.png", "feishu-qr.png"):
        rc.regular_file(output / "page" / name)
    parser = _PageLinks()
    parser.feed((output / "page" / "index.html").read_text(encoding="utf-8"))
    expected_links = {"dl-row": [formal["downloads"][platform] for platform in (rc.WIN, rc.MAC)],
                      "pre-block": [beta["downloads"][platform] for platform in (rc.WIN, rc.MAC)] if beta else [],
                      "outside": []}
    expected_labels = {"ver-badge": [formal["tag"]], "pre-tag": [beta["tag"]] if beta else []}
    if parser.links != expected_links or parser.labels != expected_labels:
        raise ValueError("generated page does not contain the exact formal and beta links and labels")
    if parser.dates != [[formal["release_date"], formal["release_date"]]]:
        raise ValueError("generated page does not contain the recorded release date")
    notes = "\n".join(f"· {line.strip()}" for line in update["notes"].split(";") if line.strip())
    expected_update = {"version": update["tag"][1:], "pub_date": update["pub_date"],
                       "notes": notes or f"Crediscope LLM {update['tag'][1:]}", "platforms": update["platforms"]}
    if rc.read_json(output / "latest.json") != expected_update:
        raise ValueError("generated updater manifest does not match candidate URLs and signatures")


def verify_signatures(source, candidate, directory, *, pubkey_conf=None):
    pubkey_conf = pubkey_conf or source / "desktop/src-tauri/tauri.conf.json"
    for role in rc.UPDATE_ROLES.values():
        asset = candidate["assets"][role]
        signature = directory / candidate["assets"][asset["signature"]]["name"]
        wire = signature.read_text(encoding="ascii").strip()
        lines = base64.b64decode(wire, validate=True).decode("utf-8").splitlines()
        if len(lines) != 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
            raise ValueError(f"invalid Tauri updater signature encoding: {role}")
        names = [item.removeprefix("file:").replace(" ", ".")
                 for item in lines[2].removeprefix("trusted comment: ").split("\t") if item.startswith("file:")]
        if names != [asset["name"]]:
            raise ValueError(f"updater signed filename differs: {role}")
        subprocess.run([sys.executable, str(source / "desktop/scripts/policy/sign_policy.py"), "verify",
                        "--in", str(directory / asset["name"]), "--sig", str(signature),
                        "--pubkey-conf", str(pubkey_conf)], check=True)


def _check_release(release, tag, *, prerelease):
    if (release.get("tag_name") != tag or release.get("draft") is not False
            or release.get("prerelease") is not prerelease):
        raise ValueError(f"unexpected GitHub release status: {tag}")


def _legacy_downloads(tag):
    specs = rc.asset_specs(tag)
    return {platform: f"{rc.BASE_URL}/{tag}/{specs[role]['name']}" for platform, role in rc.DOWNLOAD_ROLES.items()}


def _legacy_entry(release, tag, notes):
    names = [asset.get("name") for asset in release["assets"]]
    if len(names) != len(set(names)):
        raise ValueError("legacy release has duplicate assets")
    links = _legacy_downloads(tag)
    uploaded = {asset.get("name") for asset in release["assets"] if asset.get("state") == "uploaded"}
    for platform, link in links.items():
        if link.rsplit("/", 1)[1] not in uploaded:
            raise ValueError(f"legacy release missing exact installer: {platform}")
    return {"tag": tag, "notes": notes, "downloads": links, "release_date": _utc_date(release["published_at"])}


def _candidate_inventory(release, tag):
    _check_release(release, tag, prerelease=True)
    allowed = {rc.MANIFEST_NAME, PENDING_NAME, *(spec["name"] for spec in rc.asset_specs(tag).values())}
    assets = {asset.get("name"): asset for asset in release["assets"]}
    if len(assets) != len(release["assets"]) or not assets.keys() <= allowed:
        raise ValueError("existing candidate has duplicate or unexpected assets; use a new tag")
    return assets


def _publication_key(tag):
    return f"releases/{tag}/candidate-publication.json"


def _read_publication(backend, tag, *, require_ready=False):
    record = backend.read_r2_json(_publication_key(tag))
    if record is not None:
        if set(record) != {"status", "candidate"} or record["status"] not in ("pending", "ready"):
            raise ValueError(f"invalid publication record for {tag}")
        rc.validate_candidate(record["candidate"], tag)
        if require_ready and record["status"] == "pending":
            candidate = record["candidate"]
            raise ValueError(f"candidate publication paused: {tag}; retry build {candidate['build_id']}")
    return record


def _write_publication(backend, work, candidate, status):
    key = _publication_key(candidate["tag"])
    path = work / "candidate-publication.json"
    rc.write_json(path, {"status": status, "candidate": candidate})
    backend.put_r2(path, key)
    _visible(lambda: backend.read_r2_bytes(key) == path.read_bytes(), f"R2 candidate publication {status}")


def _candidate_from_release(backend, release, directory, publication):
    if any(asset.get("name") == PENDING_NAME for asset in release["assets"]):
        raise ValueError(f"candidate publication paused: {release['tag_name']}; retry the matching build")
    manifests = [asset for asset in release["assets"] if asset.get("name") == rc.MANIFEST_NAME]
    if len(manifests) != 1 or not 0 < manifests[0].get("size", 0) <= rc.MANIFEST_MAX_BYTES:
        raise ValueError("release must have one candidate manifest within the size limit")
    directory.mkdir()
    path = directory / rc.MANIFEST_NAME
    backend.download_release(release["tag_name"], rc.MANIFEST_NAME, directory)
    content = rc.regular_file(path).read_bytes()
    candidate = rc.validate_candidate(rc.json_object(content.decode("utf-8")), release["tag_name"])
    rc.verify_release_assets(candidate, release, content)
    if publication is not None and candidate != publication["candidate"]:
        raise ValueError("GitHub candidate differs from the accepted R2 publication")
    return candidate


def _current_formal(backend, work):
    release = backend.release("latest")
    if release is None:
        return None, None
    tag = rc.release_tag(release.get("tag_name"), stable=True)
    _check_release(release, tag, prerelease=False)
    publication = _read_publication(backend, tag, require_ready=True)
    if publication is not None or any(asset.get("name") in (rc.MANIFEST_NAME, PENDING_NAME) for asset in release["assets"]):
        candidate = _candidate_from_release(backend, release, work / "current-formal", publication)
        return download_entry(candidate, _candidate_stamp(release)), candidate
    return _legacy_entry(release, tag, backend.legacy_notes(tag)), None


def _candidate_stamp(release):
    stamp = next(asset["created_at"] for asset in release["assets"] if asset["name"] == rc.MANIFEST_NAME)
    rc.timestamp(stamp)
    return stamp


def _positional_beta(backend, work, tag, identity):
    if "-" not in rc.release_tag(tag) or not isinstance(identity, str) or not rc.DIGEST_RE.fullmatch(identity):
        raise ValueError("invalid beta candidate reference")
    work.mkdir()
    path = work / rc.MANIFEST_NAME
    backend.download_r2(f"releases/{tag}/{identity}/{rc.MANIFEST_NAME}", path)
    candidate = rc.validate_candidate(rc.read_json(rc.regular_file(path)), tag)
    if candidate["candidate_id"] != identity:
        raise ValueError("retained beta candidate ID mismatch")
    return candidate


def _page_beta(backend, work):
    page = backend.read_r2_bytes("index.html")
    if page is None:
        return None, None
    parser = _PageLinks()
    parser.feed(page.decode("utf-8"))
    formal_tags = parser.labels["ver-badge"]
    formal_tag = formal_tags[0] if len(formal_tags) == 1 else None
    links = parser.links["pre-block"]
    if not links and not parser.labels["pre-tag"]:
        return formal_tag, None
    parents = {link.rsplit("/", 1)[0] for link in links}
    if len(parents) != 1 or not all(link.startswith(rc.BASE_URL + "/") for link in links):
        raise ValueError("live beta homepage contains conflicting assets")
    relative = parents.pop().removeprefix(rc.BASE_URL + "/").split("/")
    tag = relative[0]
    if not tag or parser.labels["pre-tag"] != [tag]:
        raise ValueError("live beta homepage label does not identify its installers")
    rc.validate_pre_tag(tag)
    if len(relative) == 1:
        beta = {"tag": tag, "candidate_id": None, "downloads": _legacy_downloads(tag)}
    elif len(relative) == 2:
        beta = download_entry(_positional_beta(backend, work, *relative))
    else:
        raise ValueError("invalid beta homepage candidate path")
    if links != [beta["downloads"][platform] for platform in (rc.WIN, rc.MAC)]:
        raise ValueError("live beta homepage differs from its installer paths")
    return formal_tag, beta


def _pin_beta(backend, work, tag):
    publication = _read_publication(backend, tag, require_ready=True)
    release = backend.release(tag)
    if release is None:
        raise ValueError(f"selected beta release does not exist: {tag}")
    _check_release(release, tag, prerelease=True)
    candidate = _candidate_from_release(backend, release, work / "selected-gh", publication)
    return _positional_beta(backend, work / "selected-r2", tag, candidate["candidate_id"])


def _stable_identity(data, expected):
    return (data.get("version"), data.get("platforms")) == (expected.get("version"), expected.get("platforms"))


def _current_update(backend, current, work):
    work.mkdir()
    for role in rc.UPDATE_ROLES.values():
        signature_role = current["assets"][role]["signature"]
        asset = current["assets"][signature_role]
        path = work / asset["name"]
        backend.download_r2(rc.asset_key(current, signature_role), path)
        if rc.file_identity(path) != {key: asset[key] for key in ("size", "sha256")}:
            raise ValueError("GitHub Latest signature bytes differ from R2")
    return {"version": current["version"], "platforms": update_entry(current, work, "")["platforms"]}


def _distribution_state(backend, formal, current, work, next_update=None):
    primary = backend.read_r2_json("latest.json")
    expected = _current_update(backend, current, work / "current-signatures") if current else None
    resuming = next_update is not None and (
        (primary is not None and _stable_identity(primary, next_update))
        or (expected is not None and _stable_identity(expected, next_update))
    )
    try:
        mirror = backend.read_updates()
    except IncompleteUpdates:
        if not resuming:
            raise
        mirror = None
    version = formal["tag"][1:] if formal else None
    for channel, data in (("R2", primary), ("GitHub updates", mirror)):
        if resuming:
            if data is None or data.get("version") == next_update["version"]:
                continue
        if ((data.get("version") if data is not None else None) != version
                or (expected and (data is None or not _stable_identity(data, expected)))):
            raise ValueError(f"{channel} is out of sync; rerun the matching interrupted promotion first")
    if not resuming and primary is not None and mirror is not None and not _stable_identity(primary, mirror):
        raise ValueError("stable distribution has different updater signatures")


def _visible(check, failure):
    for delay in (0, 1, 2, 4, 8, 8):
        if delay:
            time.sleep(delay)
        try:
            if check():
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    raise ValueError(f"write not visible: {failure}; retry the same candidate")


def _put_page(backend, page, beta, work):
    for name in ("logo.png", "feishu-qr.png", "index.html"):
        path = page / name
        expected = path.read_bytes()
        backend.put_r2(path, name)
        _visible(lambda: backend.read_r2_bytes(name) == expected, f"R2 homepage {name}")
    pointer = work / BETA_STATE
    rc.write_json(pointer, {key: beta[key] if beta else None for key in ("tag", "candidate_id")})
    backend.put_r2(pointer, BETA_STATE)
    _visible(lambda: backend.read_r2_bytes(BETA_STATE) == pointer.read_bytes(), "R2 beta mirror")


def candidate_source(tree, sha, destination):
    mode = ["git", "-C", str(tree), "archive", "--format=tar", rc.source_sha(sha), "--"]
    archive_bytes = subprocess.check_output([*mode, *CANDIDATE_SOURCE_FILES], timeout=60)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for name in CANDIDATE_SOURCE_FILES:
            member = archive.getmember(name)
            if not member.isfile() or not 0 < member.size <= 10 * 1024 * 1024:
                raise ValueError(f"invalid candidate source object: {name}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    return destination


def publish_build(backend, source, work, tag, sha, build, notes, pre_tag, staged):
    rc.release_tag(tag)
    rc.validate_pre_tag(pre_tag)
    if rc.pinned_source(source) != sha:
        raise ValueError("renderer checkout differs from tested source SHA")
    beta_build = "-" in tag
    if beta_build:
        pre_tag = ""
    order = tuple(map(int, rc.build_version_id(build).split(".")))
    publication = _read_publication(backend, tag)
    existing = backend.release(tag)
    inventory = _candidate_inventory(existing, tag) if existing is not None else {}
    previous = publication["candidate"] if publication else None
    if publication is None and PENDING_NAME in inventory:
        raise ValueError(f"candidate publication paused: {tag}; missing R2 publication record")
    if publication is None and rc.MANIFEST_NAME in inventory:
        previous = _candidate_from_release(backend, existing, work / "previous-candidate", None)
    same_build = False
    if previous is not None:
        previous_order = tuple(map(int, previous["build_id"].split(".")))
        if order < previous_order:
            raise ValueError(f"stale build {build} for {tag}; accepted build is {previous['build_id']}")
        same_build = order == previous_order
        if same_build and pre_tag != previous["pre_tag"]:
            raise ValueError("retry must use the original pre_tag")

    pre = None
    if pre_tag:
        if same_build:
            pre = _positional_beta(backend, work / "pinned-beta", pre_tag, previous["pre_candidate_id"])
        else:
            pre = _pin_beta(backend, work, pre_tag)
    directory = work / "assets"
    candidate = rc.assemble_candidate(tag, sha, build, notes, pre_tag, pre["candidate_id"] if pre else None,
                                      staged, directory)
    if same_build and candidate != previous:
        raise ValueError("retry must use the original candidate inputs and staged bytes")
    verify_signatures(source, candidate, directory)
    manifest = directory / rc.MANIFEST_NAME
    accepted = publication is not None and same_build
    ready = accepted and publication["status"] == "ready"
    github_matches = _attachment_set_matches(candidate, existing, manifest)
    if ready and not github_matches:
        raise ValueError(f"published GitHub candidate differs from the accepted publication: {tag}")
    formal, current = _current_formal(backend, work)
    _distribution_state(backend, formal, current, work)
    if beta_build:
        if formal is None:
            raise ValueError("no established formal release for the beta homepage")
        page_formal, page_beta = formal, download_entry(candidate)
    else:
        if formal and rc.stable_version(tag) <= rc.stable_version(formal["tag"]):
            raise ValueError("formal candidate must be newer than GitHub Latest")
        page_formal = download_entry(candidate)
        page_beta = download_entry(pre) if pre else None
    outputs = work / "output"
    render_outputs(source, outputs, page_formal, page_beta, update_entry(candidate, directory, candidate["created_at"]))

    if not accepted:
        _write_publication(backend, work, candidate, "pending")
    if not ready:
        _put_candidate_files(backend, work, candidate, directory)
        if not github_matches:
            backend.publish_candidate(candidate, directory, exists=existing is not None)
        _visible(lambda: _release_matches(backend, tag, candidate, manifest, prerelease=True), "GitHub candidate assets")
        _write_publication(backend, work, candidate, "ready")
    if beta_build:
        _put_page(backend, outputs / "page", page_beta, work)
    return candidate


def _put_candidate_files(backend, work, candidate, directory):
    readback = work / "candidate-readback"
    readback.mkdir()
    for role, asset in candidate["assets"].items():
        key = rc.asset_key(candidate, role)
        copied = readback / asset["name"]
        backend.put_r2(directory / asset["name"], key)

        def visible():
            try:
                backend.download_r2(key, copied)
                return rc.file_identity(copied) == {field: asset[field] for field in ("size", "sha256")}
            finally:
                copied.unlink(missing_ok=True)

        _visible(visible, f"R2 candidate asset {role}")
    key = f"releases/{candidate['tag']}/{candidate['candidate_id']}/{rc.MANIFEST_NAME}"
    manifest = directory / rc.MANIFEST_NAME
    backend.put_r2(manifest, key)
    _visible(lambda: backend.read_r2_bytes(key) == manifest.read_bytes(), "R2 candidate manifest")


def _attachment_set_matches(candidate, release, manifest):
    if release is None:
        return False
    try:
        rc.verify_release_assets(candidate, release, manifest.read_bytes())
    except ValueError:
        return False
    return True


def _release_matches(backend, reference, candidate, manifest, *, prerelease):
    release = backend.release(reference)
    if release is None:
        return False
    _check_release(release, candidate["tag"], prerelease=prerelease)
    rc.verify_release_assets(candidate, release, manifest.read_bytes())
    return True


def promote(backend, evidence_source, work, tag, windows, macos):
    rc.release_tag(tag, stable=True)
    publication = _read_publication(backend, tag, require_ready=True)
    release = backend.release(tag)
    if release is None or release.get("draft") is not False or release.get("tag_name") != tag:
        raise ValueError(f"candidate release unavailable: {tag}")
    formal, current = _current_formal(backend, work)
    if formal and rc.stable_version(tag) < rc.stable_version(formal["tag"]):
        raise ValueError("promotion would downgrade GitHub Latest")
    if release.get("prerelease") is not True and (not current or current["tag"] != tag):
        raise ValueError("only a new candidate or the same Latest may be promoted")
    directory = work / "candidate"
    candidate = _candidate_from_release(backend, release, directory, publication)
    stamp = _candidate_stamp(release)
    verify_evidence(candidate, windows, macos, published_at=stamp)
    source = candidate_source(evidence_source, candidate["source_sha"], work / "candidate-source")
    for role, asset in candidate["assets"].items():
        backend.download_r2(rc.asset_key(candidate, role), directory / asset["name"])
    rc.verify_files(candidate, directory)
    verify_signatures(source, candidate, directory)
    update = update_entry(candidate, directory, stamp)
    _distribution_state(backend, formal, current, work, {"version": tag[1:], "platforms": update["platforms"]})
    visible_formal, beta = _page_beta(backend, work / "visible-beta")
    if visible_formal != tag and candidate["pre_tag"]:
        beta = download_entry(_positional_beta(backend, work / "pinned-beta", candidate["pre_tag"],
                                               candidate["pre_candidate_id"]))
    outputs = work / "output"
    render_outputs(source, outputs, download_entry(candidate, stamp), beta, update)

    latest = outputs / "latest.json"
    backend.put_r2(latest, "latest.json")
    _visible(lambda: backend.read_r2_bytes("latest.json") == latest.read_bytes(), "R2 stable manifest")
    try:
        updates = backend.read_updates_bytes()
    except IncompleteUpdates:
        updates = None
    if updates != latest.read_bytes():
        backend.publish_updates(latest)
    _visible(lambda: backend.read_updates_bytes() == latest.read_bytes(), "GitHub updates manifest")
    if current is None or current["tag"] != tag:
        backend.mark_latest(tag)
    manifest = directory / rc.MANIFEST_NAME
    _visible(lambda: _release_matches(backend, "latest", candidate, manifest, prerelease=False), "GitHub Latest")
    _put_page(backend, outputs / "page", beta, work)
    backend.mirror_stable(candidate, latest)
    return candidate


class WorkflowBackend:
    def __init__(self, work):
        self.work = work
        self.s3 = f"s3://{os.environ['R2_BUCKET']}/"
        self.endpoint = f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"

    def _gh_json(self, route, *, optional=False):
        result = subprocess.run(["gh", "api", route], capture_output=True, text=True)
        if result.returncode:
            if optional and "(HTTP 404)" in result.stderr:
                return None
            raise ValueError(f"GitHub API failed for {route}: {result.stderr.strip()}")
        return rc.json_object(result.stdout)

    def release(self, tag):
        route = "latest" if tag == "latest" else f"tags/{rc.release_tag(tag)}"
        return self._gh_json(f"repos/{HUB}/releases/{route}", optional=True)

    def download_release(self, tag, name, directory):
        subprocess.run(["gh", "release", "download", tag, "-R", HUB, "--pattern", name,
                        "--dir", str(directory)], check=True)

    def read_updates_bytes(self):
        release = self._gh_json(f"repos/{HUB}/releases/tags/updates", optional=True)
        if release is None:
            raise ValueError("GitHub updates release must exist before publication")
        hits = [asset for asset in release["assets"] if asset.get("name") == "latest.json"]
        if not hits:
            return None
        if len(hits) != 1:
            raise ValueError("duplicate GitHub updates manifests")
        asset = hits[0]
        digest = asset.get("digest")
        if (asset.get("state") != "uploaded" or not 0 < asset.get("size", 0) <= rc.MANIFEST_MAX_BYTES
                or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
            raise IncompleteUpdates("GitHub updates has an incomplete manifest attachment")
        with tempfile.TemporaryDirectory(dir=self.work) as tmp:
            directory = Path(tmp)
            self.download_release("updates", "latest.json", directory)
            path = directory / "latest.json"
            content = path.read_bytes()
            if len(content) != asset["size"] or hashlib.sha256(content).hexdigest() != digest.removeprefix("sha256:"):
                raise IncompleteUpdates("GitHub updates attachment bytes disagree with its inventory")
            return content

    def read_updates(self):
        data = self.read_updates_bytes()
        if data is None:
            return None
        try:
            return rc.json_object(data.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise IncompleteUpdates("GitHub updates has invalid manifest bytes") from exc

    def _aws(self, *args, **kwargs):
        return subprocess.run(["aws", "s3", *args, "--endpoint-url", self.endpoint, "--no-progress"], **kwargs)

    def download_r2(self, key, path):
        self._aws("cp", self.s3 + key, str(path), check=True)

    def read_r2_bytes(self, key):
        result = self._aws("cp", self.s3 + key, "-", capture_output=True)
        if result.returncode:
            error = result.stderr.decode(errors="replace")
            if "(404)" in error or "(NoSuchKey)" in error:
                return None
            raise ValueError(f"R2 read failed for {key}: {error.strip()}")
        return result.stdout

    def read_r2_json(self, key):
        data = self.read_r2_bytes(key)
        return rc.json_object(data.decode("utf-8")) if data is not None else None

    def put_r2(self, path, key):
        self._aws("cp", str(path), self.s3 + key, check=True)

    def _delete_asset(self, asset):
        asset_id = asset.get("id")
        if type(asset_id) is not int or asset_id <= 0:
            raise ValueError("GitHub attachment has no asset ID")
        subprocess.run(["gh", "api", "--method", "DELETE", f"repos/{HUB}/releases/assets/{asset_id}"], check=True)

    def publish_candidate(self, candidate, directory, *, exists):
        tag = candidate["tag"]
        if not exists:
            subprocess.run(["gh", "release", "create", tag, "-R", HUB, "--prerelease", "--latest=false",
                            "--generate-notes"], check=True)
            _visible(lambda: self.release(tag) is not None, f"GitHub candidate release {tag}")

        def inventory():
            release = self.release(tag)
            if release is None:
                raise ValueError(f"GitHub candidate release unavailable: {tag}")
            return _candidate_inventory(release, tag)

        def remove(name):
            asset = inventory().get(name)
            if asset is not None:
                self._delete_asset(asset)
                _visible(lambda: name not in inventory(), f"GitHub removal of {name}")

        def upload(path, identity):
            def matches():
                asset = inventory().get(path.name, {})
                return (asset.get("state") == "uploaded" and asset.get("size") == identity["size"]
                        and asset.get("digest") == f"sha256:{identity['sha256']}")

            if matches():
                return
            remove(path.name)
            subprocess.run(["gh", "release", "upload", tag, str(path), "-R", HUB], check=True)
            _visible(matches, f"GitHub attachment {path.name}")

        manifest = directory / rc.MANIFEST_NAME
        marker = directory / PENDING_NAME
        rc.write_json(marker, {"tag": tag, "status": "pending"})
        upload(marker, rc.file_identity(marker))

        def complete():
            assets = inventory()
            if PENDING_NAME not in assets:
                return False
            paused = {"tag_name": tag, "assets": [asset for name, asset in assets.items() if name != PENDING_NAME]}
            return _attachment_set_matches(candidate, paused, manifest)

        if not complete():
            remove(rc.MANIFEST_NAME)
            for asset in candidate["assets"].values():
                upload(directory / asset["name"], asset)
            upload(manifest, rc.file_identity(manifest))
            _visible(complete, "GitHub candidate attachment set")
        remove(PENDING_NAME)

    def publish_updates(self, latest):
        release = self._gh_json(f"repos/{HUB}/releases/tags/updates")
        matches = [asset for asset in release["assets"] if asset.get("name") == "latest.json"]
        if len(matches) > 1:
            raise ValueError("duplicate GitHub updates manifests")
        if matches and matches[0].get("state") == "starter":
            self._delete_asset(matches[0])
        subprocess.run(["gh", "release", "upload", "updates", str(latest), "-R", HUB, "--clobber"], check=True)

    def mark_latest(self, tag):
        subprocess.run(["gh", "release", "edit", tag, "-R", HUB, "--latest", "--prerelease=false"], check=True)

    def legacy_notes(self, tag):
        result = subprocess.run(["git", "show", "origin/main:page_state.json"], capture_output=True, text=True)
        if result.returncode != 0:
            return ""
        state = rc.json_object(result.stdout)
        notes = state.get("notes")
        return notes if state.get("tag") == tag and isinstance(notes, str) else ""

    def mirror_stable(self, candidate, latest):
        subprocess.run(["git", "config", "user.name", "crediscope-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "bot@crediscope.local"], check=True)
        for _ in range(3):
            subprocess.run(["git", "fetch", "origin", "main"], check=True)
            subprocess.run(["git", "switch", "-C", "release-state", "origin/main"], check=True)
            Path("latest.json").write_bytes(latest.read_bytes())
            rc.write_json(Path("page_state.json"), {"tag": candidate["tag"], "notes": candidate["notes"],
                          "candidate_id": candidate["candidate_id"]})
            subprocess.run(["git", "add", "latest.json", "page_state.json"], check=True)
            if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
                return
            subprocess.run(["git", "commit", "-m", f"Publish {candidate['tag']} stable state"], check=True)
            if subprocess.run(["git", "push", "origin", "HEAD:main"], check=False).returncode == 0:
                expected_page = Path("page_state.json").read_bytes()

                def visible():
                    subprocess.run(["git", "fetch", "origin", "main"], check=True)
                    return (subprocess.check_output(["git", "show", "origin/main:latest.json"]) == latest.read_bytes()
                            and subprocess.check_output(["git", "show", "origin/main:page_state.json"]) == expected_page)

                _visible(visible, "Git stable state mirror")
                return
        raise ValueError("stable publication completed; Git mirror push needs a retry")


def prepare_evidence(source, directory, output):
    manifest = rc.regular_file(directory / rc.MANIFEST_NAME)
    candidate = rc.validate_candidate(rc.read_json(manifest))
    rc.release_tag(candidate["tag"], stable=True)
    if rc.pinned_source(source) != candidate["source_sha"]:
        raise ValueError("evidence preparation needs the application candidate checkout")
    rc.verify_files(candidate, directory)
    verify_signatures(source, candidate, directory)
    output.mkdir(parents=True, exist_ok=False)
    update_input = output / "updater-input.json"
    rc.write_json(update_input, update_entry(candidate, directory, datetime.now(timezone.utc).isoformat()))
    subprocess.run([sys.executable, str(source / "desktop/scripts/gen_latest_json.py"),
                    "--render-input", str(update_input), "--out", str(output / "latest.json")], check=True)

    receipts = {}
    for kind in ("dmg", "updater"):
        asset = candidate["assets"][f"macos-{kind}"]
        receipts[kind] = {"artifact_name": asset["name"], "artifact_sha256": asset["sha256"],
                          "observed_version": None, "observed_arch": None, "running_executables": None, "result": "pending",
                          "tested_at": None, "observation": ""}
    receipts["dmg"].update(mounted=None, installed=None, launched=None)
    receipts["updater"].update(native_updater=None, restarted=None, from_version=None,
                               signature_sha256=candidate["assets"]["macos-signature"]["sha256"])
    rc.write_json(output / "macos-evidence.json", {"evidence_schema": 2, "platform": rc.MAC, "tag": candidate["tag"],
                  "candidate_id": candidate["candidate_id"], "source_sha": candidate["source_sha"],
                  "build_id": candidate["build_id"], "candidate_created_at": candidate["created_at"],
                  "receipts": receipts})
    return candidate


def _evidence_file(source, ref):
    if not re.fullmatch(r"v[0-9][0-9A-Za-z.-]*\.json", ref) or ".." in ref:
        raise ValueError("release evidence reference must be a golden-runs JSON basename")
    return rc.read_json(rc.regular_file(source / "docs/testing/golden-runs" / ref))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build, promotion = commands.add_parser("build"), commands.add_parser("promote")
    for command in (build, promotion):
        command.add_argument("--tag", required=True)
        command.add_argument("--source", type=Path, required=True)
    build.add_argument("--sha", required=True)
    build.add_argument("--build-id", required=True)
    build.add_argument("--notes", default="")
    build.add_argument("--pre-tag", default="")
    build.add_argument("--mac", type=Path, required=True)
    build.add_argument("--windows", type=Path, required=True)
    promotion.add_argument("--windows-evidence", required=True)
    promotion.add_argument("--macos-evidence", required=True)
    prepare = commands.add_parser("prepare-evidence", help="prepare an isolated updater manifest and a macOS draft")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--assets", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "prepare-evidence":
            prepare_evidence(args.source, args.assets, args.out)
            return
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            backend = WorkflowBackend(work)
            if args.action == "build":
                candidate = publish_build(backend, args.source, work, args.tag, args.sha, args.build_id, args.notes, args.pre_tag,
                                          {rc.MAC: args.mac, rc.WIN: args.windows})
            else:
                candidate = promote(backend, args.source, work, args.tag,
                                    _evidence_file(args.source, args.windows_evidence),
                                    _evidence_file(args.source, args.macos_evidence))
            print(f"{args.action} {candidate['tag']} source={candidate['source_sha']} candidate={candidate['candidate_id']}")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        parser.exit(1, f"release publication: {exc}\n")


if __name__ == "__main__":
    main()
