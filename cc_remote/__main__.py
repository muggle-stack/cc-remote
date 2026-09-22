"""Release-management CLI: python -m cc_remote (or cc-remote)."""
from __future__ import annotations

import argparse
import sys

from cc_remote import __version__
from cc_remote.update import UpdateError, update


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cc-remote")
    parser.add_argument("--version", action="version", version=f"cc-remote {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("update", help="update a local Release installation")
    command.add_argument("--check", action="store_true", help="check without downloading or restarting")
    command.add_argument("--version", dest="target_version", help="select an exact stable version")
    command.add_argument("--role", choices=("relay", "wrapper"), help="required when both roles are installed")
    command.add_argument(
        "--allow-protocol-change", action="store_true",
        help="activate a protocol change during a coordinated multi-machine upgrade",
    )
    args = parser.parse_args(argv)
    try:
        return update(
            role=args.role, target_version=args.target_version, check=args.check,
            allow_protocol_change=args.allow_protocol_change,
        )
    except (UpdateError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Update interrupted. Inspect the installation before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
