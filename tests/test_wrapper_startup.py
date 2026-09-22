"""Zero-token regressions for wrapper startup ordering."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from cc_remote.config import WrapperConfig
from cc_remote.wrapper import __main__ as wrapper_main
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.machine import WrapperMachine


class _Transport:
    def __init__(self) -> None:
        self.on_connected = None

    async def send(self, _message: object) -> None:
        return None


def test_prepare_codex_daemons_starts_every_profile_best_effort(
    monkeypatch, tmp_path,
) -> None:
    async def run() -> None:
        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.codex_daemon_mode = "auto"
        cfg.codex_profiles_json = json.dumps({
            "primary": {
                "label": "Primary",
                "home": str(tmp_path / "primary"),
                "default": True,
            },
            "stack": {
                "label": "Stack",
                "home": str(tmp_path / "stack"),
            },
        })
        machine = WrapperMachine(cfg, _Transport())
        assert all(not manager.allow_restart for manager in machine._codex_daemons.values())
        assert all(manager.require_shared for manager in machine._codex_daemons.values())
        calls: list[tuple[str, str, str]] = []
        resolved = 0

        def resolve() -> str:
            nonlocal resolved
            resolved += 1
            return "/opt/codex"

        def environment(_bin: str, home: str | None = None) -> dict[str, str]:
            return {"CODEX_HOME": home or "default"}

        class Manager:
            def __init__(self, profile_id: str) -> None:
                self.profile_id = profile_id

            async def ensure_started(self, binary, env):
                calls.append((self.profile_id, binary, env["CODEX_HOME"]))
                if self.profile_id == "stack":
                    raise RuntimeError("profile unavailable")
                return SimpleNamespace(socket_path=None, verified_remote_control=True)

        machine._codex_daemons = {
            profile.id: Manager(profile.id)
            for profile in machine._codex_profiles
        }
        monkeypatch.setattr(machine_module, "resolve_codex_bin", resolve)
        monkeypatch.setattr(machine_module, "codex_env", environment)

        await machine.prepare_codex_daemons()

        assert resolved == 1
        assert set(calls) == {
            ("primary", "/opt/codex", str((tmp_path / "primary").resolve())),
            ("stack", "/opt/codex", str((tmp_path / "stack").resolve())),
        }
        receipt = json.loads((cfg.state_dir / "codex-readiness.json").read_text())
        assert {row["profile"]: row["reason"] for row in receipt["profiles"]} == {
            "primary": "daemon_unavailable", "stack": "connection_failed",
        }

    asyncio.run(run())


def test_default_account_check_resolves_home_even_with_legacy_none_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    cfg = WrapperConfig(
        codex_daemon_mode="auto", state_dir=tmp_path / "state", codex_profiles_json="",
        claude_work_root=tmp_path / "claude-work", codex_work_root=tmp_path / "codex-work",
    )
    monkeypatch.setattr(machine_module, "resolve_codex_bin", lambda: "/opt/codex")
    async def check(profile, home, binary, daily, env, manager):
        assert home == str((tmp_path / ".codex").resolve())
        assert "CODEX_HOME" not in env
        return {"profile": profile, "status": "ready"}
    monkeypatch.setattr(machine_module.codex_readiness, "check_profile", check)
    machine = WrapperMachine(cfg, _Transport())
    assert machine._codex_home(machine._codex_profiles.default) is None
    asyncio.run(machine.prepare_codex_daemons())
    receipt = json.loads((cfg.state_dir / "codex-readiness.json").read_text())
    assert receipt["profiles"] == [{"profile": "primary", "status": "ready"}]


def test_disabled_sharing_publishes_without_starting_cli(monkeypatch, tmp_path) -> None:
    cfg = WrapperConfig(
        codex_daemon_mode="off", state_dir=tmp_path / "state", codex_profiles_json="",
        claude_work_root=tmp_path / "claude", codex_work_root=tmp_path / "codex",
    )
    def unexpected():
        raise AssertionError("disabled sharing must not resolve or start Codex")
    monkeypatch.setattr(machine_module, "resolve_codex_bin", unexpected)
    machine = WrapperMachine(cfg, _Transport())
    asyncio.run(machine.prepare_codex_daemons())
    receipt = json.loads((cfg.state_dir / "codex-readiness.json").read_text())
    assert receipt["profiles"] == [{"profile": "primary", "status": "disabled"}]


def test_wrapper_entrypoint_initializes_work_before_optional_codex_probe(monkeypatch) -> None:
    events: list[str] = []
    cfg = WrapperConfig()

    class FakeTransport:
        def __init__(self, *_args, **_kwargs) -> None:
            events.append("transport")

    class FakeMachine:
        viewer_pages = None
        def __init__(self, _cfg, _transport) -> None:
            events.append("machine")

        async def initialize_work(self) -> None:
            events.append("work-ready")

        async def prepare_codex_daemons(self) -> None:
            assert "work-ready" in events
            events.append("prepare")

        async def run(self) -> None:
            events.append("run")
            await asyncio.sleep(0)

    class FakeViewerTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            events.append("viewer-start")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("viewer-stop")

    monkeypatch.setattr(wrapper_main, "wrapper_config", lambda: cfg)
    monkeypatch.setattr(wrapper_main, "validate_wrapper_config", lambda _cfg: None)
    monkeypatch.setattr(
        wrapper_main, "scrub_parent_control_secrets", lambda: events.append("scrub"))
    monkeypatch.setattr(wrapper_main, "WrapperTransport", FakeTransport)
    monkeypatch.setattr(wrapper_main, "WrapperMachine", FakeMachine)
    monkeypatch.setattr(wrapper_main, "ViewerTransport", FakeViewerTransport)

    asyncio.run(wrapper_main.main())

    assert events == ["scrub", "transport", "machine", "work-ready", "prepare", "run", "viewer-start", "viewer-stop"]


def test_work_initialization_is_not_repeated_when_run_follows_prewarm(tmp_path):
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "claude"
    cfg.codex_work_root = tmp_path / "codex"
    machine = WrapperMachine(cfg, _Transport())
    calls = []
    machine._work = SimpleNamespace(initialize=lambda: calls.append("initialized"))

    async def run():
        await machine.initialize_work()
        await machine.initialize_work()

    asyncio.run(run())
    assert calls == ["initialized"]
