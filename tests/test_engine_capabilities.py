from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from cc_remote.protocol import (
    ERR_BAD_PROMPT,
    GetEngineCapabilities,
    ManageEnginePlugin,
)
from cc_remote.wrapper import engine_capabilities as capabilities_module
from cc_remote.wrapper import machine as machine_module
from tests.test_multisession import _mk_machine


class _ClaudeProcess:
    def __init__(self, stdout: bytes = b"[]") -> None:
        self.stdout = stdout
        self.returncode = 0
        self.killed = False

    async def communicate(self):
        return self.stdout, b""

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


def test_claude_capability_listing_uses_effective_configured_cli(
    monkeypatch, tmp_path
):
    async def run():
        configured = "/configured/claude"
        effective = "/sdk/runtime/claude"
        resolved = []
        spawned = []

        def resolve(value):
            resolved.append(value)
            return effective, "configured"

        async def spawn(*args, **kwargs):
            spawned.append((args, kwargs))
            return _ClaudeProcess(
                b'[{"id":"example","name":"Example","enabled":true}]'
            )

        monkeypatch.setattr(capabilities_module, "resolve_claude_cli", resolve)
        monkeypatch.setattr(capabilities_module, "_claude_skills", lambda _cwd: [])
        monkeypatch.setattr(capabilities_module, "_claude_hooks", lambda _cwd: [])
        monkeypatch.setattr(
            capabilities_module.asyncio, "create_subprocess_exec", spawn
        )

        items, errors, _ = await capabilities_module.engine_capabilities(
            "claude", str(tmp_path), "code", configured
        )

        assert resolved == [configured]
        assert spawned[0][0][:4] == (
            effective,
            "plugin",
            "list",
            "--json",
        )
        assert [item["id"] for item in items] == ["example"]
        assert errors == []

    asyncio.run(run())


def test_claude_skill_create_and_remove_uses_scoped_recoverable_trash(
    monkeypatch, tmp_path
):
    async def run():
        home = tmp_path / "home"
        project = tmp_path / "project"
        home.mkdir()
        project.mkdir()
        monkeypatch.setattr(
            capabilities_module.Path, "home", classmethod(lambda _cls: home)
        )

        await capabilities_module.manage_engine_skill(
            "claude", "create", str(project), name="release-notes",
            description="Write release notes", instructions="Summarize verified changes.",
            scope="user",
        )
        manifest = home / ".claude" / "skills" / "release-notes" / "SKILL.md"
        assert manifest.is_file()
        assert "Write release notes" in manifest.read_text()

        [item] = capabilities_module._claude_skills(str(project))
        await capabilities_module.manage_engine_skill(
            "claude", "remove", str(project), skill_id=item["id"]
        )
        assert not manifest.parent.exists()
        trash = home / ".claude" / ".cc-remote-trash" / "skills"
        assert [entry.name for entry in trash.iterdir()][0].startswith("release-notes-")

    asyncio.run(run())


def test_claude_user_skills_follow_config_dir_outside_home(
    monkeypatch, tmp_path
):
    async def run():
        home = tmp_path / "home"
        config = tmp_path / "profiles" / "claude"
        project = tmp_path / "project"
        home.mkdir()
        project.mkdir()
        monkeypatch.setattr(
            capabilities_module.Path, "home", classmethod(lambda _cls: home)
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

        active = config / "skills" / "active" / "SKILL.md"
        legacy = home / ".claude" / "skills" / "legacy" / "SKILL.md"
        project_skill = project / ".claude" / "skills" / "project" / "SKILL.md"
        for manifest, name in (
            (active, "active"),
            (legacy, "legacy"),
            (project_skill, "project"),
        ):
            manifest.parent.mkdir(parents=True)
            manifest.write_text(f"---\nname: {name}\n---\n")

        items = capabilities_module._claude_skills(str(project))
        assert {(item["name"], item["scope"]) for item in items} == {
            ("active", "user"),
            ("project", "project"),
        }

        await capabilities_module.manage_engine_skill(
            "claude", "create", str(project), name="created-user",
            instructions="Use the configured root.", scope="user",
        )
        await capabilities_module.manage_engine_skill(
            "claude", "create", str(project), name="created-project",
            instructions="Stay project-local.", scope="project",
        )
        user_manifest = config / "skills" / "created-user" / "SKILL.md"
        assert user_manifest.is_file()
        assert not (home / ".claude" / "skills" / "created-user").exists()
        assert (
            project / ".claude" / "skills" / "created-project" / "SKILL.md"
        ).is_file()

        created = next(
            item for item in capabilities_module._claude_skills(str(project))
            if item["name"] == "created-user"
        )
        await capabilities_module.manage_engine_skill(
            "claude", "remove", str(project), skill_id=created["id"]
        )
        assert not user_manifest.parent.exists()
        assert list((config / ".cc-remote-trash" / "skills").iterdir())

    asyncio.run(run())


def test_skill_create_rejects_path_traversal(monkeypatch, tmp_path):
    async def run():
        home = tmp_path / "home"
        project = tmp_path / "project"
        home.mkdir()
        project.mkdir()
        monkeypatch.setattr(
            capabilities_module.Path, "home", classmethod(lambda _cls: home)
        )
        with pytest.raises(ValueError, match="名称"):
            await capabilities_module.manage_engine_skill(
                "claude", "create", str(project), name="../escape",
                instructions="unsafe", scope="user",
            )
        assert not (home / ".claude" / "escape").exists()

    asyncio.run(run())


def test_claude_hook_create_remove_preserves_unrelated_settings(
    monkeypatch, tmp_path
):
    async def run():
        home = tmp_path / "home"
        project = tmp_path / "project"
        home.mkdir()
        project.mkdir()
        monkeypatch.setattr(
            capabilities_module.Path, "home", classmethod(lambda _cls: home)
        )
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "keep-me", "custom": {"x": 1}}))

        await capabilities_module.manage_engine_hook(
            "claude", "create", str(project), event="PreToolUse",
            matcher="Bash", command="/usr/local/bin/check", timeout=15,
            scope="user",
        )
        after_create = json.loads(settings.read_text())
        assert after_create["model"] == "keep-me"
        assert after_create["custom"] == {"x": 1}
        assert after_create["hooks"]["PreToolUse"][0]["matcher"] == "Bash"

        [item] = capabilities_module._claude_hooks(str(project))
        assert "/usr/local/bin/check" not in json.dumps(item)
        await capabilities_module.manage_engine_hook(
            "claude", "remove", str(project), hook_id=item["id"]
        )
        after_remove = json.loads(settings.read_text())
        assert after_remove == {"model": "keep-me", "custom": {"x": 1}}

    asyncio.run(run())


def test_claude_user_hooks_follow_config_dir_outside_home(
    monkeypatch, tmp_path
):
    async def run():
        home = tmp_path / "home"
        config = tmp_path / "profiles" / "claude"
        project = tmp_path / "project"
        home.mkdir()
        config.mkdir(parents=True)
        project.mkdir()
        monkeypatch.setattr(
            capabilities_module.Path, "home", classmethod(lambda _cls: home)
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

        active_settings = config / "settings.json"
        active_settings.write_text(json.dumps({
            "model": "keep-active",
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "a"}]}]
            },
        }))
        legacy_settings = home / ".claude" / "settings.json"
        legacy_settings.parent.mkdir(parents=True)
        legacy_settings.write_text(json.dumps({
            "hooks": {
                "Notification": [
                    {"hooks": [{"type": "command", "command": "legacy"}]}
                ]
            }
        }))
        project_settings = project / ".claude" / "settings.local.json"
        project_settings.parent.mkdir(parents=True)
        project_settings.write_text(json.dumps({
            "hooks": {
                "PostToolUse": [
                    {"hooks": [{"type": "command", "command": "project"}]}
                ]
            }
        }))

        hooks = capabilities_module._claude_hooks(str(project))
        assert {(hook["name"], hook["scope"]) for hook in hooks} == {
            ("PreToolUse", "user"),
            ("PostToolUse", "project-local"),
        }

        await capabilities_module.manage_engine_hook(
            "claude", "create", str(project), event="SessionStart",
            command="user-hook", scope="user",
        )
        await capabilities_module.manage_engine_hook(
            "claude", "create", str(project), event="SessionEnd",
            command="project-hook", scope="project",
        )
        assert json.loads(active_settings.read_text())["model"] == "keep-active"
        assert "SessionStart" in json.loads(active_settings.read_text())["hooks"]
        assert "SessionEnd" in json.loads(project_settings.read_text())["hooks"]
        assert "SessionStart" not in json.loads(legacy_settings.read_text())["hooks"]

        created = next(
            hook for hook in capabilities_module._claude_hooks(str(project))
            if hook["name"] == "SessionStart"
        )
        await capabilities_module.manage_engine_hook(
            "claude", "remove", str(project), hook_id=created["id"]
        )
        after_remove = json.loads(active_settings.read_text())
        assert after_remove["model"] == "keep-active"
        assert "SessionStart" not in after_remove["hooks"]

    asyncio.run(run())


def test_codex_skill_toggle_uses_native_config_write(monkeypatch, tmp_path):
    async def run():
        skill_path = tmp_path / ".codex" / "skills" / "example" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("---\nname: example\ndescription: test\n---\n")
        skill_id = capabilities_module._opaque_id("skill", str(skill_path))
        calls = []

        async def component(method, params, cwd):
            assert method == "skills/list"
            return {"data": [{"skills": [{
                "name": "example", "path": str(skill_path), "scope": "repo",
                "enabled": False, "description": "test",
            }]}]}

        async def rpc(method, params, *, cwd):
            calls.append((method, params, cwd))
            return {}

        monkeypatch.setattr(capabilities_module, "_codex_component", component)
        monkeypatch.setattr(capabilities_module, "codex_rpc", rpc)
        await capabilities_module.manage_engine_skill(
            "codex", "enable", str(tmp_path), skill_id=skill_id
        )
        assert calls == [("skills/config/write", {
            "path": str(skill_path), "enabled": True,
        }, str(tmp_path))]

    asyncio.run(run())


def test_codex_capabilities_include_native_hooks_as_read_only(monkeypatch, tmp_path):
    async def run():
        async def components(requests, cwd):
            assert cwd == str(tmp_path)
            responses = {
                "skills/list": {"data": []},
                "hooks/list": {"data": [{"hooks": [{
                    "key": "private-native-key", "eventName": "preToolUse",
                    "enabled": True, "source": "user", "trustStatus": "trusted",
                    "handlerType": "command", "matcher": "shell",
                    "statusMessage": "Checking command", "command": "secret command",
                }], "warnings": []}]},
                "plugin/list": {"marketplaces": []},
                "app/list": {"data": []},
                "mcpServerStatus/list": {"data": []},
            }
            return [responses[method] for method, _params in requests]

        monkeypatch.setattr(
            capabilities_module, "_codex_components", components
        )
        items, errors, _notes = await capabilities_module.codex_capabilities(
            str(tmp_path), "code"
        )
        [hook] = [item for item in items if item["kind"] == "hook"]
        assert hook["event"] == "preToolUse"
        assert hook["status"] == "trusted"
        assert hook["actions"] == []
        assert "private-native-key" not in json.dumps(hook)
        assert "secret command" not in json.dumps(hook)
        assert errors == []

    asyncio.run(run())


def test_codex_capabilities_keep_skills_when_another_component_times_out(
    monkeypatch, tmp_path,
):
    async def run():
        skill_path = tmp_path / ".codex" / "skills" / "example" / "SKILL.md"

        async def batch(requests, cwd, *, timeout):
            assert cwd == str(tmp_path)
            assert timeout == capabilities_module._COMPONENT_TIMEOUT
            responses = {
                "skills/list": {"data": [{"skills": [{
                    "name": "example",
                    "path": str(skill_path),
                    "scope": "repo",
                    "enabled": True,
                }]}]},
                "hooks/list": RuntimeError("timed out"),
                "plugin/list": RuntimeError("timed out"),
                "app/list": RuntimeError("timed out"),
                "mcpServerStatus/list": RuntimeError("timed out"),
            }
            return [responses[method] for method, _params in requests]

        monkeypatch.setattr(capabilities_module, "codex_rpc_batch", batch)
        items, errors, _notes = await capabilities_module.codex_capabilities(
            str(tmp_path), "code"
        )

        assert [item["name"] for item in items if item["kind"] == "skill"] == [
            "example"
        ]
        assert errors == [
            "hooks: app-server request failed",
            "plugins: app-server request failed",
            "apps: app-server request failed",
            "mcp: app-server request failed",
        ]

    asyncio.run(run())


def test_codex_skills_only_skips_unrelated_component_requests(
    monkeypatch, tmp_path,
):
    async def run():
        skill_path = tmp_path / ".codex" / "skills" / "example" / "SKILL.md"
        seen = []

        async def components(requests, cwd):
            seen.extend(requests)
            assert cwd == str(tmp_path)
            return [{"data": [{"skills": [{
                "name": "example",
                "path": str(skill_path),
                "scope": "repo",
                "enabled": True,
            }]}]}]

        monkeypatch.setattr(capabilities_module, "_codex_components", components)
        items, errors, notes = await capabilities_module.codex_capabilities(
            str(tmp_path), "code", skills_only=True
        )

        assert [method for method, _params in seen] == ["skills/list"]
        assert [item["name"] for item in items] == ["example"]
        assert errors == []
        assert notes == []

    asyncio.run(run())


def test_claude_skills_only_does_not_spawn_plugin_cli(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setattr(capabilities_module, "_claude_skills", lambda _cwd: [{
            "kind": "skill",
            "id": "skill:example",
            "name": "example",
            "enabled": True,
            "scope": "user",
            "actions": ["remove"],
        }])
        monkeypatch.setattr(
            capabilities_module,
            "resolve_claude_cli",
            lambda _configured: pytest.fail(
                "skills-only discovery must not resolve the plugin CLI"
            ),
        )

        items, errors, notes = await capabilities_module.engine_capabilities(
            "claude", str(tmp_path), "code", "/configured/claude",
            skills_only=True,
        )

        assert [item["name"] for item in items] == ["example"]
        assert errors == []
        assert notes == []

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["skill", "hook"])
def test_work_rejects_extension_mutations(kind, tmp_path):
    async def run():
        if kind == "skill":
            with pytest.raises(ValueError, match="Work"):
                await capabilities_module.manage_engine_skill(
                    "claude", "create", str(tmp_path), space="work",
                    name="example", instructions="test",
                )
        else:
            with pytest.raises(ValueError, match="Work"):
                await capabilities_module.manage_engine_hook(
                    "claude", "create", str(tmp_path), space="work",
                    event="PreToolUse", command="true",
                )

    asyncio.run(run())


def test_project_hook_rejects_symlinked_config_directory(monkeypatch, tmp_path):
    async def run():
        home = tmp_path / "home"
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        home.mkdir()
        project.mkdir()
        outside.mkdir()
        try:
            (project / ".claude").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if sys.platform == "win32":
                pytest.skip(f"Windows symlink privilege is unavailable: {exc}")
            raise
        monkeypatch.setattr(
            capabilities_module.Path, "home", classmethod(lambda _cls: home)
        )
        with pytest.raises(ValueError, match="目录"):
            await capabilities_module.manage_engine_hook(
                "claude", "create", str(project), event="PreToolUse",
                command="true", scope="project",
            )
        assert not (outside / "settings.local.json").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("action", "verb"),
    (("install", "install"), ("uninstall", "uninstall")),
)
def test_claude_plugin_mutation_uses_effective_configured_cli(
    monkeypatch, tmp_path, action, verb
):
    async def run():
        configured = "/configured/claude"
        effective = "/sdk/runtime/claude"
        resolved = []
        spawned = []

        def resolve(value):
            resolved.append(value)
            return effective, "configured"

        async def spawn(*args, **kwargs):
            spawned.append((args, kwargs))
            return _ClaudeProcess()

        monkeypatch.setattr(capabilities_module, "resolve_claude_cli", resolve)
        monkeypatch.setattr(
            capabilities_module.asyncio, "create_subprocess_exec", spawn
        )

        await capabilities_module.manage_engine_plugin(
            "claude",
            "example",
            action,
            str(tmp_path),
            space="code",
            claude_bin=configured,
        )

        assert resolved == [configured]
        assert spawned[0][0][:4] == (
            effective,
            "plugin",
            verb,
            "example",
        )

    asyncio.run(run())


def test_work_plugin_mutation_is_rejected_before_engine_access(
    monkeypatch, tmp_path
):
    async def run():
        monkeypatch.setattr(
            capabilities_module,
            "resolve_claude_cli",
            lambda _configured: pytest.fail("Work mutation must not resolve a CLI"),
        )
        with pytest.raises(ValueError, match="Work"):
            await capabilities_module.manage_engine_plugin(
                "claude",
                "example",
                "install",
                str(tmp_path),
                space="work",
                claude_bin="/configured/claude",
            )

    asyncio.run(run())


def test_machine_forwards_configured_cli_to_capability_listing(
    monkeypatch, tmp_path
):
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.claude_bin = "/configured/claude"
        machine._focused_ctx = lambda: SimpleNamespace(
            engine="claude",
            space="code",
            cwd="/different/focused/project",
        )
        seen = []

        async def discover(
            engine, cwd, space, claude_bin, *, skills_only=False,
        ):
            seen.append((engine, cwd, space, claude_bin, skills_only))
            return [], [], []

        monkeypatch.setattr(machine_module, "engine_capabilities", discover)
        await machine._handle_get_engine_capabilities(
            GetEngineCapabilities(
                engine="claude", cwd=str(tmp_path), client_id="client-1",
                cmd_id="capabilities-1", skills_only=True,
            )
        )

        assert seen == [
            ("claude", str(tmp_path), "code", "/configured/claude", True)
        ]
        assert transport.sent[-1].request_id == "capabilities-1"
        assert transport.sent[-1].cwd == str(tmp_path)
        assert transport.sent[-1].skills_only is True

    asyncio.run(run())


def test_machine_forwards_work_space_to_plugin_backend(monkeypatch, tmp_path):
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.claude_bin = "/configured/claude"
        machine._focused_ctx = lambda: SimpleNamespace(
            engine="claude",
            space="work",
            cwd="/different/focused/project",
        )
        seen = []

        async def reject(engine, plugin_id, action, cwd, *, space, claude_bin):
            seen.append((engine, plugin_id, action, cwd, space, claude_bin))
            raise ValueError("Work 不允许修改引擎插件")

        monkeypatch.setattr(machine_module, "manage_engine_plugin", reject)
        result = await machine._handle_manage_engine_plugin(
            ManageEnginePlugin(
                engine="claude",
                action="install",
                plugin_id="example",
                space="work",
                cwd=str(tmp_path),
                client_id="client-1",
            )
        )

        assert seen == [
            (
                "claude",
                "example",
                "install",
                str(tmp_path),
                "work",
                "/configured/claude",
            )
        ]
        assert result.code == ERR_BAD_PROMPT
        assert result in transport.sent
        assert transport.sent[-1].type == "engine_capabilities"

    asyncio.run(run())
