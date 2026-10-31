"""Release assets must contain a verified HAP matching the current native sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from deploy.prepare_harmony_release import HarmonyReleaseError, prepare


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_source(tmp_path: Path):
    root = tmp_path / "source"
    (root / "harmony/AppScope").mkdir(parents=True)
    (root / "harmony/entry/src").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "cc_remote").mkdir()
    (root / "cc_remote/__init__.py").write_text('__version__ = "9.9.9"\n')
    pin = json.loads((ROOT / "deploy/harmony-release.json").read_text())
    native = pin["harmony"]
    app = {
        "bundleName": native["bundle_name"], "versionName": native["version_name"],
        "versionCode": native["version_code"], "buildMode": native["build"],
        "compileSdkVersion": native["compile_sdk_version"],
    }
    (root / "harmony/AppScope/app.json5").write_text(json.dumps({"app": app}))
    (root / "harmony/entry/src/main.ets").write_text("original native source\n")
    (root / "harmony/README.md").write_text("original docs\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
        "-c", "commit.gpgsign=false", "commit", "-qm", "native build source",
    ], cwd=root, check=True)
    pin["source"]["commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()
    hap = tmp_path / "original.hap"
    with zipfile.ZipFile(hap, "w") as archive:
        archive.writestr("module.json", json.dumps({"app": app}))
        archive.writestr("ets/modules.abc", b"native bytecode fixture")
    pin["sha256"] = hashlib.sha256(hap.read_bytes()).hexdigest()
    pin["bytes"] = hap.stat().st_size
    (root / "deploy/harmony-release.json").write_text(json.dumps(pin))
    return root, hap, tmp_path / "dist", pin


def test_republishes_exact_bytes_and_preserves_original_native_provenance(release_source):
    root, hap, output, pin = release_source
    (root / "harmony/README.md").write_text("new publishing instructions\n")

    notes = prepare(root, output, asset_file=hap)

    published = output / "cc-remote-harmony-v9.9.9-unsigned.hap"
    assert published.read_bytes() == hap.read_bytes()
    info = json.loads((output / "MOBILE_BUILD_INFO.json").read_text())
    assert info["source"] == pin["source"]
    assert info["harmony"] == pin["harmony"]
    assert info["reuse"]["byte_for_byte"] is True
    assert info["reuse"]["release"] == "v9.9.9"
    assert "0.1.0-preview.5" in notes and published.name in notes
    assert "not GitHub Actions attested" in notes
    checksums = (output / "MOBILE_SHA256SUMS").read_text().splitlines()
    assert len(checksums) == 2
    for line in checksums:
        digest, name = line.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("changed", [
    "harmony/entry/src/main.ets", "harmony/AppScope/app.json5",
    "harmony/new-build-input.json5",
])
def test_native_changes_block_stale_hap_before_any_assets_are_written(release_source, changed):
    root, hap, output, _ = release_source
    (root / changed).write_text("changed native input\n")
    subprocess.run(["git", "add", changed], cwd=root, check=True)

    with pytest.raises(HarmonyReleaseError, match="native sources changed"):
        prepare(root, output, asset_file=hap)
    assert not output.exists()


@pytest.mark.parametrize("corruption", ["bytes", "size", "metadata"])
def test_rejects_replaced_assets_and_mismatched_native_versions(release_source, corruption):
    root, hap, output, pin = release_source
    if corruption == "bytes":
        data = bytearray(hap.read_bytes())
        data[-1] ^= 1
        hap.write_bytes(data)
    elif corruption == "size":
        hap.write_bytes(hap.read_bytes() + b"extra")
    else:
        pin["harmony"]["version_code"] += 1
        (root / "deploy/harmony-release.json").write_text(json.dumps(pin))

    with pytest.raises(HarmonyReleaseError, match="size|SHA256|native identity"):
        prepare(root, output, asset_file=hap)
    assert not output.exists()


def test_unavailable_pinned_download_fails_release(release_source, monkeypatch):
    root, _, output, _ = release_source
    original_run = subprocess.run

    def unavailable(args, **kwargs):
        if args[0] == "gh":
            raise subprocess.CalledProcessError(1, args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        prepare(root, output)
    assert not output.exists()


def test_workflow_requires_hap_before_publish_and_keeps_native_build_out_of_attestation():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    attest = workflow.index("name: Attest release assets")
    harmony = workflow.index("name: Prepare required HarmonyOS release assets")
    publish = workflow.index("name: Create draft, upload assets, and publish")
    assert attest < harmony < publish
    assert "continue-on-error" not in workflow[harmony:publish]
    assert "python -m deploy.prepare_harmony_release" in workflow[harmony:publish]
    assert 'gh release upload "$RELEASE_TAG" dist/*' in workflow[publish:]
    assert 'cat "$RUNNER_TEMP/harmony-release-notes.md" >> "$notes_file"' in workflow[publish:]
