"""Keep relay control-plane credentials out of model/tool subprocesses."""
from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Mapping


CONTROL_PLANE_SECRET_KEYS = (
    "WRAPPER_TOKEN",
    "LOGIN_PASSWORD",
    "SESSION_SECRET",
)


def sanitized_child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for key in CONTROL_PLANE_SECRET_KEYS:
        env.pop(key, None)
    return env


def child_env_tombstones() -> dict[str, str]:
    """Claude SDK merges options.env over os.environ, so empty values scrub it."""
    return {key: "" for key in CONTROL_PLANE_SECRET_KEYS}


def scrub_parent_control_secrets() -> None:
    """Hide captured credentials from model/tool descendants.

    On Linux, deleting a key from ``os.environ`` does not rewrite the initial
    environment bytes exposed through ``/proc/<pid>/environ``.  Claude tools run
    as the same Unix user as this process, so make the wrapper non-dumpable
    before removing the live mapping.  This also blocks ptrace/process-memory
    reads of the in-memory transport token.  Failing to apply the protection is
    fatal: running a bypass-permissions model with a readable bearer token would
    be a privilege-boundary bypass.
    """
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        # linux/prctl.h: PR_SET_DUMPABLE = 4.
        if prctl(4, 0, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(
                error_number,
                "failed to disable process dumpability for wrapper secrets",
            )
    for key in CONTROL_PLANE_SECRET_KEYS:
        os.environ.pop(key, None)
