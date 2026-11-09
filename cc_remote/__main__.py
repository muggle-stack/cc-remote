"""Release-management CLI: python -m cc_remote (or cc-remote)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from cc_remote import __version__
from cc_remote.update import UpdateError, update


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cc-remote")
    parser.add_argument("--version", action="version", version=f"cc-remote {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    tasks = commands.add_parser("tasks", help="generic asynchronous tasks MCP (runs as the current user)")
    tasks.add_argument("task_args", nargs=argparse.REMAINDER)
    messages = commands.add_parser("messages", help="local cross-session messaging MCP")
    messages.add_argument("message_args", nargs=argparse.REMAINDER)
    ui_updates = commands.add_parser("ui-updates", help="authorize UI updates for this managed device")
    ui_updates.add_argument("action", choices=("enable", "disable"))
    ui_updates.add_argument("--allow-relay-update", action="store_true",
                            help="also allow this device's existing administrator SSH route to upgrade its Relay")
    ui_updates.add_argument("--control-socket", type=Path,
                            help="Wrapper's existing private control socket, for a custom state directory")
    command = commands.add_parser("update", help="update a local Release installation")
    command.add_argument("--check", action="store_true", help="check without downloading or restarting")
    command.add_argument("--version", dest="target_version", help="select an exact stable version")
    command.add_argument("--role", choices=("relay", "wrapper"), help="required when both roles are installed")
    command.add_argument("--relay-ssh", help="SSH alias or user@host for Relay upgrades (saved after verification)")
    command.add_argument(
        "--allow-protocol-change", action="store_true",
        help="activate a protocol change during a coordinated multi-machine upgrade",
    )
    args = parser.parse_args(argv)
    if args.command == "ui-updates":
        from cc_remote.update import host_platform, select_installation
        from deploy.ui_update_job import configure
        try:
            current = select_installation("wrapper", *host_platform())
            result = configure(current.root, enabled=args.action == "enable",
                               allow_relay=args.allow_relay_update, control_socket=args.control_socket)
            print("UI updates enabled." if result["enabled"] else "UI updates disabled.")
            return 0
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    if args.command == "tasks":
        from cc_remote.async_tasks import main as task_main
        return task_main(args.task_args)
    if args.command == "messages":
        from cc_remote.session_messages import main as message_main
        return message_main(args.message_args)
    try:
        return update(
            role=args.role, target_version=args.target_version, check=args.check,
            allow_protocol_change=args.allow_protocol_change,
            relay_ssh=args.relay_ssh,
        )
    except (UpdateError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Update interrupted. Inspect the installation before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
