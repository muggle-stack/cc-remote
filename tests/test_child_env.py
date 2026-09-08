"""Claude CLI selection and child-process environment regressions."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport import subprocess_cli as sdk_subprocess_cli
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

from cc_remote.config import WrapperConfig, validate_wrapper_config
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.child_env import (
    CLAUDE_ACCOUNT_ENV_KEYS,
    CONTROL_PLANE_SECRET_KEYS,
    claude_profile_child_env,
    claude_profile_process_env,
    claude_sdk_process_env,
    sanitized_child_env,
    scrub_parent_control_secrets,
)
from cc_remote.wrapper.claude_transport import (
    AccountIsolatedSubprocessCLITransport,
    account_isolated_transport,
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
    assert options.settings is None


def test_claude_profile_child_environment_is_request_local(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-account")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/ambient/claude")
    before = dict(os.environ)

    isolated = claude_profile_child_env(
        "/profiles/company", isolate_account_env=True)

    assert isolated["CLAUDE_CONFIG_DIR"] == "/profiles/company"
    assert all(key not in isolated for key in CLAUDE_ACCOUNT_ENV_KEYS)
    assert dict(os.environ) == before


def test_isolated_claude_profile_child_environment_requires_config_dir():
    with pytest.raises(
        ValueError,
        match="isolated Claude profile requires a config directory",
    ):
        claude_profile_child_env(None, isolate_account_env=True)


def test_claude_sdk_process_environment_really_unsets_account_sources(
    monkeypatch,
):
    for key in CLAUDE_ACCOUNT_ENV_KEYS:
        monkeypatch.setenv(key, f"ambient-{key}")
    monkeypatch.setenv("CLAUDECODE", "nested")
    before = dict(os.environ)

    child = claude_sdk_process_env({
        "CLAUDE_CONFIG_DIR": "/profiles/company",
        "ANTHROPIC_PROFILE": "company",
    })

    assert child["CLAUDE_CONFIG_DIR"] == "/profiles/company"
    assert child["ANTHROPIC_PROFILE"] == "company"
    assert "CLAUDECODE" not in child
    assert all(
        key not in child
        for key in CLAUDE_ACCOUNT_ENV_KEYS
        if key not in {"CLAUDE_CONFIG_DIR", "ANTHROPIC_PROFILE"}
    )
    assert dict(os.environ) == before


@pytest.mark.asyncio
async def test_account_isolated_transport_scrubs_only_its_child_environment(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient.invalid")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-aws")
    monkeypatch.setenv("WRAPPER_TOKEN", "control-secret")
    monkeypatch.setenv("CLAUDECODE", "nested")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    parent = dict(os.environ)
    captured: dict[str, object] = {}

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None

    process = FakeProcess()

    async def fake_open_process(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr(
        "claude_agent_sdk._internal.transport.subprocess_cli.anyio.open_process",
        fake_open_process,
    )
    options = ClaudeAgentOptions(
        cli_path="/fake/claude",
        env={
            "CLAUDE_CONFIG_DIR": "/profiles/company",
            "WRAPPER_TOKEN": "",
        },
    )
    transport = account_isolated_transport(options)

    try:
        await transport.connect()
    finally:
        sdk_subprocess_cli._ACTIVE_CHILDREN.discard(process)

    child = captured["env"]
    assert isinstance(child, dict)
    assert child["CLAUDE_CONFIG_DIR"] == "/profiles/company"
    assert child["WRAPPER_TOKEN"] == ""
    assert child["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    assert "ANTHROPIC_BASE_URL" not in child
    assert "AWS_ACCESS_KEY_ID" not in child
    assert "CLAUDECODE" not in child
    assert dict(os.environ) == parent


@pytest.mark.asyncio
async def test_isolated_sdk_handle_uses_the_child_only_transport(monkeypatch):
    captured: dict[str, object] = {}

    class FakeClaudeClient:
        def __init__(self, *, options, transport):
            captured["options"] = options
            captured["transport"] = transport

        async def connect(self):
            return None

    monkeypatch.setattr(sdk_module, "ClaudeSDKClient", FakeClaudeClient)
    handle = SdkHandle(
        WrapperConfig(),
        claude_config_dir="/profiles/company",
        isolate_account_env=True,
    )
    monkeypatch.setattr(handle, "_start_message_pump", lambda: None)

    await handle.connect(cwd="/tmp", _suppress_context_probe=True)

    assert isinstance(
        captured["transport"],
        AccountIsolatedSubprocessCLITransport,
    )


def test_single_claude_account_preserves_ambient_provider_configuration(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-account")

    overlay = claude_profile_child_env(None, isolate_account_env=False)

    assert "ANTHROPIC_AUTH_TOKEN" not in overlay
    assert "CLAUDE_CONFIG_DIR" not in overlay
    assert all(overlay[key] == "" for key in CONTROL_PLANE_SECRET_KEYS)


def test_direct_claude_profile_process_keeps_runtime_environment(monkeypatch):
    monkeypatch.setenv("PATH", "/runtime/bin")
    monkeypatch.setenv("HOME", "/runtime/home")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-account")
    monkeypatch.setenv("WRAPPER_TOKEN", "control-secret")
    before = dict(os.environ)

    child = claude_profile_process_env(
        "/profiles/company", isolate_account_env=True)

    assert child["PATH"] == "/runtime/bin"
    assert child["HOME"] == "/runtime/home"
    assert child["HTTPS_PROXY"] == "http://proxy.example"
    assert child["CLAUDE_CONFIG_DIR"] == "/profiles/company"
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert "WRAPPER_TOKEN" not in child
    assert dict(os.environ) == before


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

    # SDK 0.2.151 returns inline JSON here whenever options.sandbox is set,
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


def test_claude_code_profile_loads_only_its_native_user_settings(tmp_path):
    profile = tmp_path / "company"
    profile.mkdir()
    settings = profile / "settings.json"
    settings.write_text(json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:19195",
            "ANTHROPIC_AUTH_TOKEN": "profile-secret-not-for-argv",
            "WRAPPER_TOKEN": "must-never-reach-claude",
        },
        "model": "claude-mythos-5[1m]",
    }), encoding="utf-8")

    options = SdkHandle(
        WrapperConfig(state_dir=tmp_path / "state"),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(tmp_path / "workspace"))

    # CLAUDE_CONFIG_DIR makes this the native user source. Do not also promote
    # the whole file through --settings, which would invert normal precedence
    # for every non-provider setting it contains.
    assert options.settings is None
    assert "HOME" not in options.env
    # cc-remote never opens, copies, or rewrites the user's source file.
    assert json.loads(settings.read_text(encoding="utf-8"))["env"][
        "WRAPPER_TOKEN"
    ] == "must-never-reach-claude"
    assert options.setting_sources == ["user"]
    assert options.env["CLAUDE_CONFIG_DIR"] == str(profile)
    assert all(key not in options.env for key in CLAUDE_ACCOUNT_ENV_KEYS)

    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "/verified/bundled/claude"
    command = transport._build_command()

    assert "--settings" not in command
    assert "--setting-sources=user" in command
    assert "profile-secret-not-for-argv" not in "\0".join(command)
    assert "must-never-reach-claude" not in "\0".join(command)


def test_claude_code_profile_without_settings_keeps_one_native_boundary(tmp_path):
    profile = tmp_path / "subscription"
    profile.mkdir()

    options = SdkHandle(
        WrapperConfig(state_dir=tmp_path / "state"),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(tmp_path / "workspace"))

    assert options.settings is None
    assert "HOME" not in options.env
    assert options.setting_sources == ["user"]
    assert options.env["CLAUDE_CONFIG_DIR"] == str(profile)
    assert all(key not in options.env for key in CLAUDE_ACCOUNT_ENV_KEYS)


def test_explicit_single_profile_keeps_legacy_setting_sources(tmp_path):
    profile = tmp_path / "single"
    profile.mkdir()
    settings = profile / "settings.json"
    settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://single"}}),
        encoding="utf-8",
    )
    state = tmp_path / "state"

    options = SdkHandle(
        WrapperConfig(state_dir=state),
        claude_config_dir=str(profile),
        isolate_account_env=False,
    )._options(None, str(tmp_path / "workspace"))

    assert options.settings == str(settings)
    assert options.setting_sources is None
    assert options.env["CLAUDE_CONFIG_DIR"] == str(profile)
    assert not (state / "claude-profile-homes-v1").exists()


def test_claude_profile_boundary_never_reads_or_copies_account_settings(tmp_path):
    profile = tmp_path / "company"
    profile.mkdir()
    source = profile / "settings.json"
    secret = "provider-secret-must-stay-in-profile"
    source.write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": secret}}),
        encoding="utf-8",
    )
    source_before = source.read_bytes()
    source_mtime = source.stat().st_mtime_ns
    cfg = WrapperConfig(state_dir=tmp_path / "state")
    first = SdkHandle(
        cfg,
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(tmp_path / "workspace"))

    unchanged = SdkHandle(
        cfg,
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(tmp_path / "workspace"))
    assert first.settings is None
    assert unchanged.settings is None
    assert first.env["CLAUDE_CONFIG_DIR"] == str(profile)
    assert unchanged.env["CLAUDE_CONFIG_DIR"] == str(profile)
    assert "HOME" not in first.env and "HOME" not in unchanged.env
    assert source.read_bytes() == source_before
    assert source.stat().st_mtime_ns == source_mtime
    assert not (tmp_path / "state" / "claude-profile-homes-v1").exists()
    assert secret not in repr(first.env)
    assert secret not in repr(unchanged.env)


@pytest.mark.parametrize("payload", ["not-json", "[]", '{"env": []}'])
def test_claude_profile_settings_validation_stays_owned_by_claude(
    tmp_path, payload,
):
    profile = tmp_path / "broken"
    profile.mkdir()
    (profile / "settings.json").write_text(payload, encoding="utf-8")

    options = SdkHandle(
        WrapperConfig(state_dir=tmp_path / "state"),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(tmp_path / "workspace"))

    assert options.env["CLAUDE_CONFIG_DIR"] == str(profile)
    assert "HOME" not in options.env
    assert options.settings is None
    assert payload not in repr(options.env)


def test_secondary_claude_profile_does_not_load_primary_as_home_project(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    primary = home / ".claude"
    profile = home / ".claude-stack"
    primary.mkdir(parents=True)
    profile.mkdir()
    primary_settings = primary / "settings.json"
    selected_settings = profile / "settings.json"
    primary_settings.write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://primary.invalid"},
    }), encoding="utf-8")
    selected_settings.write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:19195"},
    }), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    options = SdkHandle(
        WrapperConfig(state_dir=tmp_path / "state"),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(home))

    # With cwd=HOME, project/local resolve under HOME/.claude and therefore
    # belong to the primary account, not to this selected Stack profile.
    assert options.setting_sources == ["user"]
    assert options.settings is None
    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "/verified/bundled/claude"
    command = transport._build_command()
    assert "--setting-sources=user" in command
    assert str(primary_settings) not in command
    assert "primary.invalid" not in "\0".join(command)


def test_primary_claude_profile_is_also_isolated_from_project_settings(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    profile = home / ".claude"
    profile.mkdir(parents=True)
    (profile / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    options = SdkHandle(
        WrapperConfig(state_dir=tmp_path / "state"),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(home))

    assert options.setting_sources == ["user"]


def test_secondary_oauth_profile_without_settings_still_blocks_home_collision(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    profile = home / ".claude-subscription"
    profile.mkdir()
    monkeypatch.setenv("HOME", str(home))

    options = SdkHandle(
        WrapperConfig(state_dir=tmp_path / "state"),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(home))

    assert options.settings is None
    assert options.setting_sources == ["user"]


def test_isolated_profile_never_loads_project_provider_settings(tmp_path):
    profile = tmp_path / "profile"
    project = tmp_path / "workspace"
    project_settings = project / ".claude" / "settings.json"
    profile.mkdir()
    project_settings.parent.mkdir(parents=True)
    (profile / "settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "http://selected.invalid"},
    }), encoding="utf-8")
    project_settings.write_text(json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "https://project.invalid",
            "ANTHROPIC_AUTH_TOKEN": "project-secret",
        },
        "model": "project-model",
    }), encoding="utf-8")

    options = SdkHandle(
        WrapperConfig(state_dir=tmp_path / "state"),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )._options(None, str(project))
    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "/verified/bundled/claude"
    command = transport._build_command()

    assert options.settings is None
    assert options.setting_sources == ["user"]
    assert "--setting-sources=user" in command
    serialized = "\0".join(command)
    assert "project.invalid" not in serialized
    assert "project-secret" not in serialized
    assert "project-model" not in serialized


def test_isolated_claude_profile_never_falls_back_without_config_dir(tmp_path):
    with pytest.raises(ValueError, match="requires a config directory"):
        SdkHandle(
            WrapperConfig(state_dir=tmp_path / "state"),
            isolate_account_env=True,
        )._options(None, str(tmp_path / "workspace"))


def test_claude_work_policy_wins_over_profile_user_settings(tmp_path):
    profile = tmp_path / "company"
    profile.mkdir()
    (profile / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://profile"}}),
        encoding="utf-8",
    )
    policy = tmp_path / "work-policy.json"
    policy.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://work"}}),
        encoding="utf-8",
    )
    handle = SdkHandle(
        WrapperConfig(),
        claude_config_dir=str(profile),
        isolate_account_env=True,
    )
    handle.work_mode = True
    handle.work_settings_path = str(policy)

    options = handle._options(None, str(tmp_path / "workspace"))

    assert options.settings == str(policy)
    assert options.setting_sources == []


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
    assert options.hooks is None
    assert options.extra_args == {
        "replay-user-messages": None,
    }


@pytest.mark.parametrize(("mode", "threshold"), [
    ("inherit", None), ("auto", None), ("custom", 400_000),
])
def test_autocompact_does_not_replace_or_empty_native_system_prompt(mode, threshold):
    handle = SdkHandle(WrapperConfig())
    original = handle._options(None, "/tmp/code").system_prompt
    handle.set_auto_compact(mode, threshold)
    resumed = handle._options("native-session", "/tmp/code")
    assert resumed.system_prompt == original
    assert original["preset"] == "claude_code"
    assert original["append"].strip()


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
    assert WrapperConfig().experimental_claude_broker is False

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
            sdk_version="0.2.151",
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
            sdk_version="0.2.151",
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
