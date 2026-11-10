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
                   'case "$1:$#" in start:3|cancel:2|reconcile:2|observe:2) ;; *) exit 2 ;; esac\n'
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
        from deploy.ui_update_broker import install
        install(root, account)
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
    # Receipts are atomically replaced. Exact replays can be answered without
    # contending with a worker's progress write or waiting on network checks.
    try:
        existing = store.state(identity)
    except FileNotFoundError:
        existing = None
    if existing:
        if existing["version"] != version:
            raise UpdateError("update request identity was reused")
        return public_job(existing)
    with store.lock(blocking=True):
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
        store.save({**state, "original_release": str(value.release)})
        spawn_worker(root, value, account, store, identity)
        return public_job(store.state(identity))


def spawn_worker(root: Path, value, account, store: UpdateJobs, identity: str, generation: int = 0) -> None:
    label = f"cc-remote-ui-update-{identity}" + (f"-recovery-{generation}" if generation else "")
    runner = [str(value.release / ".venv/bin/python"), "-I", "-B",
              str(value.release / "deploy/ui_update_job.py"), "--root", str(root), "recover" if generation else "run", identity]
    if generation:
        runner.append(str(generation))
    log = store.folder / f"{identity}-recovery-{generation}.log" if generation else store.folder / f"{identity}.log"
    # Fixed environment: no browser-provided flags, import paths or URLs.
    # Linux installers run as root. Keep their caches and curl configuration
    # in the administrator's home; SSH explicitly drops to the original device
    # account in update_relay.py and native service ownership is unchanged.
    runner_account = pwd.getpwuid(0) if value.manifest["os"] == "linux" else account
    environment = {"HOME": runner_account.pw_dir, "USER": runner_account.pw_name,
                   "PATH": "/usr/sbin:/usr/bin:/sbin:/bin" if value.manifest["os"] == "linux"
                           else "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
                   "CC_REMOTE_MANAGED_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    if value.manifest["os"] == "darwin":
        plist = store.folder / (f"{label}.plist" if generation else f"{identity}.plist")
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        from deploy.install_cli import _atomic_file
        _atomic_file(plist, plistlib.dumps({
            "Label": label, "ProgramArguments": runner, "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "AbandonProcessGroup": True,
            "ThrottleInterval": 5,
            "WorkingDirectory": account.pw_dir, "EnvironmentVariables": environment,
            "StandardOutPath": str(log), "StandardErrorPath": str(log),
        }), 0o600)
        command = ["/bin/launchctl", "bootstrap", f"gui/{account.pw_uid}", str(plist)]
    else:
        command = ["/usr/bin/systemd-run", "--unit", label, "--no-block", "--collect", "--property=Type=oneshot",
                   "--property=Restart=on-failure", "--property=RestartSec=5", "--property=KillMode=process",
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


def cancel(root: Path, identity: str) -> dict:
    installation(root)
    store = UpdateJobs(root)
    with store.lock(blocking=True):
        state = store.state(identity)
        if state["phase"] in {"submitting", "preparing", "downloading", "validating", "waiting"}:
            if not state.get("worker_pid") and not state.get("attempt_started"):
                # Fence a delayed OS launch under the same lock its worker must
                # acquire before accepting installation work.
                store.save({**state, "phase": "cancelled", "reason": None, "cancel_requested": True})
            else:
                store.save({**state, "cancel_requested": True})
        return public_job(store.state(identity))


def worker_alive(state: dict) -> bool:
    from cc_remote.wrapper.process_scan import process_identity
    pid = state.get("worker_pid")
    if type(pid) is not int or pid < 1:
        return False
    identity = process_identity(pid)
    if identity is not None:
        return identity.start_ticks == state.get("worker_start")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reconcile(root: Path, identity: str, *, explicit: bool = False) -> dict:
    """Fence a departed worker, then observe or recover its original activation."""
    from cc_remote import activation
    from deploy.activation import health
    value, account = installation(root)
    store = UpdateJobs(root)
    with store.lock(blocking=True):
        try:
            state = store.state(identity)
        except FileNotFoundError:
            # A terminal-originated switch has the same durable activation
            # identity. Expose its recovery through the existing status channel.
            from deploy.release_manifest import load_manifest
            record = activation.receipt(root, identity)
            if not record or not record.get("previous"):
                raise UpdateError("original activation record is unavailable")
            target = load_manifest(Path(record["release"]) / "release-manifest.json")
            previous = load_manifest(Path(record["previous"]) / "release-manifest.json")
            state, _ = store.create(identity, target["product_version"], previous["product_version"])
            state.update(phase="verifying", original_release=record["previous"])
            store.save(state)
        if state["phase"] in {"succeeded", "failed", "blocked", "cancelled"} or worker_alive(state):
            return public_job(state)
        if (state["phase"] in {"submitting", "unknown"} and not state.get("attempt_started")
                and not state.get("worker_pid") and time.time() - state.get("created_at", 0) < 30):
            return public_job(state)  # Give an accepted OS launch time to enter its worker.
        if activation.install_locked(root):
            return public_job(state)
        record = activation.receipt(root, identity)
        policy = store.policy()
        probe = record or {"control_socket": policy["control_socket"], "uid": policy["uid"]}
        current = str(value.release)
        if health(probe, current):
            # A later CLI activation may already have moved current. A durable
            # terminal receipt describes this request's historical outcome;
            # never roll the healthy newer installation back to settle its UI.
            if (record and record["phase"] == "succeeded"
                    or not record and value.manifest["product_version"] == state["version"]):
                store.save({**state, "phase": "succeeded", "reason": None})
                return public_job(store.state(identity))
            if (record and record["phase"] in {"unchanged", "rolled_back"}
                    or not record and current == state.get("original_release")):
                store.save({**state, "phase": "failed", "reason": "rolled_back" if record and record["phase"] == "rolled_back" else "worker_exited"})
                return public_job(store.state(identity))
        generation = state.get("recovery_generation", 0)
        attempts = 0 if explicit else state.get("recovery_attempts", 0)
        if attempts >= 3 or time.time() - state.get("recovery_started_at", 0) < 30:
            return public_job(state)
        generation += 1
        store.save({**state, "phase": "verifying", "reason": "recovering", "recovery_generation": generation,
                    "recovery_started_at": time.time(), "recovery_attempts": attempts + 1,
                    "worker_pid": None, "worker_start": None})
        spawn_worker(root, value, account, store, identity, generation)
        return public_job(store.state(identity))


def recover(root: Path, identity: str, generation: int) -> None:
    from cc_remote import activation
    from cc_remote.wrapper.process_scan import process_identity
    from deploy import activation as installer
    from deploy.install_lock import acquire_install_lock, LOCK_FD_ENV
    installation(root)
    store = UpdateJobs(root)
    with store.lock(blocking=True):
        state = store.state(identity)
        if state.get("recovery_generation") != generation or state["phase"] in {"succeeded", "failed", "cancelled", "blocked"}:
            return
        process = process_identity(os.getpid())
        if process is None:
            raise UpdateError("cannot identify recovery worker")
        store.save({**state, "worker_pid": process.pid, "worker_start": process.start_ticks})
    descriptor = None
    previous_descriptor = os.environ.get(LOCK_FD_ENV)
    try:
        descriptor = acquire_install_lock(root)
        os.environ[LOCK_FD_ENV] = str(descriptor)
        if activation.receipt(root, identity) is None:
            raise UpdateError("original activation record is unavailable")
        outcome = installer.recover(root, identity)
        result = {"phase": outcome, "reason": None if outcome == "succeeded" else "rolled_back"}
    except Exception as exc:
        print(f"Update recovery deferred: {exc}", file=sys.stderr, flush=True)
        result = {"phase": "unknown", "reason": "recovery_required"}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if previous_descriptor is None:
            os.environ.pop(LOCK_FD_ENV, None)
        else:
            os.environ[LOCK_FD_ENV] = previous_descriptor
    with store.lock(blocking=True):
        state = store.state(identity)
        if state.get("recovery_generation") == generation:
            store.save({**state, **result})
    retire(root, identity, generation)


class Cancelled(UpdateError):
    pass


def resume_original(root: Path, identity: str) -> None:
    """An OS-restarted controller follows its installer; it never resubmits it."""
    from cc_remote import activation
    store = UpdateJobs(root)
    while activation.install_locked(root):
        if store.state(identity)["phase"] in {"succeeded", "failed", "blocked", "cancelled"}:
            retire(root, identity)
            return
        time.sleep(2)
    # Mark this observer departed before reconciliation, which checks exact
    # process identity and serializes recovery under the original request id.
    with store.lock(blocking=True):
        state = store.state(identity)
        if state["phase"] in {"succeeded", "failed", "blocked", "cancelled"}:
            retire(root, identity)
            return
        store.save({**state, "worker_pid": None, "worker_start": None})
    reconcile(root, identity)


def run(root: Path, identity: str) -> None:
    installation(root)
    store = UpdateJobs(root)
    job_id(identity)
    with store.lock(blocking=True):
        state = store.state(identity)
        if state["phase"] in {"succeeded", "failed", "blocked", "cancelled"}:
            retire(root, identity)
            return
        if worker_alive(state):
            return
        resumed = bool(state.get("attempt_started")) or state["phase"] not in {"submitting", "unknown"} or bool(state.get("worker_pid"))
        from cc_remote.wrapper.process_scan import process_identity
        identity_info = process_identity(os.getpid())
        if identity_info is None:
            raise UpdateError("cannot identify update worker")
        store.save({**state, "phase": state["phase"] if resumed else "preparing", "attempt_started": True, "worker_pid": os.getpid(),
                    "worker_start": identity_info.start_ticks})
    if resumed:
        resume_original(root, identity)
        return
    policy = store.policy()
    # Elevation never inherits an unprivileged caller's package source override.
    os.environ["CC_REMOTE_GITHUB_REPOSITORY"] = policy["repository"]
    os.environ.pop("CC_REMOTE_RELEASE_BASE_URL", None)
    os.environ["CC_REMOTE_UI_ALLOW_RELAY_UPDATE"] = "1" if policy["allow_relay_update"] else "0"
    os.environ["CC_REMOTE_UI_UPDATE_ID"] = identity

    def progress(phase: str) -> None:
        with store.lock(blocking=True):
            current = store.state(identity)
            if current["phase"] in {"succeeded", "failed", "blocked", "cancelled"}:
                return  # The shared installer already committed its outcome.
            if (current.get("cancel_requested") or not store.policy()["enabled"]) and current["phase"] in {
                "preparing", "downloading", "validating", "waiting",
            }:
                raise Cancelled("update cancelled before activation")
            store.save({**current, "phase": "validating" if phase == "ready" else phase})

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
        from cc_remote.activation import receipt, TERMINAL_PHASES
        activation = receipt(root, identity)
        settled = activation and activation["phase"] in TERMINAL_PHASES
        outcome = {"phase": "unknown" if phase in {"installing", "verifying"} and not settled else "blocked" if reason in {
            "protocol_change", "service_migration", "relay_permission", "permission_required", "ssh_auth", "ssh_host_key", "tls_certificate",
        } else "failed", "reason": reason}
        if settled:
            outcome = {"phase": "succeeded" if activation["phase"] == "succeeded" else "failed",
                       "reason": None if activation["phase"] == "succeeded" else "rolled_back" if activation["phase"] == "rolled_back" else activation.get("reason", reason)}
        if store.state(identity).get("cancel_requested") and settled and activation["phase"] == "unchanged":
            outcome = {"phase": "cancelled", "reason": None}
        print(f"UI update stopped: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    with store.lock(blocking=True):
        store.save({**store.state(identity), **outcome})
    if outcome["phase"] == "unknown":
        # The Wrapper can be stopped here. Recovery cannot depend on its next
        # browser poll or on a local-control socket that no longer exists.
        resume_original(root, identity)
    retire(root, identity)


def retire(root: Path, identity: str, generation: int = 0) -> None:
    # Do not leave completed launchd jobs pinning old immutable runtimes forever.
    # The durable receipt is already committed. bootout may terminate this very
    # worker; systemd's --collect performs the equivalent after process exit.
    if host_platform()[0] == "darwin":
        state = UpdateJobs(root).state(identity)
        if state["phase"] in {"succeeded", "failed", "blocked", "cancelled"}:
            try:
                subprocess.run(["/bin/launchctl", "bootout",
                                f"gui/{os.geteuid()}/cc-remote-ui-update-{job_id(identity)}" + (f"-recovery-{generation}" if generation else "")],
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
    for action in ("run", "cancel", "reconcile", "observe"):
        sub.add_parser(action).add_argument("id")
    recovery = sub.add_parser("recover")
    recovery.add_argument("id")
    recovery.add_argument("generation", type=int)
    args = parser.parse_args()
    try:
        if args.action == "run":
            run(args.root, args.id)
            return 0
        if args.action == "recover":
            recover(args.root, args.id, args.generation)
            return 0
        value = (launch(args.root, args.id, args.version) if args.action == "start" else
                 reconcile(args.root, args.id, explicit=args.action == "reconcile") if args.action in {"reconcile", "observe"} else cancel(args.root, args.id))
        print(json.dumps({"ok": True, "job": value}))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "reason": failure_reason(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
