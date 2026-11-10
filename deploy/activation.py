#!/usr/bin/env python3
"""Shared installer admission, durable outcome and interrupted-switch recovery."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_remote import activation
from cc_remote.update_jobs import UpdateJobs, job_id, read_json, failure_reason
from deploy.install_lock import LOCK_FD_ENV, verify_install_lock


def _peer_identity(peer: socket.socket, uid: int):
    from cc_remote.wrapper.process_scan import process_identity, process_owner_uid
    if sys.platform == "linux":
        pid, owner, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if owner != uid:
            raise ValueError("Wrapper control peer has the wrong owner")
    elif sys.platform == "darwin":
        # sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2. The PID comes from the
        # connected socket, never from the JSON response or a stale receipt.
        pid = peer.getsockopt(0, 2)
    else:
        raise ValueError("Wrapper control peer identity is unavailable")
    identity = process_identity(pid)
    if identity is None or process_owner_uid(pid) != uid:
        raise ValueError("Wrapper control peer cannot be identified")
    return identity


def _legacy_wrapper_release(identity, release: str) -> bool:
    from cc_remote.wrapper.process_scan import process_command, process_environment_value
    args = process_command(identity)
    if not args or args[1:] not in {(b"-m", b"cc_remote.wrapper"), (b"-s", b"-m", b"cc_remote.wrapper")}:
        return False
    executable = Path(os.fsdecode(args[0]))
    if sys.platform == "darwin":
        # Framework Python execs its app binary, replacing argv[0]. Its original
        # venv launcher remains in the kernel's process environment snapshot.
        complete, launcher = process_environment_value(identity, "__PYVENV_LAUNCHER__")
        if not complete:
            return False
        if launcher:
            executable = Path(launcher)
    expected = Path(release).resolve(strict=True)
    # Do not resolve the interpreter symlink itself: different release venvs
    # can share one Python runtime. Verify the release containing that venv.
    if (not executable.is_absolute() or executable.name not in {"python", "python3"}
            or executable.parent.name != "bin" or executable.parent.parent.name != ".venv"
            or executable.parent.parent.parent.resolve(strict=True) != expected):
        return False
    if sys.platform == "linux":
        cwd = Path(f"/proc/{identity.pid}/cwd").resolve(strict=True)
    else:
        result = subprocess.run(["/usr/sbin/lsof", "-n", "-P", "-a", "-p", str(identity.pid),
                                 "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=5, check=True)
        paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
        if len(paths) != 1:
            return False
        cwd = Path(paths[0]).resolve(strict=True)
    return cwd == expected


def control(path: Path, request: dict, uid: int, *, expected_release: str | None = None) -> dict:
    import stat
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise ValueError("unsafe Wrapper control socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(5)
        peer.connect(str(path))
        identity = _peer_identity(peer, uid) if expected_release is not None else None
        peer.sendall(json.dumps(request).encode() + b"\n")
        with peer.makefile("rb") as stream:
            result = json.loads(stream.readline(4097))
        if not isinstance(result, dict):
            raise ValueError("invalid Wrapper control response")
        if identity is not None:
            from dataclasses import asdict
            from cc_remote.wrapper.process_scan import process_identity
            if result == {"status": "rejected", "error": "Unknown control request"}:
                # Previous Wrappers do not implement update_status. An exact
                # legacy rejection proves the control loop answered; only the
                # kernel-identified Wrapper from this release may satisfy it.
                if _legacy_wrapper_release(identity, expected_release):
                    result = {"status": "ready", "release": expected_release, "process": asdict(identity)}
            if (process_identity(identity.pid) != identity
                    or result.get("status") == "ready" and result.get("process") != asdict(identity)):
                raise ValueError("Wrapper control process changed during health inspection")
        return result


def health(value: dict, release: str) -> bool:
    try:
        if value.get("role") == "relay":
            from cc_remote.update_relay import relay_release, _get_json
            from deploy.release_manifest import load_manifest
            expected = load_manifest(Path(release) / "release-manifest.json")
            observed = relay_release(value["origin"])
            web = _get_json(value["origin"] + "/cc-remote-build.json")
            expected_web = json.loads((Path(release) / "web/dist/cc-remote-build.json").read_text())
            pid = subprocess.check_output(["systemctl", "show", "cc-remote-relay", "--property=MainPID", "--value"], text=True, timeout=10).strip()
            return (observed["version"] == expected["product_version"] and observed["protocol"] == expected["protocol_version"]
                    and web == expected_web and pid.isdecimal() and int(pid) > 0
                    and Path(f"/proc/{pid}/cwd").resolve(strict=True) == Path(release))
        result = control(Path(value["control_socket"]), {"op": "update_status"}, value["uid"], expected_release=release)
        return result.get("status") == "ready" and result.get("release") == release and bool(result.get("process"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def change(root: Path, identity: str, **fields) -> dict:
    value = activation.receipt(root, identity)
    if value is None:
        raise ValueError("activation receipt missing")
    value.update(fields, updated_at=time.time())
    activation.save(root, value)
    return value


def begin(args) -> str:
    import pwd
    root = args.root
    verify_install_lock(root, int(os.environ[LOCK_FD_ENV]))
    target = args.release.resolve(strict=True)
    if target.parent != root / "releases":
        raise ValueError("activation target escapes releases")
    identity = job_id(os.environ.get("CC_REMOTE_UI_UPDATE_ID") or os.environ.get("CC_REMOTE_ACTIVATION_ID") or uuid.uuid4().hex)
    if activation.receipt(root, identity) is not None:
        raise ValueError("activation already has a receipt; reconcile it before retrying")
    value = {"schema": 1, "id": identity, "root": str(root), "release": str(target),
             "previous": str((root / "current").resolve()) if (root / "current").exists() else None,
             "generation": args.generation or None, "phase": "waiting", "created_at": time.time(),
             "role": args.role,
             "ui_job": os.environ.get("CC_REMOTE_UI_UPDATE_ID"), "snapshot": None}
    if args.role == "wrapper":
        from deploy.work_registry_snapshot import resolve_wrapper_state_dir
        state = resolve_wrapper_state_dir(args.home, env_file=args.env_file, plist=args.plist)
        value.update(control_socket=str(state / "control/control.sock"), uid=pwd.getpwnam(args.user).pw_uid)
    else:
        from urllib.parse import urlsplit
        parsed = urlsplit(args.origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("invalid Relay recovery origin")
        value["origin"] = args.origin
    activation.save(root, value)
    return identity


def prepare(root: Path, identity: str) -> None:
    verify_install_lock(root, int(os.environ[LOCK_FD_ENV]))
    value = activation.receipt(root, identity)
    unavailable_since = None
    legacy = False
    deadline = time.monotonic() + 24 * 3600
    while time.monotonic() < deadline:
        if value.get("ui_job"):
            store = UpdateJobs(root)
            state = store.state(value["ui_job"])
            if state.get("cancel_requested") or not store.policy()["enabled"]:
                raise ValueError("update cancelled before activation")
            with store.lock(blocking=True):
                store.save({**store.state(value["ui_job"]), "phase": "waiting"})
        try:
            response = control(Path(value["control_socket"]), {"op": "reserve_ui_update" if legacy else "reserve_activation", "id": identity}, value["uid"])
        except (OSError, ValueError):
            response = {"status": "unavailable"}
        if response.get("status") == "ready":
            change(root, identity, phase="quiesced")
            return
        if response.get("status") == "rejected" and not legacy:
            # One-time migration for the previous opt-in updater. Reuse its
            # actual busy-work admission, never bypass it or enable a policy.
            from deploy.release_manifest import load_manifest
            store = UpdateJobs(root)
            policy = store.policy()
            if (not policy["enabled"] or policy["control_socket"] != value["control_socket"]
                    or policy["uid"] != value["uid"]):
                raise ValueError("running Wrapper does not support installer admission")
            target = load_manifest(Path(value["release"]) / "release-manifest.json")
            previous = load_manifest(Path(value["previous"]) / "release-manifest.json")
            with store.lock(blocking=True):
                state, _ = store.create(identity, target["product_version"], previous["product_version"])
                store.save({**state, "phase": "waiting", "original_release": value["previous"]})
            value = change(root, identity, ui_job=identity)
            legacy = True
            continue
        if response.get("status") == "rejected":
            raise ValueError("running Wrapper does not support installer admission; complete the compatibility deployment first")
        if response.get("status") != "busy":
            unavailable_since = unavailable_since or time.monotonic()
            if time.monotonic() - unavailable_since >= 15:
                raise ValueError("Wrapper control connection unavailable; no service was stopped")
        else:
            unavailable_since = None
        time.sleep(1)
    raise ValueError("affected operations did not finish; no service was stopped")


def finish(root: Path, identity: str, outcome: str) -> None:
    verify_install_lock(root, int(os.environ[LOCK_FD_ENV]))
    value = change(root, identity, phase=outcome)
    if value.get("ui_job"):
        store = UpdateJobs(root)
        with store.lock(blocking=True):
            state = store.state(value["ui_job"])
            if outcome in activation.TERMINAL_PHASES:
                store.save({**state, "phase": "succeeded" if outcome == "succeeded" else "cancelled" if state.get("cancel_requested") or value.get("reason") == "cancelled" else "failed",
                            "reason": None if outcome == "succeeded" else "rolled_back" if outcome == "rolled_back" else value.get("reason", "update_failed")})


def service(row: dict, action: str) -> None:
    descriptor = int(os.environ[LOCK_FD_ENV])
    if row.get("linux_service"):
        from deploy import linux_service
        profile = linux_service.validate(row["linux_service"])
        (linux_service.stop if action == "stop" else linux_service.start)(profile)
    elif sys.platform == "darwin":
        command = (["launchctl", "bootout", f"gui/{os.getuid()}/{row['service']}"] if action == "stop" else
                   ["launchctl", "bootstrap", f"gui/{os.getuid()}", row["service_file"]])
        result = subprocess.run(command, capture_output=True, timeout=60, pass_fds=(descriptor,))
        if result.returncode and action != "stop":
            raise ValueError("could not restart the restored Wrapper")
        if action == "stop":
            observed = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{row['service']}"],
                                      capture_output=True, text=True, timeout=10)
            if observed.returncode == 0 and re.search(r"\bpid = [1-9][0-9]*", observed.stdout):
                raise ValueError("Wrapper still running; private state was not restored")
    else:
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", row["service"]):
            raise ValueError("invalid Wrapper service")
        if action == "stop" and row.get("role") == "wrapper":
            from deploy.check_codex_lifetime import guarded_stop
            evidence = Path(row["backup"]) / ("recovery-stop-" + uuid.uuid4().hex + ".json")
            guarded_stop(row["service"], row["uid"], evidence)
        else:
            subprocess.run(["systemctl", action, row["service"]], check=True, capture_output=True, timeout=90, pass_fds=(descriptor,))


def recover(root: Path, identity: str) -> str:
    """Called only by an independent, privileged worker holding the install lock."""
    from deploy import release_retention
    verify_install_lock(root, int(os.environ[LOCK_FD_ENV]))
    value = activation.receipt(root, identity)
    if not value:
        raise ValueError("activation recovery receipt is incomplete")
    current = str((root / "current").resolve(strict=True))
    if not value.get("generation"):
        candidates = [row for row in release_retention.read_ledger(root)["generations"]
                      if row["phase"] == "prepared" and row["release"] == value["release"]
                      and row["previous"] == (value["previous"] or "")]
        if len(candidates) == 1:
            value = change(root, identity, generation=candidates[0]["id"])
    if not value.get("generation"):
        if value["phase"] == "succeeded" and current == value["release"] and health(value, current):
            return "succeeded"
        if current == value["previous"] == value["release"] and health(value, current):
            # Reinstalling the same immutable release deliberately has no
            # retention generation. A live, identity-verified original service
            # can settle the interrupted attempt without restoring private
            # state or claiming that every reinstall step completed.
            finish(root, identity, "unchanged")
            return "failed"
        if value["phase"] in {"waiting", "quiesced", "unchanged"} and current == value["previous"] and health(value, current):
            finish(root, identity, "unchanged")
            return "failed"
        raise ValueError("activation recovery generation is unavailable")
    _, row = release_retention.generation(root, value["generation"])
    if row["phase"] == "committed" or value["phase"] == "succeeded":
        if current == value["release"] and health(value, current):
            if row["phase"] == "prepared":
                release_retention.commit(root, row["id"], snapshot=value.get("snapshot"))
            finish(root, identity, "succeeded")
            return "succeeded"
        raise ValueError("committed installation needs service recovery; private state must not be rolled back")
    if not value.get("previous") or row["previous"] != value["previous"] or row["release"] != value["release"]:
        raise ValueError("activation recovery identities do not match")
    if current not in {value["previous"], value["release"]}:
        raise ValueError("installation changed outside the original transaction")
    if (value["phase"] in {"waiting", "quiesced", "unchanged", "rolled_back", "restored"} or row["phase"] == "aborted") and current == value["previous"] and health(value, current):
        if row["phase"] == "prepared":
            release_retention.abort(root, row["id"])
        finish(root, identity, "rolled_back" if row["phase"] == "aborted" or value["phase"] in {"rolled_back", "restored"} else "unchanged")
        return "failed"
    if row["phase"] != "prepared":
        raise ValueError("original rollback already finalized but service health is unconfirmed")
    change(root, identity, phase="restoring")
    service({**row, **({"uid": value["uid"]} if value.get("role") == "wrapper" else {})}, "stop")
    if value.get("snapshot"):
        from deploy.work_registry_snapshot import restore_snapshot
        snapshot = Path(value["snapshot"])
        if snapshot.parent != root / "rollback-data":
            raise ValueError("snapshot escapes managed rollback data")
        restore_snapshot(snapshot)
    backup = Path(row["backup"])
    files = read_json(backup / "files.json", root.stat().st_uid, limit=1024 * 1024)
    # Configuration restoration follows exactly the installer-owned inventory.
    for entry in files["files"]:
        destination = Path(entry["destination"])
        if destination.is_symlink():
            raise ValueError("refusing to restore configuration through a symlink")
        if entry["present"]:
            source = backup / entry["file"]
            if source.parent != backup:
                raise ValueError("invalid configuration backup path")
            from deploy.install_cli import _atomic_file
            if row.get("linux_service") and str(destination) == row["service_file"]:
                from deploy.linux_service import restore_program
                restore_program(row["linux_service"], source)
            else:
                _atomic_file(destination, source.read_bytes(), entry["mode"])
                os.chown(destination, entry["uid"], entry["gid"])
        else:
            destination.unlink(missing_ok=True)
    staged = root / (".activation-restore-" + identity)
    if staged.exists() or staged.is_symlink():
        staged.unlink()
    staged.symlink_to(value["previous"])
    os.replace(staged, root / "current")
    change(root, identity, phase="restored")
    if sys.platform != "darwin" and not row.get("linux_service"):
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30, pass_fds=(int(os.environ[LOCK_FD_ENV]),))
    if value.get("role") == "relay":
        service({"service": "caddy"}, "restart")
    service(row, "start")
    for _ in range(45):
        if health(value, value["previous"]):
            release_retention.abort(root, row["id"])
            finish(root, identity, "rolled_back")
            return "failed"
        time.sleep(1)
    raise ValueError("restored installation has not passed its health check")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("action", choices=("begin", "prepare", "phase", "finish", "recover"))
    parser.add_argument("--id")
    parser.add_argument("--value")
    parser.add_argument("--snapshot")
    parser.add_argument("--release", type=Path)
    parser.add_argument("--generation")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--user")
    parser.add_argument("--plist", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--role", choices=("wrapper", "relay"), default="wrapper")
    parser.add_argument("--origin")
    args = parser.parse_args()
    if args.action == "recover" and LOCK_FD_ENV not in os.environ:
        from deploy.install_lock import acquire_install_lock
        descriptor = acquire_install_lock(args.root)
        os.environ[LOCK_FD_ENV] = str(descriptor)
    verify_install_lock(args.root, int(os.environ[LOCK_FD_ENV]))
    try:
        if args.action == "begin":
            print(begin(args))
        elif args.action == "prepare":
            prepare(args.root, job_id(args.id))
        elif args.action == "recover":
            print(recover(args.root, job_id(args.id)))
        elif args.action == "finish":
            if args.value not in activation.TERMINAL_PHASES | {"recovery_required"}:
                raise ValueError("invalid activation outcome")
            finish(args.root, job_id(args.id), args.value)
        else:
            if args.value not in activation.ACTIVE_PHASES | {"waiting"}:
                raise ValueError("invalid activation phase")
            fields = {"phase": args.value}
            if args.snapshot:
                fields["snapshot"] = args.snapshot
            if args.generation:
                fields["generation"] = job_id(args.generation)
            value = activation.receipt(args.root, job_id(args.id))
            if value.get("ui_job"):
                store = UpdateJobs(args.root)
                with store.lock(blocking=True):
                    state = store.state(value["ui_job"])
                    if args.value == "stopping" and (state.get("cancel_requested") or not store.policy()["enabled"]):
                        raise ValueError("update cancelled before activation")
                    change(args.root, args.id, **fields)
                    if args.value in {"stopping", "verifying"}:
                        store.save({**state, "phase": "verifying" if args.value == "verifying" else "installing"})
            else:
                change(args.root, args.id, **fields)
    except (OSError, ValueError) as exc:
        if args.id and activation.receipt(args.root, job_id(args.id)):
            change(args.root, args.id, reason=failure_reason(exc))
        raise



if __name__ == "__main__":
    main()
