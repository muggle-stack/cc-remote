"""Claude CLI selection and child-process environment regressions."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from cc_remote.config import WrapperConfig, validate_wrapper_config
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.child_env import (
    CONTROL_PLANE_SECRET_KEYS,
    sanitized_child_env,
    scrub_parent_control_secrets,
)
from cc_remote.wrapper.codex_handle import _codex_env
from cc_remote.wrapper.sdk import SdkHandle


ROOT = Path(__file__).resolve().parents[1]


def test_codex_child_environment_drops_control_plane_secrets(monkeypatch):
    for key in CONTROL_PLANE_SECRET_KEYS:
        monkeypatch.setenv(key, f"secret-{key}")
    clean = _codex_env("codex")
    assert all(key not in clean for key in CONTROL_PLANE_SECRET_KEYS)
    assert all(key not in sanitized_child_env() for key in CONTROL_PLANE_SECRET_KEYS)


def test_claude_sdk_options_override_inherited_control_secrets(monkeypatch):
    for key in CONTROL_PLANE_SECRET_KEYS:
        monkeypatch.setenv(key, f"secret-{key}")
    options = SdkHandle(WrapperConfig())._options(None, "/tmp")
    assert options.env == {key: "" for key in CONTROL_PLANE_SECRET_KEYS}


def test_scrub_removes_live_environment_mapping(monkeypatch):
    # Do not apply PR_SET_DUMPABLE to the pytest worker itself on Linux; exercise
    # the portable mapping behavior with the platform branch disabled.
    import cc_remote.wrapper.child_env as child_env

    monkeypatch.setattr(child_env.sys, "platform", "test-platform")
    for key in CONTROL_PLANE_SECRET_KEYS:
        monkeypatch.setenv(key, f"secret-{key}")
    scrub_parent_control_secrets()
    assert all(key not in os.environ for key in CONTROL_PLANE_SECRET_KEYS)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() == 0,
    reason="Linux /proc same-uid permission regression (root can bypass it)",
)
def test_linux_model_child_cannot_read_wrapper_initial_environment():
    sentinel = "CONTROL_PLANE_SENTINEL_DO_NOT_PRINT"
    script = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        from cc_remote.wrapper.child_env import scrub_parent_control_secrets

        parent_pid = os.getpid()
        scrub_parent_control_secrets()
        assert "WRAPPER_TOKEN" not in os.environ
        probe = subprocess.run(
            [sys.executable, "-c",
             f"open('/proc/{parent_pid}/environ', 'rb').read()"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        raise SystemExit(0 if probe.returncode != 0 else 9)
        """
    )
    env = dict(os.environ)
    env["WRAPPER_TOKEN"] = sentinel
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.fspath(os.path.dirname(os.path.dirname(__file__))),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert sentinel not in result.stdout + result.stderr


def test_wrapper_systemd_keeps_secret_source_outside_model_namespace():
    unit = (ROOT / "deploy" / "cc-remote-wrapper.service").read_text()
    assert "EnvironmentFile=/etc/cc-remote/wrapper.env" in unit
    assert "EnvironmentFile=/path/to/cc-remote/.env" not in unit
    assert "Environment=PYTHON_DOTENV_DISABLED=1" in unit
    assert "InaccessiblePaths=/etc/cc-remote -/path/to/cc-remote/.env" in unit
    assert "LimitCORE=0" in unit
    assert "NoNewPrivileges=true" in unit


def test_claude_bin_defaults_to_path_and_can_be_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    assert WrapperConfig().claude_bin == ""

    cli = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_BIN", f"  {cli}  ")
    cfg = WrapperConfig()
    assert cfg.claude_bin == str(cli)
    assert SdkHandle(cfg)._options(None).cli_path == str(cli)


def test_claude_bin_rejects_relative_path(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "bin/claude")
    cfg = WrapperConfig()
    with pytest.raises(ValueError, match="absolute path"):
        validate_wrapper_config(cfg)
    with pytest.raises(RuntimeError, match="absolute path"):
        SdkHandle.preflight(cfg.claude_bin)


def test_claude_preflight_uses_path_only_when_no_explicit_binary(monkeypatch):
    seen = []
    monkeypatch.setattr(sdk_module, "SDK_VERSION", "0.2.110")
    monkeypatch.setattr(
        sdk_module.shutil,
        "which",
        lambda name: seen.append(name) or "/bin/claude",
    )

    SdkHandle.preflight()

    assert seen == ["claude"]


def test_claude_preflight_checks_explicit_binary_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk_module, "SDK_VERSION", "0.2.110")
    monkeypatch.setattr(
        sdk_module.shutil,
        "which",
        lambda _name: pytest.fail("explicit CLAUDE_BIN must not fall back to PATH"),
    )
    cli = tmp_path / "claude-custom"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)

    SdkHandle.preflight(str(cli))

    cli.chmod(0o600)
    with pytest.raises(RuntimeError, match="CLAUDE_BIN is not an executable file"):
        SdkHandle.preflight(str(cli))
