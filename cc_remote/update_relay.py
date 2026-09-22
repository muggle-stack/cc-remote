"""Coordinate a device update with its configured Relay's managed installer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from dotenv import dotenv_values

from cc_remote.update import Installation, UpdateError, version_tuple
from deploy.install_cli import _atomic_file
from deploy.release_manifest import load_manifest


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def relay_origin(installation: Installation) -> str:
    if installation.manifest["os"] == "darwin":
        environment = {}
        label = installation.metadata.get("service_label", "com.muggle.cc-remote.wrapper")
        service = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        try:
            if service.exists():
                environment = plistlib.loads(service.read_bytes()).get("EnvironmentVariables", {})
            value = environment.get("RELAY_URL")
            if not value:
                path = Path(environment.get("CC_REMOTE_DEVICE_CONFIG") or Path.home() / ".cc-remote/device.json")
                value = json.loads(path.read_text()).get("relay_url")
        except (OSError, ValueError, AttributeError) as exc:
            raise UpdateError("cannot read this device's Relay URL; check its pairing configuration") from exc
    else:
        config = {**dotenv_values("/etc/cc-remote/wrapper.env"),
                  **dotenv_values("/etc/cc-remote/device.env")}
        value = config.get("RELAY_URL")
    if not isinstance(value, str):
        raise UpdateError("this device has no configured Relay URL")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"ws", "wss", "http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise UpdateError("invalid configured Relay URL")
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    return f"{scheme}://{parsed.netloc}"


def _get_json(url: str) -> dict:
    try:
        request = Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "cc-remote-updater"})
        with build_opener(_NoRedirect).open(request, timeout=15) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("oversized response")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("invalid response")
        return value
    except (OSError, ValueError) as exc:
        raise UpdateError("cannot verify Relay health/version; no local service was changed") from exc


def relay_release(origin: str) -> dict:
    health = _get_json(f"{origin}/healthz")
    if health.get("ok") is not True:
        raise UpdateError("Relay is not healthy; inspect it before updating this device")
    # v4.0.1 advertised product version only in the served Web manifest.
    manifest = _get_json(f"{origin}/cc-remote-build.json")
    version_tuple(manifest.get("version"))
    protocol = manifest.get("protocol")
    if (type(protocol) is not int or protocol < 1 or protocol != health.get("protocol")
            or health.get("version", manifest["version"]) != manifest["version"]):
        raise UpdateError("Relay and Web versions are inconsistent; inspect the server deployment")
    return {"version": manifest["version"], "protocol": protocol}


def _ssh_target(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_\[][A-Za-z0-9_.:\[\]-]{0,253}", value,
    ):
        raise UpdateError("--relay-ssh must be an SSH host alias or user@host; use ~/.ssh/config for ports/keys")
    return value


class RelayUpdate:
    def __init__(self, installation: Installation, ssh_target: str | None = None):
        self.installation = installation
        self.origin = relay_origin(installation)
        self.settings = installation.root / "update.json"
        self.journal = installation.root / "upstream-update.json"
        stored = {}
        if self.settings.exists():
            try:
                stored = json.loads(self.settings.read_text())
                if not isinstance(stored, dict):
                    raise ValueError("invalid settings")
            except (OSError, ValueError) as exc:
                raise UpdateError("invalid update.json; inspect the saved Relay update configuration") from exc
        target = ssh_target or os.environ.get("CC_REMOTE_RELAY_SSH") or stored.get("relay_ssh")
        self.target = _ssh_target(target) if target else None

    def inspect(self) -> dict:
        info = relay_release(self.origin)
        print(f"Relay {self.origin}: v{info['version']} (protocol {info['protocol']})", flush=True)
        return info

    def save_settings(self) -> None:
        if self.target:
            _atomic_file(self.settings, (json.dumps({"relay_ssh": self.target}) + "\n").encode(), 0o600)

    def _ssh(self, command: list[str], *, timeout: int = 30) -> str:
        if not self.target:
            raise UpdateError(
                "Relay also needs updating. Set CC_REMOTE_RELAY_SSH=user@host, "
                "or use --relay-ssh user@host with the new updater/installer; "
                "its SSH account must be allowed to run the managed installer with sudo"
            )
        args = ["/usr/bin/ssh", "-oBatchMode=yes", "-oConnectTimeout=10",
                "-oServerAliveInterval=10", "-oServerAliveCountMax=2", "--", self.target,
                shlex.join(command)]
        if self.installation.manifest["os"] == "linux" and os.geteuid() == 0:
            # Local activation runs as root, SSH remains the original user's
            # identity. Do not copy private keys or use root's unrelated config.
            user = self.installation.metadata["user"]
            try:
                pwd.getpwnam(user)
            except KeyError as exc:
                raise UpdateError("the original Wrapper service user no longer exists") from exc
            args = ["sudo", "-H", "-u", user, "--", *args]
        try:
            response = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("Relay SSH connection unavailable; inspect the recorded upstream transaction before retrying") from exc
        if response.returncode:
            # Do not print arbitrary remote output or credential helper output.
            raise UpdateError("Relay SSH command failed; inspect SSH/sudo access and the recorded upstream transaction")
        return response.stdout

    def _verify_host(self) -> None:
        script = (
            "import json,sys;sys.path.insert(0,'/opt/cc-remote/current');"
            "from cc_remote.update import read_installation,host_platform;"
            "from pathlib import Path;"
            "i=read_installation(Path('/opt/cc-remote'),*host_platform());"
            "print(json.dumps({'domain':i.metadata['domain'],'version':i.manifest['product_version']}))"
        )
        try:
            info = json.loads(self._ssh(["sudo", "-n", "/opt/cc-remote/current/.venv/bin/python",
                                         "-I", "-c", script]))
        except (ValueError, TypeError) as exc:
            raise UpdateError("cannot verify the SSH host's managed Relay installation") from exc
        if not isinstance(info, dict) or info.get("domain") != urlsplit(self.origin).hostname:
            raise UpdateError("the SSH host does not manage this device's Relay domain; nothing was activated")

    def _write_transaction(self, transaction: dict) -> None:
        _atomic_file(self.journal, (json.dumps(transaction, sort_keys=True) + "\n").encode(), 0o600)

    def _finish(self, transaction: dict) -> None:
        unit = transaction.get("unit")
        if (not isinstance(unit, str) or not re.fullmatch(r"cc-remote-update-[a-f0-9]{32}", unit)
                or transaction.get("origin") != self.origin or transaction.get("ssh") != self.target):
            raise UpdateError("upstream transaction does not match this Relay; inspect upstream-update.json")
        version_tuple(transaction.get("version"))
        if type(transaction.get("protocol")) is not int or transaction["protocol"] < 1:
            raise UpdateError("invalid upstream transaction protocol; inspect upstream-update.json")
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            output = self._ssh(["sudo", "-n", "systemctl", "show", f"{unit}.service",
                                "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus"])
            state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            if state.get("LoadState") != "loaded":
                raise UpdateError(f"Relay transaction outcome unknown: inspect {unit}.service; no second update was started")
            if state.get("ActiveState") == "failed":
                raise UpdateError(f"Relay update failed: inspect journalctl -u {unit}.service and its rollback report")
            if state.get("SubState") == "exited":
                if state.get("Result") != "success" or state.get("ExecMainStatus") != "0":
                    raise UpdateError(f"Relay update did not succeed; inspect {unit}.service")
                info = self.inspect()
                if (info["version"] != transaction["version"]
                        or info["protocol"] != transaction["protocol"]):
                    raise UpdateError("Relay updater exited but the public version does not match; local activation stopped")
                self._write_transaction({**transaction, "complete": True})
                return
            time.sleep(2)
        raise UpdateError(f"Relay update still pending: {unit}.service; run update again to inspect the same transaction")

    def ensure(self, version: str, protocol: int, *, allow_protocol_change: bool) -> None:
        # A lost SSH acknowledgement is not permission to launch a second job.
        if self.journal.exists():
            try:
                transaction = json.loads(self.journal.read_text())
                if not isinstance(transaction, dict):
                    raise ValueError("invalid journal")
            except (OSError, ValueError) as exc:
                raise UpdateError("cannot read upstream-update.json; inspect the previous update") from exc
            if transaction.get("complete") is not True:
                self._finish(transaction)
        info = self.inspect()
        if version_tuple(info["version"]) >= version_tuple(version):
            if info["protocol"] != protocol:
                raise UpdateError("Relay is newer but incompatible with the selected device release; select a matching release")
            print("Relay is already current; updating this device only.", flush=True)
            self.save_settings()
            return
        if info["protocol"] != protocol and not allow_protocol_change:
            raise UpdateError("Relay protocol changes; arrange the device upgrades and repeat with --allow-protocol-change")
        if not self.target and sys.stdin.isatty():
            try:
                self.target = _ssh_target(input("Relay needs updating. Existing SSH admin host (user@host or alias): ").strip())
            except (EOFError, KeyboardInterrupt) as exc:
                raise UpdateError("Relay update cancelled; no local service was changed") from exc
        self._verify_host()
        self.save_settings()
        unit = f"cc-remote-update-{uuid.uuid4().hex}"
        transaction = {"unit": unit, "origin": self.origin, "ssh": self.target,
                       "version": version, "protocol": protocol, "complete": False}
        self._write_transaction(transaction)
        command = ["sudo", "-n", "systemd-run", "--unit", unit,
                   "--property=Type=oneshot", "--property=RemainAfterExit=yes", "--",
                   "/usr/local/bin/cc-remote", "update", "--role", "relay", "--version", version]
        if allow_protocol_change:
            command.append("--allow-protocol-change")
        print(f"Updating Relay first; independent server transaction: {unit}", flush=True)
        self._ssh(command)
        self._finish(transaction)


def installer_main(argv: list[str] | None = None) -> int:
    """New-bundle preflight, including callers running the old v4.0.1 updater.

    The role installer already holds the installation lock and has built and
    validated this bundle. This check runs before stopping the local Wrapper.
    Older Release installs may not yet have installation.json, so use the
    service identity validated by the installer instead of registering early.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--user", required=True)
    parser.add_argument("--service-label")
    parser.add_argument("--relay-ssh")
    parser.add_argument("--allow-protocol-change", action="store_true")
    args = parser.parse_args(argv)
    try:
        current = args.root / "current"
        previous = current.resolve(strict=True)
        if not current.is_symlink() or previous.parent != (args.root / "releases").resolve():
            raise UpdateError("current must identify an existing immutable Wrapper release")
        old = load_manifest(previous / "release-manifest.json")
        target = load_manifest(args.bundle / "release-manifest.json")
        if (old["role"] != "wrapper" or target["role"] != "wrapper"
                or any(old[key] != target[key] for key in ("os", "arch"))):
            raise UpdateError("upstream preflight requires matching Wrapper installations")
        if old["protocol_version"] != target["protocol_version"] and not args.allow_protocol_change:
            raise UpdateError("protocol changes; coordinate all devices and use --allow-protocol-change")
        metadata = {"schema": 1, "role": "wrapper", "user": args.user}
        if args.service_label:
            metadata["service_label"] = args.service_label
        installation = Installation(args.root, previous, old, metadata)
        RelayUpdate(installation, args.relay_ssh).ensure(
            target["product_version"], target["protocol_version"],
            allow_protocol_change=args.allow_protocol_change,
        )
    except (OSError, ValueError, UpdateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(installer_main())
