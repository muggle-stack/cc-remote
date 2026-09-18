"""Version policy and effective Claude CLI discovery.

The Agent SDK bundles a Claude Code executable, but the wrapper explicitly
passes the user's daily ``~/.local/bin/claude`` (or ``CLAUDE_BIN`` override).
Preflight inspects that exact executable before a session can start.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess

import claude_agent_sdk


VERIFIED_SDK_VERSION = "0.2.151"
MINIMUM_CLAUDE_CLI_VERSION = "2.1.263"
_CLI_VERSION_TIMEOUT = 3.0
_VERSION_RE = re.compile(
    r"(?<!\d)(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?)"
)
_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?$"
)


@dataclass(frozen=True)
class ClaudeRuntime:
    sdk_version: str
    cli_path: str
    cli_version: str
    cli_source: str


class UnsupportedClaudeCliVersion(RuntimeError):
    """The selected daily CLI predates cc-remote's verified native contract."""

    def __init__(self, version: str, minimum: str) -> None:
        self.version = version
        self.minimum = minimum
        super().__init__(
            f"Claude CLI {version!r} is older than required {minimum}; "
            "run `claude update` and restart cc-remote"
        )


def validate_sdk_version(version: str | None = None) -> str:
    """Require the exact SDK whose private stream contract was verified."""
    actual = version if version is not None else claude_agent_sdk.__version__
    if actual != VERIFIED_SDK_VERSION:
        raise RuntimeError(
            f"claude-agent-sdk {actual!r} is not the verified "
            f"{VERIFIED_SDK_VERSION}; install requirements.lock or re-run the "
            "Claude interrupt/drain compatibility suite before upgrading"
        )
    return actual


def _cli_version_parts(version: str) -> tuple[tuple[int, int, int], bool]:
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        raise RuntimeError(f"invalid Claude CLI version: {version!r}")
    release = tuple(int(match.group(index)) for index in range(1, 4))
    return release, match.group(4) is not None


def validate_cli_version(version: str) -> str:
    """Require the CLI release that owns the native controls we rely on."""
    actual_release, actual_is_prerelease = _cli_version_parts(version)
    minimum_release, _ = _cli_version_parts(MINIMUM_CLAUDE_CLI_VERSION)
    if (
        actual_release < minimum_release
        or (actual_release == minimum_release and actual_is_prerelease)
    ):
        raise UnsupportedClaudeCliVersion(
            version, MINIMUM_CLAUDE_CLI_VERSION,
        )
    return version


def bundled_claude_path() -> str | None:
    """Return the executable bundled by the installed Agent SDK, if present."""
    package = Path(claude_agent_sdk.__file__).resolve().parent
    name = "claude.exe" if os.name == "nt" else "claude"
    candidate = package / "_bundled" / name
    if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
        return str(candidate)
    return None


def _external_candidates() -> list[str]:
    """Mirror the public SDK's external fallback order after its bundle."""
    home = Path.home()
    candidates: list[str] = []
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    candidates.extend(str(path) for path in (
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    ))
    return candidates


def resolve_claude_cli(configured: str = "") -> tuple[str, str]:
    """Resolve the executable the SDK will actually spawn and its source."""
    value = configured.strip()
    if value:
        path = os.path.expanduser(value)
        if not os.path.isabs(path):
            raise RuntimeError("CLAUDE_BIN must be an absolute path")
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise RuntimeError(f"CLAUDE_BIN is not an executable file: {path}")
        return path, "configured"

    bundled = bundled_claude_path()
    if bundled:
        return bundled, "bundled"

    seen: set[str] = set()
    for candidate in _external_candidates():
        path = os.path.abspath(os.path.expanduser(candidate))
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path, "external"
    raise RuntimeError(
        "Claude CLI not found in the Agent SDK bundle, PATH, or standard locations"
    )


def probe_claude_cli_version(path: str) -> str:
    """Read a bounded semantic version from one resolved Claude executable."""
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=_CLI_VERSION_TIMEOUT, check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"unable to execute Claude CLI: {path}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"Claude CLI version probe exited with status "
            f"{result.returncode}: {path}"
        )
    match = _VERSION_RE.search((result.stdout or "") + (result.stderr or ""))
    if match is None:
        raise RuntimeError(f"unable to determine Claude CLI version: {path}")
    return match.group(1)


def inspect_claude_runtime(configured: str = "") -> ClaudeRuntime:
    """Validate the SDK and report the exact CLI runtime it will use."""
    sdk_version = validate_sdk_version()
    cli_path, cli_source = resolve_claude_cli(configured)
    cli_version = validate_cli_version(probe_claude_cli_version(cli_path))
    return ClaudeRuntime(
        sdk_version=sdk_version,
        cli_path=cli_path,
        cli_version=cli_version,
        cli_source=cli_source,
    )
