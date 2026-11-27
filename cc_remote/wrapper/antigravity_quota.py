"""Read account quota and the subscription label from the local agy CLI."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time

from cc_remote.protocol import (
    MAX_SAFE_WIRE_TIMESTAMP_SECONDS, AntigravityQuota, AntigravityQuotaBucket, AntigravityQuotaGroup,
)
from cc_remote.wrapper.antigravity_client import AntigravityLaunch


_REFRESH_SECONDS = 60
_OUTPUT_LIMIT = 256 * 1024
_MODEL_PREFIX = "Models within this group: "
_BUCKET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")

# agy feeds documented status-line metadata to this transient command. Save
# only the subscription field in the private directory for this read.
_PLAN_CAPTURE = """\
import json
from pathlib import Path
import sys

plan = json.load(sys.stdin).get("plan_tier")
if isinstance(plan, str) and 0 < len(plan) <= 96:
    Path(sys.argv[1]).write_text(plan, encoding="utf-8")
"""


def _label(value: object, limit: int) -> str | None:
    if isinstance(value, str):
        label = value.strip()
        if 0 < len(label) <= limit and label.isprintable():
            return label
    return None


def _reset_time(value: object) -> int | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return None
        epoch = int(moment.timestamp())
        if 0 < epoch <= MAX_SAFE_WIRE_TIMESTAMP_SECONDS:
            return epoch
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _bucket(value: object) -> AntigravityQuotaBucket | None:
    if not isinstance(value, dict):
        return None
    identifier, fraction = value.get("id"), value.get("remaining_fraction")
    window = _label(value.get("window"), 32)
    if (not isinstance(identifier, str) or not _BUCKET_ID.fullmatch(identifier)
            or window is None or type(fraction) not in (int, float) or not 0 <= fraction <= 1):
        return None
    return AntigravityQuotaBucket(
        id=identifier, window=window, name=_label(value.get("name"), 128) or window,
        remaining_fraction=fraction, resets_at=_reset_time(value.get("reset_time")),
    )


def parse_quota(result: object) -> AntigravityQuota:
    if not isinstance(result, dict) or result.get("status") != "SUCCESS" or result.get("num_turns") != 0:
        raise ValueError("Invalid agy quota response")
    command = result.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        raise ValueError("Missing agy usage command result")
    data = command.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ValueError("Missing agy quota groups")
    groups = []
    for row in data["groups"][:16]:
        if not isinstance(row, dict) or not isinstance(row.get("buckets"), list):
            continue
        name = _label(row.get("name"), 128)
        buckets = {}
        for value in row["buckets"][:8]:
            if bucket := _bucket(value):
                buckets.setdefault(bucket.id, bucket)
        if not name or not buckets:
            continue
        description = row.get("description")
        model_labels = (description[len(_MODEL_PREFIX):].split(",")[:64]
                        if isinstance(description, str) and description.startswith(_MODEL_PREFIX) else [])
        groups.append(AntigravityQuotaGroup(
            name=name, buckets=list(buckets.values()),
            models=[label for value in model_labels if (label := _label(value, 128))],
        ))
    return AntigravityQuota(
        groups=groups, plan=_label(data.get("plan_tier"), 96), observed_at=int(time.time()),
    )


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    process.wait()


def _read_plan(launch: AntigravityLaunch, directory: Path) -> str | None:
    if os.name != "posix":
        return None
    import termios

    plan_path = directory / "plan.txt"
    command = shlex.join([sys.executable, "-c", _PLAN_CAPTURE, str(plan_path)])
    settings = json.dumps({
        "statusLine": {"type": "command", "command": command, "enabled": True},
    })
    master, slave = os.openpty()
    process = None
    try:
        with suppress(termios.error):
            termios.tcsetwinsize(slave, (24, 100))
        process = subprocess.Popen(
            [launch.binary, "--settings", settings, "--log-file", str(directory / "plan.log")],
            cwd=directory, env={**launch.environment, "PWD": str(directory), "TERM": "xterm-256color"},
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        deadline = time.monotonic() + 8
        while process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                os.read(master, 65536)
            with suppress(FileNotFoundError):
                if plan := _label(plan_path.read_text(encoding="utf-8"), 96):
                    return plan
        return None
    finally:
        if process is not None:
            _stop(process)
        os.close(slave)
        os.close(master)


def read_native_quota(launch: AntigravityLaunch) -> AntigravityQuota:
    with TemporaryDirectory(prefix="ccr-agy-quota-") as folder, TemporaryFile() as output:
        directory = Path(folder).resolve()
        process = subprocess.Popen(
            [launch.binary, "--print", "/usage", "--output-format", "json",
             "--print-timeout", "10s", "--log-file", str(directory / "usage.log")],
            cwd=directory, env={**launch.environment, "PWD": str(directory)},
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        try:
            if process.wait(timeout=12) != 0:
                raise ValueError("agy account quota unavailable")
            output.seek(0)
            raw = output.read(_OUTPUT_LIMIT + 1)
            if len(raw) > _OUTPUT_LIMIT:
                raise ValueError("agy quota response too large")
            result = parse_quota(json.loads(raw))
        finally:
            _stop(process)
        if result.groups and not result.plan:
            with suppress(OSError, ValueError):
                result.plan = _read_plan(launch, directory)
        return result


class AntigravityQuotaReader:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._launch: AntigravityLaunch | None = None
        self._last: AntigravityQuota | None = None
        self._fresh_until = 0.0
        self._generation = 0
        self._auth_changes = 0

    def invalidate(self):
        self._generation += 1
        self._last = None
        self._fresh_until = 0.0

    @contextmanager
    def account_change(self):
        self._auth_changes += 1
        self.invalidate()
        try:
            yield
        finally:
            self._auth_changes -= 1
            self.invalidate()

    async def refresh(self, launch: AntigravityLaunch | None) -> AntigravityQuota:
        async with self._lock:
            if self._auth_changes or launch is None:
                return AntigravityQuota(error="unavailable")
            if launch != self._launch:
                self.invalidate()
                self._launch = launch
            if self._last is not None and time.monotonic() < self._fresh_until:
                return self._last
            generation = self._generation
            try:
                result = await asyncio.to_thread(read_native_quota, launch)
            except (OSError, subprocess.TimeoutExpired):
                result = (self._last or AntigravityQuota()).model_copy(
                    update={"error": "refresh_failed"},
                )
            except (ValueError, RecursionError):
                # Sign-in may have changed outside cc-remote. Discard the
                # previous reading unless the native account still responds.
                result = AntigravityQuota(error="unavailable")
            if generation != self._generation:
                return AntigravityQuota(error="unavailable")
            self._last = result
            self._fresh_until = time.monotonic() + _REFRESH_SECONDS
            return result
