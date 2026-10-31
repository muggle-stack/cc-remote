"""Attach a pinned Harmony HAP to every release without misrepresenting its build."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

from deploy.validate_protocol_bundle import backend_product_version


class HarmonyReleaseError(ValueError):
    pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def verify_native_source(root: Path, source_commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise HarmonyReleaseError("Harmony build source must be a full commit SHA")
    _git(root, "cat-file", "-e", f"{source_commit}:harmony/AppScope/app.json5")
    # Documentation and test-only edits do not change the installed native app.
    changed = _git(
        root, "diff", "--name-only", source_commit, "--", "harmony/",
        ":(exclude)harmony/README.md", ":(exclude)harmony/tests/",
        ":(exclude)harmony/playwright.config.mjs",
    )
    if changed:
        raise HarmonyReleaseError(
            "Harmony native sources changed; build and verify a new unsigned HAP "
            "and update deploy/harmony-release.json before publishing:\n" + changed
        )


def validate_hap(path: Path, pin: dict) -> bytes:
    payload = path.read_bytes()
    if len(payload) != pin["bytes"]:
        raise HarmonyReleaseError("Harmony HAP size does not match its pinned build")
    if hashlib.sha256(payload).hexdigest() != pin["sha256"]:
        raise HarmonyReleaseError("Harmony HAP SHA256 does not match its pinned build")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if archive.testzip() is not None:
            raise HarmonyReleaseError("Harmony HAP archive is corrupt")
        app = json.loads(archive.read("module.json"))["app"]
    native = pin["harmony"]
    expected = {
        "bundleName": native["bundle_name"],
        "versionName": native["version_name"],
        "versionCode": native["version_code"],
        "buildMode": native["build"],
        "compileSdkVersion": native["compile_sdk_version"],
    }
    if any(app.get(key) != value for key, value in expected.items()):
        raise HarmonyReleaseError("Harmony HAP native identity does not match its metadata")
    return payload


def prepare(
    root: Path, output_dir: Path, *, asset_file: Path | None = None,
) -> str:
    pin = json.loads((root / "deploy/harmony-release.json").read_text())
    if pin["schema"] != 1 or pin["harmony"]["signed"] is not False:
        raise HarmonyReleaseError("Expected a pinned unsigned Harmony release build")
    version = backend_product_version(root / "cc_remote/__init__.py")
    commit = _git(root, "rev-parse", "HEAD")
    verify_native_source(root, pin["source"]["commit"])
    filename = f"cc-remote-harmony-v{version}-unsigned.hap"
    with tempfile.TemporaryDirectory(prefix="cc-remote-harmony-") as temporary:
        if asset_file is None:
            subprocess.run([
                "gh", "release", "download", pin["release"],
                "--repo", pin["repository"], "--pattern", pin["asset"],
                "--dir", temporary,
            ], check=True)
            asset_file = Path(temporary) / pin["asset"]
        payload = validate_hap(asset_file, pin)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / filename).write_bytes(payload)
    metadata = {
        "schema": 1,
        "harmony": pin["harmony"],
        "source": pin["source"],
        "assets": {filename: {"sha256": pin["sha256"], "bytes": pin["bytes"]}},
        "reuse": {
            "release": f"v{version}", "commit": commit,
            "from_release": pin["release"], "from_asset": pin["asset"],
            "byte_for_byte": True,
            "validation": "Native source equality, pinned SHA256/size, ZIP integrity "
                          "and native app identity verified; no new device acceptance",
        },
    }
    info = output_dir / "MOBILE_BUILD_INFO.json"
    info.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    (output_dir / "MOBILE_SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted([info, output_dir / filename])
    ))
    return (
        "\n\n### 鸿蒙 App / HarmonyOS app\n\n"
        f"本次附件包含 `{filename}`，原生版本为 "
        f"**{pin['harmony']['version_name']}**（versionCode {pin['harmony']['version_code']}）。"
        "原生代码未变化，安装包按原始字节复用；已有对应版本无需重装。"
        "HAP 未签名，需要使用自己的开发者及设备 Profile 签名后安装。"
        "构建来源和校验值见 `MOBILE_BUILD_INFO.json`、`MOBILE_SHA256SUMS`。\n\n"
        "The attached unsigned Harmony HAP reuses the verified native build byte-for-byte. "
        "Its app version is independent of the cc-remote release version. "
        "It requires your own developer/device signing profile. "
        "The original local native build is not GitHub Actions attested; "
        "existing users of this app version need no native reinstall.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--asset-file", type=Path, help="Validate a previously downloaded HAP")
    parser.add_argument("--notes-file", type=Path, required=True)
    args = parser.parse_args()
    notes = prepare(args.root, args.output_dir, asset_file=args.asset_file)
    args.notes_file.write_text(notes)


if __name__ == "__main__":
    main()
