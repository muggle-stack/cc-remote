"""Claude CLI selection and child-process environment regressions."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

from cc_remote.config import WrapperConfig, validate_wrapper_config
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.child_env import (
    CONTROL_PLANE_SECRET_KEYS,
    sanitized_child_env,
    scrub_parent_control_secrets,
)
from cc_remote.wrapper.codex_handle import _codex_env
from cc_remote.wrapper.sdk import CLAUDE_WORK_TOOLS, SdkHandle
from cc_remote.wrapper.work_prompt import WORK_SYSTEM_PROMPT


ROOT = Path(__file__).resolve().parents[1]


def test_codex_child_environment_drops_control_plane_secrets(monkeypatch):
    for key in CONTROL_PLANE_SECRET_KEYS:
        monkeypatch.setenv(key, f"secret-{key}")
    clean = _codex_env("codex")
    assert all(key not in clean for key in CONTROL_PLANE_SECRET_KEYS)
    assert all(key not in sanitized_child_env() for key in CONTROL_PLANE_SECRET_KEYS)


def test_codex_proxy_is_scoped_to_codex_children(monkeypatch):
    monkeypatch.setenv("CC_REMOTE_CODEX_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "internal.example")

    clean = _codex_env("codex")

    assert clean["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert clean["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert clean["http_proxy"] == "http://127.0.0.1:7897"
    assert clean["https_proxy"] == "http://127.0.0.1:7897"
    assert clean["NO_PROXY"].split(",") == [
        "internal.example", "127.0.0.1", "localhost", "::1",
    ]
    assert os.environ.get("HTTP_PROXY") != "http://127.0.0.1:7897"


@pytest.mark.parametrize("value", [
    "http://user:secret@127.0.0.1:7897",
    "http://127.0.0.1:7897/path",
    "ftp://127.0.0.1:7897",
])
def test_wrapper_rejects_unsafe_codex_proxy(value):
    cfg = WrapperConfig(
        wrapper_token="a" * 32,
        codex_proxy=value,
    )
    with pytest.raises(ValueError, match="CC_REMOTE_CODEX_PROXY"):
        validate_wrapper_config(cfg)


def test_claude_sdk_options_override_inherited_control_secrets(monkeypatch):
    for key in CONTROL_PLANE_SECRET_KEYS:
        monkeypatch.setenv(key, f"secret-{key}")
    options = SdkHandle(WrapperConfig())._options(None, "/tmp")
    assert options.env == {key: "" for key in CONTROL_PLANE_SECRET_KEYS}


def test_claude_work_uses_minimal_isolated_runtime():
    handle = SdkHandle(WrapperConfig())
    handle.work_mode = True
    handle.work_settings_path = "/tmp/cc-remote-work-policy.json"

    options = handle._options(None, "/tmp/workspace")

    assert options.settings == "/tmp/cc-remote-work-policy.json"
    assert options.setting_sources == []
    assert options.skills == []
    assert options.tools == CLAUDE_WORK_TOOLS
    assert options.agents == {}
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.sandbox is None
    assert options.extra_args == {
        "replay-user-messages": None,
        "safe-mode": None,
    }
    assert options.system_prompt == WORK_SYSTEM_PROMPT
    assert isinstance(options.system_prompt, str)
    assert "not acting as a coding agent" in options.system_prompt
    assert "preset" not in options.system_prompt


def test_claude_work_passes_complete_policy_path_without_sdk_replacement(
    tmp_path,
):
    policy = tmp_path / "work-policy.json"
    payload = {
        "env": {
            "ANTHROPIC_BASE_URL": "https://provider.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "secret-must-not-enter-argv",
        },
        "permissions": {"defaultMode": "acceptEdits"},
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "filesystem": {
                "denyRead": ["~/"],
                "allowRead": [str(tmp_path / "workspace")],
                "denyWrite": ["~/"],
                "allowWrite": [str(tmp_path / "workspace")],
            },
        },
    }
    policy.write_text(json.dumps(payload), encoding="utf-8")
    handle = SdkHandle(WrapperConfig())
    handle.work_mode = True
    handle.work_settings_path = str(policy)
    options = handle._options(None, str(tmp_path / "workspace"))

    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "/verified/bundled/claude"
    command = transport._build_command()
    settings_index = command.index("--settings")

    # SDK 0.2.128 returns inline JSON here whenever options.sandbox is set,
    # replacing the policy file's complete sandbox object. Work must pass the
    # wrapper-owned file path verbatim instead.
    assert command[settings_index + 1] == str(policy)
    assert "secret-must-not-enter-argv" not in "\0".join(command)
    assert json.loads(policy.read_text(encoding="utf-8")) == payload
    assert command[command.index("--tools") + 1] == ",".join(
        CLAUDE_WORK_TOOLS)
    assert "--safe-mode" in command
    assert "--strict-mcp-config" in command
    assert "--mcp-config" not in command


def test_claude_code_keeps_official_prompt_preset_and_runtime_surface():
    ask_server = object()
    options = SdkHandle(
        WrapperConfig(), ask_server=ask_server)._options(None, "/tmp/code")

    assert options.system_prompt["preset"] == "claude_code"
    assert "cc-remote-ask" in options.system_prompt["append"]
    assert options.tools is None
    assert options.agents is None
    assert options.strict_mcp_config is False
    assert options.mcp_servers["cc-remote-ask"]["instance"] is ask_server
    assert options.extra_args == {"replay-user-messages": None}


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


def test_claude_bin_defaults_to_daily_local_cli_and_can_be_configured(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if sys.platform == "win32":
        # Path.home()/os.path.expanduser resolve "~" from USERPROFILE (or
        # HOMEDRIVE+HOMEPATH) on Windows, not from HOME.
        monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    assert WrapperConfig().claude_bin == str(home / ".local/bin/claude")

    # An explicitly empty dotenv value must not silently restore the SDK bundle.
    monkeypatch.setenv("CLAUDE_BIN", "   ")
    assert WrapperConfig().claude_bin == str(home / ".local/bin/claude")

    cli = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_BIN", f"  {cli}  ")
    cfg = WrapperConfig()
    assert cfg.claude_bin == str(cli)
    assert SdkHandle(cfg)._options(None).cli_path == str(cli)


def test_claude_pty_broker_is_hidden_and_opt_in(monkeypatch):
    monkeypatch.delenv("CC_REMOTE_EXPERIMENTAL_CLAUDE_BROKER", raising=False)
    cfg = WrapperConfig()
    assert cfg.experimental_claude_broker is False
    if sys.platform == "win32":
        assert cfg.claude_broker_socket == ""
    else:
        assert os.path.isabs(cfg.claude_broker_socket)

    monkeypatch.setenv("CC_REMOTE_EXPERIMENTAL_CLAUDE_BROKER", "true")
    assert WrapperConfig().experimental_claude_broker is True


def test_claude_bin_rejects_relative_path(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "bin/claude")
    cfg = WrapperConfig()
    with pytest.raises(ValueError, match="absolute path"):
        validate_wrapper_config(cfg)
    with pytest.raises(RuntimeError, match="absolute path"):
        SdkHandle.preflight(cfg.claude_bin)


def test_claude_preflight_inspects_effective_bundled_runtime(monkeypatch):
    seen = []
    monkeypatch.setattr(
        sdk_module,
        "inspect_claude_runtime",
        lambda configured: seen.append(configured) or SimpleNamespace(
            sdk_version="0.2.128",
            cli_version="2.1.220",
            cli_source="bundled",
            cli_path="/sdk/_bundled/claude",
        ),
    )

    SdkHandle.preflight()

    assert seen == [""]


def test_claude_preflight_inspects_configured_runtime(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        sdk_module,
        "inspect_claude_runtime",
        lambda configured: seen.append(configured) or SimpleNamespace(
            sdk_version="0.2.128",
            cli_version="2.1.220",
            cli_source="configured",
            cli_path=configured,
        ),
    )
    cli = tmp_path / "claude-custom"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)

    SdkHandle.preflight(str(cli))
    assert seen == [str(cli)]
