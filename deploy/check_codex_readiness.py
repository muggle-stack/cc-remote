"""Print this activation's Codex result without running an account CLI as root."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_remote.wrapper.codex_readiness import REPORT_NAME
from cc_remote.wrapper.codex_daemon import socket_identity
from cc_remote.wrapper.process_scan import ProcessIdentity, process_identity, process_owner_uid
from deploy.work_registry_snapshot import resolve_wrapper_state_dir

_REASONS = {
    "daemon_unavailable": "共享服务尚不可用，请检查当前账号的 Codex 安装。",
    "account_socket_mismatch": "账号与连接地址不一致，请检查 CODEX_HOME 和账号配置。",
    "daily_cli_missing": "服务环境找不到 codex，请安装 CLI 或检查服务的 PATH。",
    "daily_cli_mismatch": "终端 CLI 与 cc-remote 没有找到同一个服务，请检查 Codex 路径和账号。",
    "version_mismatch": "Codex CLI 与运行中的服务版本不一致；当前任务保留，结束后再更新或重开 Codex。",
    "daemon_changed": "检查期间 Codex 服务发生变化，本次未确认连接。",
    "connection_failed": "连接检查未通过；现有任务保留，请检查 Wrapper 日志。",
}


def socket_still_ready(row: dict, owner_uid: int) -> bool:
    try:
        path = row["socket"]
        if not isinstance(path, str) or not os.path.isabs(path):
            return False
        if list(socket_identity(path, owner_uid=owner_uid)) != row["socket_identity"]:
            return False
        # No account binary, authentication or model API is invoked as root.
        # Check only that the exact recently verified listener still accepts.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.2)
            connection.connect(path)
        return True
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return False


def read_receipt(path: Path, release: Path, after: float) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise ValueError("invalid Codex readiness receipt")
        raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("oversized Codex readiness receipt")
    try:
        report = json.loads(raw)
        identity = ProcessIdentity(**report["wrapper"])
        rows = report["profiles"]
        valid = (
            report["schema"] == 1 and report["source"] == str(release.resolve())
            and isinstance(report["created_at"], (int, float))
            and after <= report["created_at"] <= time.time() + 5
            and isinstance(rows, list) and 0 < len(rows) <= 32
            and all(isinstance(row, dict) and isinstance(row.get("profile"), str)
                    and row.get("status") in {"ready", "disabled", "unavailable"} for row in rows)
            and process_identity(identity.pid) == identity
            and process_owner_uid(identity.pid) == info.st_uid
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not valid:
        return None
    for row in rows:
        if row["status"] == "ready" and not socket_still_ready(row, info.st_uid):
            row.update(status="unavailable", reason="daemon_changed")
    return report


def describe(report: dict) -> bool:
    complete = True
    for row in report["profiles"]:
        # Values come from private configuration but must not inject terminal controls.
        profile = json.dumps(row["profile"], ensure_ascii=False)
        if row["status"] == "ready":
            print(f"Codex {profile}: 共享连接已就绪（CLI 与 cc-remote 的连接检查通过）。")
        elif row["status"] == "disabled":
            print(f"Codex {profile}: 保留已有的关闭设置，未启用共享连接。")
        else:
            complete = False
            print(f"Codex {profile}: {_REASONS.get(row.get('reason'), _REASONS['connection_failed'])}")
    if any(row["status"] == "ready" for row in report["profiles"]):
        print("新终端请使用对应账号的 codex / codex resume。安装前已打开的独立会话请等任务结束后重开。")
        print("已验证本地连接，未发送模型消息；现有终端、shell 别名及额外启动参数的实际连接仍需确认。")
    return complete


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--after", required=True, type=float)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--plist", type=Path)
    parser.add_argument("--wait", type=float, default=45)
    args = parser.parse_args(argv)
    try:
        state = resolve_wrapper_state_dir(args.home, env_file=args.env_file, plist=args.plist)
        deadline = time.monotonic() + max(0, min(args.wait, 45))
        while True:
            report = read_receipt(state / REPORT_NAME, args.release, args.after)
            if report is not None:
                return 0 if describe(report) else 1
            if time.monotonic() >= deadline:
                print("Codex: 未收到本次启动的连接检查结果，请检查 Wrapper 日志；不能据此确认已共享。")
                return 1
            time.sleep(1)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Codex: 无法读取连接检查结果（{type(exc).__name__}）；请检查 Wrapper 日志。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
