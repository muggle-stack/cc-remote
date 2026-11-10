#!/usr/bin/env python3
"""Explicitly authorized, OS-owned UI updater for one managed installation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import pwd
import shlex
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_remote.update import (UpdateError, UpdateOutcomeUnknown, host_platform, latest_version, read_installation,
                              release_repository, update, version_tuple)
from cc_remote.update_jobs import (JOBS, POLICY, UpdateJobs, failure_reason,
                                   helper_path, job_id, public_job, write_json)

SUDOERS_DIRECTORY = Path("/etc/sudoers.d")


def require_job_manager(system: str) -> None:
    if system == "linux" and (not Path("/run/systemd/system").is_dir()
                             or not os.access("/usr/bin/systemd-run", os.X_OK)):
        raise UpdateError("UI updates require an available OS job manager (systemd)")


def installation(root: Path):
    if not root.is_absolute() or root.resolve() != root:
        raise UpdateError("installation root must be canonical")
    system, arch = host_platform()
    value = read_installation(root, system, arch)
    if value.role != "wrapper":
        raise UpdateError("UI updates require a device installation")
    account = pwd.getpwnam(value.metadata["user"])
    if system == "linux":
        from deploy.linux_service import trusted
        trusted(root)
        trusted(root / "installation.json")
        trusted(value.release)
        trusted((value.release / ".venv/bin/python").resolve())
        if os.geteuid() != 0:
            raise UpdateError("UI update launch needs configured sudo authorization")
        if os.environ.get("SUDO_UID") not in (None, str(account.pw_uid), "0"):
            raise UpdateError("caller does not own this device")
    elif os.geteuid() != account.pw_uid or root.stat().st_uid != account.pw_uid:
        raise UpdateError("run macOS UI updates as the installation user")
    return value, account


def configure(root: Path, *, enabled: bool, allow_relay: bool = False,
              control_socket: Path | None = None) -> dict:
    value, account = installation(root)
    system = value.manifest["os"]
    store = UpdateJobs(root)
    if not enabled:
        policy = store.policy()
        policy["enabled"] = False
        write_json(root / POLICY, policy)
        return {"enabled": False}
    require_job_manager(system)
    control = control_socket or Path(account.pw_dir) / ".cc-remote/control/control.sock"
    if not control.is_absolute() or len(os.fsencode(control)) > 103:
        raise UpdateError("invalid Wrapper control socket")
    folder = root / JOBS
    folder.mkdir(mode=0o755 if system == "linux" else 0o700, exist_ok=True)
    store.validate_folder()
    if system == "linux":
        from deploy.install_cli import _atomic_file
        from deploy.linux_service import trusted
        helper = helper_path(root)
        helper.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        trusted(helper.parent)
        entry = root / "current/deploy/ui_update_job.py"
        command = [str(root / "current/.venv/bin/python"), "-I", "-B", str(entry), "--root", str(root)]
        content = ('#!/bin/sh\n# cc-remote restricted UI update launcher.\n'
                   'case "$1:$#" in start:3|cancel:2) ;; *) exit 2 ;; esac\n'
                   'exec ' + shlex.join(command) + ' "$@"\n')
        if helper.exists() and not helper.read_text().startswith('#!/bin/sh\n# cc-remote restricted UI update launcher.\n'):
            raise UpdateError("unrelated update launcher already exists")
        _atomic_file(helper, content.encode(), 0o755)
        rule = SUDOERS_DIRECTORY / helper.name
        rule.parent.mkdir(mode=0o755, exist_ok=True)
        trusted(rule.parent)
        if rule.exists() and not rule.read_text().startswith('# cc-remote restricted UI update authorization\n'):
            raise UpdateError("unrelated sudo rule already exists")
        # Only this fixed-root launcher is authorized; it validates operation,
        # request id, stable version, owner and durable policy on every call.
        content = ("# cc-remote restricted UI update authorization\n"
                   f"{account.pw_name} ALL=(root) NOPASSWD: {helper} *\n")
        draft = folder / "sudoers.pending"
        _atomic_file(draft, content.encode(), 0o440)
        try:
            subprocess.run(["/usr/sbin/visudo", "-cf", str(draft)], capture_output=True, check=True, timeout=10)
            _atomic_file(rule, content.encode(), 0o440)
        finally:
            draft.unlink(missing_ok=True)
    policy = {"schema": 1, "root": str(root), "role": "wrapper", "enabled": True,
              "uid": account.pw_uid, "control_socket": str(control),
              "allow_relay_update": allow_relay, "repository": release_repository()}
    write_json(root / POLICY, policy)
    return {"enabled": True, "allow_relay_update": allow_relay}


def launch(root: Path, identity: str, version: str) -> dict:
    value, account = installation(root)
    store = UpdateJobs(root)
    policy = store.policy()
    if not policy["enabled"] or policy["uid"] != account.pw_uid:
        raise UpdateError("UI updates are disabled")
    job_id(identity)
    version_tuple(version)
    with store.lock():
        # An exact replay is an observation, never permission to resubmit a
        # job whose OS acknowledgement was lost.
        try:
            existing = store.state(identity)
        except FileNotFoundError:
            existing = None
        if existing:
            if existing["version"] != version:
                raise UpdateError("update request identity was reused")
            return public_job(existing)
        require_job_manager(value.manifest["os"])
        if version_tuple(version) <= version_tuple(value.manifest["product_version"]):
            raise UpdateError("selected update is not newer")
        if latest_version(policy["repository"]) != version:
            raise UpdateError("selected release changed; check for updates again")
        state, created = store.create(identity, version, value.manifest["product_version"])
        if not created:
            return public_job(state)
        label = f"cc-remote-ui-update-{identity}"
        runner = [str(value.release / ".venv/bin/python"), "-I", "-B",
                  str(value.release / "deploy/ui_update_job.py"), "--root", str(root), "run", identity]
        log = store.folder / f"{identity}.log"
        # Fixed environment: no browser-provided flags, import paths or URLs.
        environment = {"HOME": account.pw_dir, "USER": account.pw_name,
                       "PATH": "/usr/sbin:/usr/bin:/sbin:/bin" if value.manifest["os"] == "linux"
                               else "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
                       "CC_REMOTE_MANAGED_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
        if value.manifest["os"] == "darwin":
            plist = store.folder / f"{identity}.plist"
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            from deploy.install_cli import _atomic_file
            _atomic_file(plist, plistlib.dumps({
                "Label": label, "ProgramArguments": runner, "RunAtLoad": True,
                "WorkingDirectory": account.pw_dir, "EnvironmentVariables": environment,
                "StandardOutPath": str(log), "StandardErrorPath": str(log),
            }), 0o600)
            command = ["/bin/launchctl", "bootstrap", f"gui/{account.pw_uid}", str(plist)]
        else:
            command = ["/usr/bin/systemd-run", "--unit", label, "--no-block", "--collect", "--property=Type=oneshot",
                       f"--property=WorkingDirectory={root}",
                       *[f"--setenv={key}={val}" for key, val in environment.items()], "--", *runner]
        try:
            result = subprocess.run(command, capture_output=True, timeout=20, check=False)
            if result.returncode:
                raise UpdateError("OS update job acknowledgement unavailable")
        except (OSError, subprocess.SubprocessError, UpdateError):
            current = store.state(identity)
            if current["phase"] == "submitting":
                store.save({**current, "phase": "unknown", "reason": "launch_unknown"})
        return public_job(store.state(identity))


def cancel(root: Path, identity: str) -> dict:
    installation(root)
    store = UpdateJobs(root)
    with store.lock():
        state = store.state(identity)
        if state["phase"] in {"submitting", "preparing", "downloading", "validating", "waiting"}:
            store.save({**state, "cancel_requested": True})
        return public_job(store.state(identity))


class Cancelled(UpdateError):
    pass


def reserve(store: UpdateJobs, identity: str) -> None:
    policy = store.policy()
    while True:
        state = store.state(identity)
        if state.get("cancel_requested") or not store.policy()["enabled"]:
            raise Cancelled("update cancelled before activation")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            try:
                connection.connect(policy["control_socket"])
                connection.sendall(json.dumps({"op": "reserve_ui_update", "id": identity}).encode() + b"\n")
                with connection.makefile("rb") as stream:
                    response = json.loads(stream.readline(4097))
            except (OSError, ValueError):
                response = {"status": "unavailable"}
        if response.get("status") == "ready":
            return
        if response.get("status") not in {"busy", "unavailable"}:
            raise UpdateError("Wrapper refused update reservation")
        # A browser disconnect never controls this wait. No model is interrupted.
        if time.time() - state["created_at"] > 24 * 3600:
            raise UpdateError("Wrapper did not become idle for this update")
        time.sleep(2)


def run(root: Path, identity: str) -> None:
    installation(root)
    store = UpdateJobs(root)
    job_id(identity)
    with store.lock(blocking=True):
        state = store.state(identity)
        if (state["phase"] not in {"submitting", "unknown"}
                or state["phase"] == "unknown" and state.get("worker_pid")):
            # Even a duplicate OS invocation cannot start a second installer.
            return
        from cc_remote.wrapper.process_scan import process_identity
        identity_info = process_identity(os.getpid())
        if identity_info is None:
            raise UpdateError("cannot identify update worker")
        store.save({**state, "phase": "preparing", "worker_pid": os.getpid(),
                    "worker_start": identity_info.start_ticks})
    policy = store.policy()
    # Elevation never inherits an unprivileged caller's package source override.
    os.environ["CC_REMOTE_GITHUB_REPOSITORY"] = policy["repository"]
    os.environ.pop("CC_REMOTE_RELEASE_BASE_URL", None)
    os.environ["CC_REMOTE_UI_ALLOW_RELAY_UPDATE"] = "1" if policy["allow_relay_update"] else "0"

    def progress(phase: str) -> None:
        with store.lock(blocking=True):
            current = store.state(identity)
            if (current.get("cancel_requested") or not store.policy()["enabled"]) and current["phase"] in {
                "preparing", "downloading", "validating", "waiting",
            }:
                raise Cancelled("update cancelled before activation")
            store.save({**current, "phase": "waiting" if phase == "ready" else phase})
        if phase == "ready":
            reserve(store, identity)

    try:
        if not policy["enabled"]:
            raise Cancelled("UI updates were disabled")
        update(role="wrapper", target_version=state["version"], check=False,
               allow_relay_update=policy["allow_relay_update"], progress=progress)
        from deploy.release_retention import STATUS
        try:
            from cc_remote.update_jobs import read_json
            retention = read_json(root / STATUS, store.owner, limit=1024 * 1024)
            deferred = retention.get("status") != "complete"
        except (OSError, ValueError):
            deferred = True
        outcome = {"phase": "succeeded", "reason": None, "cleanup_pending": deferred}
    except Cancelled:
        outcome = {"phase": "cancelled", "reason": None}
    except UpdateOutcomeUnknown as exc:
        # The upstream receipt remains authoritative and the next attempt will
        # follow that same transaction. Before local activation there is no
        # local installer to fence: do not strand chat behind a dead worker.
        phase = store.state(identity)["phase"]
        outcome = ({"phase": "failed", "reason": "relay_unconfirmed"}
                   if phase in {"preparing", "downloading", "validating", "updating_relay", "waiting"}
                   else {"phase": "unknown", "reason": "outcome_pending"})
        print(f"UI update unresolved: {exc}", file=sys.stderr, flush=True)
    except Exception as exc:
        reason = failure_reason(exc)
        phase = store.state(identity)["phase"]
        outcome = {"phase": "unknown" if phase in {"installing", "verifying"} else "blocked" if reason in {
            "protocol_change", "service_migration", "relay_permission", "permission_required",
        } else "failed", "reason": reason}
        print(f"UI update stopped: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    with store.lock(blocking=True):
        store.save({**store.state(identity), **outcome})
    retire(root, identity)


def retire(root: Path, identity: str) -> None:
    # Do not leave completed launchd jobs pinning old immutable runtimes forever.
    # The durable receipt is already committed. bootout may terminate this very
    # worker; systemd's --collect performs the equivalent after process exit.
    if host_platform()[0] == "darwin":
        state = UpdateJobs(root).state(identity)
        if state["phase"] in {"succeeded", "failed", "blocked", "cancelled"}:
            try:
                subprocess.run(["/bin/launchctl", "bootout",
                                f"gui/{os.geteuid()}/cc-remote-ui-update-{job_id(identity)}"],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                pass  # The receipt remains authoritative if OS cleanup fails.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start")
    start.add_argument("id")
    start.add_argument("version")
    for action in ("run", "cancel"):
        sub.add_parser(action).add_argument("id")
    args = parser.parse_args()
    try:
        if args.action == "run":
            run(args.root, args.id)
            return 0
        value = launch(args.root, args.id, args.version) if args.action == "start" else cancel(args.root, args.id)
        print(json.dumps({"ok": True, "job": value}))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "reason": failure_reason(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
