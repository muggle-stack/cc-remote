import asyncio
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.codex_daemon_restart import write_restart_state
from cc_remote.codex_profiles import (
    CodexProfileRegistry,
    CodexProfileTopologyStore,
)
from cc_remote.protocol import (
    ERR_AUTH,
    CodexProfileInfo,
    Error,
    SessionList,
    SessionListInvalidated,
    WorkArtifacts,
    WorkDashboard,
    deserialize,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_external import HolderScan
from cc_remote.wrapper.codex_controls import CodexControlStore
from cc_remote.wrapper.codex_forks import CodexForkJournal
from cc_remote.wrapper.codex_turn_leases import CodexTurnLeaseStore
from cc_remote.wrapper.process_scan import ProcessIdentity
from cc_remote.wrapper.session_pins import SessionPinStore
from cc_remote.wrapper.session_plans import SessionPlanStore
from cc_remote.wrapper.session_presentation import SessionPresentationStore
from cc_remote.wrapper.machine import WrapperMachine, _CodexHistoryProfiles
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.workspaces import WorkRegistry
from cc_remote.viewer_pages import PageScope, PageRef


def _profiles(primary: Path, stack: Path) -> str:
    return json.dumps({
        "primary": {
            "label": "主账号",
            "home": str(primary),
            "default": True,
        },
        "stack": {
            "label": "Stack",
            "home": str(stack),
        },
    })


class _StubTransport:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.on_connected = None

    async def send(self, message: object) -> None:
        self.sent.append(message)


def _machine(tmp_path: Path) -> tuple[WrapperMachine, _StubTransport]:
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = _profiles(
        tmp_path / "primary", tmp_path / "stack")
    transport = _StubTransport()
    return WrapperMachine(cfg, transport), transport


def _context(
    key: str,
    native_session_id: str,
    profile_id: str,
) -> SessionContext:
    return SessionContext(
        session_id=native_session_id,
        sdk=object(),
        buffer=RingBuffer(100, 1024 * 1024),
        cwd="/tmp/cc-remote-profile-test",
        key=key,
        engine="codex",
        codex_profile_id=profile_id,
    )


def test_cli_thread_catalog_hint_dedupe_is_profile_scoped(tmp_path: Path) -> None:
    async def run() -> None:
        machine, transport = _machine(tmp_path)
        primary = _context("primary@current-primary", "current-primary", "primary")
        stack = _context("stack@current-stack", "current-stack", "stack")
        machine.sessions[primary.key] = primary
        machine.sessions[stack.key] = stack

        machine._on_codex_thread_started_hint(primary, "same-native-new")
        machine._on_codex_thread_started_hint(stack, "same-native-new")
        tasks = tuple(machine._codex_catalog_hint_tasks)
        assert len(tasks) == 1
        await asyncio.gather(*tasks)

        assert set(machine._codex_thread_started_hints) == {
            ("primary", "same-native-new"),
            ("stack", "same-native-new"),
        }
        assert len([
            event for event in transport.sent
            if isinstance(event, SessionListInvalidated)
        ]) == 1
        candidates = machine._codex_exact_catalog_candidates()
        assert candidates["primary"]["same-native-new"]["hinted"] is True
        assert candidates["stack"]["same-native-new"]["hinted"] is True

    asyncio.run(run())


def test_private_btw_thread_never_becomes_a_catalog_hint(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    btw = _context("btw-private", "private-native", "primary")
    btw.btw = True

    machine._on_codex_thread_started_hint(btw, "private-native")

    assert machine._codex_thread_started_hints == {}
    assert machine._codex_catalog_hint_tasks == set()


def test_native_catalog_filters_exact_private_btw_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        machine, _transport = _machine(tmp_path)
        btw = _context("btw-private", "private-native", "primary")
        btw.btw = True
        machine._register_private_codex_btw_thread(btw, "private-native")

        async def list_rows(_limit, *, codex_home=None):
            profile_id = Path(codex_home).name
            if profile_id != "primary":
                return []
            return [
                {
                    "session_id": "private-native",
                    "summary": None,
                    "first_prompt": None,
                    "cwd": "/repo/private",
                    "last_modified": "20",
                    "git_branch": None,
                    "forked_from_id": None,
                    "status": None,
                    "tag": None,
                },
                {
                    "session_id": "ordinary-native",
                    "summary": "Ordinary",
                    "first_prompt": "Ordinary",
                    "cwd": "/repo/ordinary",
                    "last_modified": "10",
                    "git_branch": None,
                    "forked_from_id": None,
                    "status": None,
                    "tag": None,
                },
            ]

        monkeypatch.setattr(machine_module, "list_codex_sessions", list_rows)
        rows = await machine._read_all_codex_profile_sessions()

        assert [row["session_id"] for row in rows] == [
            "primary@ordinary-native",
        ]
        assert btw.btw_real_id == "private-native"

    asyncio.run(run())


def test_inflight_catalog_send_cannot_publish_newly_private_btw(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        machine, transport = _machine(tmp_path)
        btw = _context("btw-private", "private-native", "primary")
        btw.btw = True
        raw = [{
            "session_id": "primary@private-native",
            "native_session_id": "private-native",
            "summary": "新会话",
            "first_prompt": None,
            "cwd": "/repo/private",
            "last_modified": "20",
            "git_branch": None,
            "forked_from_id": None,
            "status": "idle",
            "tag": None,
            "codex_profile_id": "primary",
            "codex_profile_label": "主账号",
        }]

        async def register_while_claiming(_rows):
            machine._register_private_codex_btw_thread(
                btw, "private-native")

        machine._claim_legacy_presentation_from_codex_catalog = (
            register_while_claiming)
        event = await machine._send_codex_session_list(
            SimpleNamespace(
                space="code", client_id="client-1", cmd_id="list-1"),
            raw,
        )

        assert isinstance(event, SessionList)
        assert event.sessions == []
        assert transport.sent[-1] is event
        assert "primary@private-native" not in machine._codex_sidebar_watches

    asyncio.run(run())


def test_cli_thread_catalog_hint_survives_placeholder_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        machine, _transport = _machine(tmp_path)
        primary = _context(
            "primary@current-primary", "current-primary", "primary")
        machine.sessions[primary.key] = primary
        machine._codex_thread_started_hints[
            ("primary", "cli-long-running")
        ] = 1.0
        monkeypatch.setattr(machine_module.time, "monotonic", lambda: 60.0)

        machine._on_codex_thread_started_hint(primary, "cli-newer")
        await asyncio.gather(*tuple(machine._codex_catalog_hint_tasks))

        assert set(machine._codex_thread_started_hints) == {
            ("primary", "cli-long-running"),
            ("primary", "cli-newer"),
        }
        candidates = machine._codex_exact_catalog_candidates()
        assert candidates["primary"]["cli-long-running"]["hinted"] is True
        assert candidates["primary"]["cli-long-running"]["hint_fresh"] is False
        assert candidates["primary"]["cli-newer"]["hint_fresh"] is True
        assert (
            candidates["primary"]["cli-long-running"]["created_at"]
            < candidates["primary"]["cli-newer"]["created_at"]
        )

    asyncio.run(run())


def test_hidden_cli_thread_hint_materializes_in_its_own_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        machine, _transport = _machine(tmp_path)
        primary = _context(
            "primary@current-primary", "current-primary", "primary")
        machine.sessions = {primary.key: primary}

        async def empty_list(_limit, *, codex_home=None):
            return []

        def exact_rows(session_ids, *, codex_home=None):
            assert Path(codex_home).name == "primary"
            return [{
                "session_id": "cli-hidden",
                "summary": None,
                "first_prompt": "CLI prompt",
                "cwd": "/repo/cli",
                "last_modified": "30",
                "git_branch": None,
                "forked_from_id": None,
                "status": None,
                "tag": None,
            }] if "cli-hidden" in session_ids else []

        monkeypatch.setattr(machine_module, "list_codex_sessions", empty_list)
        monkeypatch.setattr(
            machine_module, "codex_exact_catalog_rows", exact_rows)
        machine._on_codex_thread_started_hint(primary, "cli-hidden")
        await asyncio.gather(*tuple(machine._codex_catalog_hint_tasks))

        rows = await machine._read_all_codex_profile_sessions()

        hidden = next(
            row for row in rows
            if row["session_id"] == "primary@cli-hidden"
        )
        assert hidden["codex_profile_id"] == "primary"
        assert hidden["cwd"] == "/repo/cli"
        assert all(row["session_id"] != "stack@cli-hidden" for row in rows)

    asyncio.run(run())


def test_codex_id_capture_broadcasts_catalog_invalidation(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        machine, transport = _machine(tmp_path)
        ctx = SessionContext(
            session_id=None,
            sdk=object(),
            buffer=RingBuffer(100, 1024 * 1024),
            cwd="/repo/new",
            key="tmp-new",
            engine="codex",
            codex_profile_id="primary",
        )
        machine.sessions = {ctx.key: ctx}

        await machine._capture_session_id(ctx, "captured-native")

        assert ctx.key == "primary@captured-native"
        assert [event.type for event in transport.sent][-2:] == [
            "session_rekey",
            "session_list_invalidated",
        ]
        invalidated = transport.sent[-1]
        assert invalidated.engine == "codex"
        assert invalidated.space == "code"

    asyncio.run(run())


def test_empty_configuration_preserves_single_profile_compatibility(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    registry = CodexProfileRegistry.from_json("", default_home=home)

    assert registry.default.id == "primary"
    assert registry.default.home == home.resolve()
    assert registry.wire_session_id("primary", "same-native-id") == "same-native-id"
    assert registry.resolve_wire_session_id("same-native-id") == (
        registry.default,
        "same-native-id",
    )


def test_profile_file_is_a_stable_launchagent_configuration_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_file = tmp_path / "codex-profiles.json"
    payload = _profiles(tmp_path / "primary", tmp_path / "stack")
    profile_file.write_text(payload, encoding="utf-8")
    monkeypatch.delenv("CC_REMOTE_CODEX_PROFILES_JSON", raising=False)
    monkeypatch.setenv("CC_REMOTE_CODEX_PROFILES_FILE", str(profile_file))

    assert WrapperConfig().codex_profiles_json == payload

    inline = json.dumps({
        "solo": {
            "label": "Solo",
            "home": str(tmp_path / "solo"),
            "default": True,
        },
    })
    monkeypatch.setenv("CC_REMOTE_CODEX_PROFILES_JSON", inline)
    assert WrapperConfig().codex_profiles_json == inline


@pytest.mark.asyncio
async def test_legacy_custom_codex_home_reaches_profile_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = (tmp_path / "custom-codex-home").resolve()
    monkeypatch.setenv("CODEX_HOME", str(home))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = ""
    machine = WrapperMachine(cfg, _StubTransport())
    catalog_homes: list[str | None] = []
    rollout_homes: list[str | None] = []

    async def fake_list(
        _limit: int,
        *,
        codex_home: str | None = None,
    ) -> list[dict]:
        catalog_homes.append(codex_home)
        return []

    def fake_rollout(
        session_id: str,
        *,
        codex_home: str | None = None,
    ) -> str:
        assert session_id == "native-id"
        rollout_homes.append(codex_home)
        return "/tmp/rollout.jsonl"

    monkeypatch.setattr(machine_module, "list_codex_sessions", fake_list)
    monkeypatch.setattr(machine_module, "codex_rollout_path", fake_rollout)

    assert await machine._read_all_codex_profile_sessions() == []
    assert machine._codex_rollout_for_wire("native-id") == (
        "/tmp/rollout.jsonl"
    )
    assert catalog_homes == [str(home)]
    assert rollout_homes == [str(home)]


def test_non_default_profile_namespaces_identical_native_session_ids(
    tmp_path: Path,
) -> None:
    registry = CodexProfileRegistry.from_json(
        _profiles(tmp_path / "primary", tmp_path / "stack"),
    )

    assert registry.wire_session_id("primary", "same-native-id") == (
        "primary@same-native-id"
    )
    assert registry.wire_session_id("stack", "same-native-id") == (
        "stack@same-native-id"
    )
    profile, native = registry.resolve_wire_session_id("stack@same-native-id")
    assert profile.id == "stack"
    assert native == "same-native-id"


def test_one_explicit_profile_preserves_native_session_ids(tmp_path: Path) -> None:
    registry = CodexProfileRegistry.from_json(json.dumps({
        "solo": {
            "label": "Solo",
            "home": str(tmp_path / "solo"),
            "default": True,
        },
    }))

    assert registry.wire_session_id("solo", "native-id") == "native-id"
    profile, native = registry.resolve_wire_session_id("native-id")
    assert (profile.id, native) == ("solo", "native-id")


def test_only_multi_profile_machines_require_shared_daemons(
    tmp_path: Path,
) -> None:
    multi, _transport = _machine(tmp_path)
    assert {
        profile_id: manager.require_shared
        for profile_id, manager in multi._codex_daemons.items()
    } == {"primary": True, "stack": True}

    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "single-state"
    cfg.claude_work_root = tmp_path / "single-work" / "claude"
    cfg.codex_work_root = tmp_path / "single-work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "solo": {
            "label": "Solo",
            "home": str(tmp_path / "solo"),
            "default": True,
        },
    })
    single = WrapperMachine(cfg, _StubTransport())
    assert single._codex_daemons["solo"].require_shared is False


@pytest.mark.parametrize("raw", ["", "explicit"])
def test_single_profile_rejects_namespaced_session_aliases(
    raw: str,
    tmp_path: Path,
) -> None:
    registry = CodexProfileRegistry.from_json(
        "" if raw == "" else json.dumps({
            "solo": {
                "label": "Solo",
                "home": str(tmp_path / "solo"),
                "default": True,
            },
        }),
        default_home=tmp_path / "default",
    )

    with pytest.raises(ValueError, match="must not be namespaced"):
        registry.resolve_wire_session_id(
            f"{registry.default.id}@native-id")


def test_multi_profile_rejects_unnamespaced_native_session_id(
    tmp_path: Path,
) -> None:
    registry = CodexProfileRegistry.from_json(
        _profiles(tmp_path / "primary", tmp_path / "stack"),
    )

    with pytest.raises(ValueError, match="namespaced"):
        registry.resolve_wire_session_id("same-native-id")


def test_unknown_profile_prefix_does_not_fall_back_to_default(
    tmp_path: Path,
) -> None:
    registry = CodexProfileRegistry.from_json(
        _profiles(tmp_path / "primary", tmp_path / "stack"),
    )

    with pytest.raises(ValueError, match="unknown Codex profile"):
        registry.resolve_wire_session_id("missing@same-native-id")


def test_native_session_id_cannot_contain_profile_separator(
    tmp_path: Path,
) -> None:
    registry = CodexProfileRegistry.from_json(
        _profiles(tmp_path / "primary", tmp_path / "stack"),
    )

    with pytest.raises(ValueError, match="invalid native Codex session id"):
        registry.wire_session_id("primary", "native@ambiguous")
    with pytest.raises(ValueError, match="invalid native Codex session id"):
        registry.resolve_wire_session_id("stack@native@ambiguous")


def test_profile_prefix_and_native_id_fit_the_wire_id_boundary(
    tmp_path: Path,
) -> None:
    profile_id = "p" * 32
    registry = CodexProfileRegistry.from_json(json.dumps({
        profile_id: {
            "label": "Long profile",
            "home": str(tmp_path / "long"),
            "default": True,
        },
        "other": {
            "label": "Other",
            "home": str(tmp_path / "other"),
        },
    }))
    accepted_native = "n" * 95

    assert registry.wire_session_id(
        profile_id, accepted_native,
    ) == f"{profile_id}@{accepted_native}"
    with pytest.raises(ValueError, match="too long"):
        registry.wire_session_id(profile_id, "n" * 96)
    with pytest.raises(ValueError, match="invalid Codex wire session id"):
        registry.resolve_wire_session_id(f"{profile_id}@{'n' * 96}")

    single = CodexProfileRegistry.from_json("")
    assert single.wire_session_id("primary", "n" * 128) == "n" * 128


@pytest.mark.parametrize("profile_id", ["bad@id", "bad:id", " has-space", ""])
def test_profile_ids_are_strict(profile_id: str, tmp_path: Path) -> None:
    raw = json.dumps({
        profile_id: {
            "label": "Bad",
            "home": str(tmp_path / "bad"),
            "default": True,
        },
    })
    with pytest.raises(ValueError, match="profile id"):
        CodexProfileRegistry.from_json(raw)


def test_profile_homes_must_be_unique_after_realpath(tmp_path: Path) -> None:
    home = tmp_path / "same"
    raw = json.dumps({
        "primary": {"label": "One", "home": str(home), "default": True},
        "stack": {"label": "Two", "home": str(home / ".")},
    })
    with pytest.raises(ValueError, match="unique"):
        CodexProfileRegistry.from_json(raw)


def test_exactly_one_explicit_default_is_allowed(tmp_path: Path) -> None:
    raw = json.dumps({
        "primary": {
            "label": "One",
            "home": str(tmp_path / "one"),
            "default": True,
        },
        "stack": {
            "label": "Two",
            "home": str(tmp_path / "two"),
            "default": True,
        },
    })
    with pytest.raises(ValueError, match="one default"):
        CodexProfileRegistry.from_json(raw)


def test_profile_registry_supports_named_ui_range_and_stays_bounded(
    tmp_path: Path,
) -> None:
    payload = {
        f"account-{index}": {
            "label": f"Account {index}",
            "home": str(tmp_path / f"account-{index}"),
            "default": index == 0,
        }
        for index in range(14)
    }
    assert len(CodexProfileRegistry.from_json(json.dumps(payload))) == 14

    payload.update({
        f"account-{index}": {
            "label": f"Account {index}",
            "home": str(tmp_path / f"account-{index}"),
        }
        for index in range(14, 33)
    })
    with pytest.raises(ValueError, match="at most 32"):
        CodexProfileRegistry.from_json(json.dumps(payload))


def test_session_list_round_trips_more_than_legacy_eight_profiles() -> None:
    profiles = [
        CodexProfileInfo(id=f"account-{index}", label=f"Account {index}")
        for index in range(14)
    ]

    restored = deserialize(serialize(SessionList(
        engine="codex",
        sessions=[],
        codex_profiles=profiles,
        default_codex_profile_id="account-0",
    )))

    assert isinstance(restored, SessionList)
    assert [profile.id for profile in restored.codex_profiles] == [
        f"account-{index}" for index in range(14)
    ]
    assert restored.default_codex_profile_id == "account-0"


def test_public_metadata_never_exposes_codex_home(tmp_path: Path) -> None:
    registry = CodexProfileRegistry.from_json(
        _profiles(tmp_path / "primary", tmp_path / "stack"),
    )

    assert registry.public_profiles() == [
        {"id": "primary", "label": "主账号"},
        {"id": "stack", "label": "Stack"},
    ]
    assert all("home" not in entry for entry in registry.public_profiles())


def test_same_native_uuid_contexts_remain_distinct(tmp_path: Path) -> None:
    machine, _transport = _machine(tmp_path)
    primary = _context(
        "primary@same-native-id", "same-native-id", "primary")
    stack = _context("stack@same-native-id", "same-native-id", "stack")
    machine.sessions = {primary.key: primary, stack.key: stack}

    assert machine._ctx_by_sid("primary@same-native-id") is primary
    assert machine._ctx_by_sid("same-native-id") is None
    assert machine._ctx_by_sid("stack@same-native-id") is stack
    assert machine._ctx_by_sid("missing@same-native-id") is None
    assert machine._codex_daemon_for_profile("primary") is not (
        machine._codex_daemon_for_profile("stack")
    )
    assert machine._codex_restart_path_for_ctx(primary) == (
        tmp_path / "state" / "codex-daemon-restart-primary.json"
    )
    assert machine._codex_restart_path_for_ctx(stack) == (
        tmp_path / "state" / "codex-daemon-restart-stack.json"
    )


def test_profile_rollouts_and_watches_remain_in_their_own_home(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary_rollout = (
        tmp_path / "primary" / "sessions" / "2026" / "rollout-same-native-id.jsonl"
    )
    stack_rollout = (
        tmp_path / "stack" / "sessions" / "2026" / "rollout-same-native-id.jsonl"
    )
    for path in (primary_rollout, stack_rollout):
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")

    machine._watch_session("primary@same-native-id")
    machine._watch_session("stack@same-native-id")

    assert machine._watch["primary@same-native-id"]["path"] == str(
        primary_rollout)
    assert machine._watch[
        "primary@same-native-id"]["codex_profile_id"] == "primary"
    assert machine._watch["stack@same-native-id"]["path"] == str(stack_rollout)
    assert machine._watch["stack@same-native-id"]["codex_profile_id"] == "stack"
    assert machine._watch["stack@same-native-id"]["native_session_id"] == (
        "same-native-id"
    )


def test_profile_holder_scan_uses_matching_shell_and_tui_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine._watch = {
        "primary@primary-native": {
            "path": "/tmp/primary-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "primary",
        },
        "stack@stack-native": {
            "path": "/tmp/stack-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "stack",
        },
    }
    holder_calls: list[tuple[dict[str, str], str | None]] = []
    tui_calls: list[tuple[str, dict[str, str]]] = []

    def holders(paths, _own, *, shell_snapshot_root=None):
        holder_calls.append((dict(paths), shell_snapshot_root))
        return HolderScan(
            holders={sid: set() for sid in paths},
            complete=True,
            passive_holders={sid: set() for sid in paths},
            client_proxies={},
            private_holders={sid: set() for sid in paths},
        )

    class Tracker:
        def __init__(self, profile_id: str) -> None:
            self.profile_id = profile_id

        def bindings(self, paths, _proxies):
            tui_calls.append((self.profile_id, dict(paths)))
            return {}, True

    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    machine._codex_tui_log_trackers = {
        "primary": Tracker("primary"),
        "stack": Tracker("stack"),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: watch["path"] for sid, watch in machine._watch.items()
    }))

    assert scan.complete is True
    assert holder_calls == [
        (
            {"primary-native": "/tmp/primary-rollout.jsonl"},
            str((tmp_path / "primary" / "shell_snapshots").resolve()),
        ),
        (
            {"stack-native": "/tmp/stack-rollout.jsonl"},
            str((tmp_path / "stack" / "shell_snapshots").resolve()),
        ),
    ]
    assert tui_calls == [
        ("primary", {"primary-native": "/tmp/primary-rollout.jsonl"}),
        ("stack", {"stack-native": "/tmp/stack-rollout.jsonl"}),
    ]


def test_profile_holder_scan_maps_every_native_bucket_back_to_wire_sid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary_wire = "primary@same-native-id"
    stack_wire = "stack@same-native-id"
    machine._watch = {
        primary_wire: {
            "path": "/tmp/primary-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "primary",
        },
        stack_wire: {
            "path": "/tmp/stack-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "stack",
        },
    }
    primary_holder = ProcessIdentity(101, 1)
    stack_holder = ProcessIdentity(202, 2)
    calls = 0

    def holders(paths, _own, *, shell_snapshot_root=None):
        nonlocal calls
        calls += 1
        holder = primary_holder if calls == 1 else stack_holder
        return HolderScan(
            holders={"same-native-id": {holder}},
            complete=True,
            passive_holders={"same-native-id": {holder}},
            client_proxies={},
            private_holders={"same-native-id": {holder}},
        )

    class Tracker:
        def bindings(self, paths, _proxies):
            return {sid: set() for sid in paths}, True

    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    machine._codex_tui_log_trackers = {
        "primary": Tracker(),
        "stack": Tracker(),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: watch["path"] for sid, watch in machine._watch.items()
    }))

    assert scan.holders == {
        primary_wire: {primary_holder},
        stack_wire: {stack_holder},
    }
    assert scan.passive_holders == scan.holders
    assert scan.private_holders == scan.holders


def test_duplicate_native_uuid_logical_tui_stays_in_its_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary_wire = "primary@same-native-id"
    stack_wire = "stack@same-native-id"
    machine._watch = {
        primary_wire: {
            "path": "/tmp/primary-rollout.jsonl", "engine": "codex",
            "codex_profile_id": "primary",
        },
        stack_wire: {
            "path": "/tmp/stack-rollout.jsonl", "engine": "codex",
            "codex_profile_id": "stack",
        },
    }
    tui = ProcessIdentity(211, 12)

    def holders(paths, _own, *, shell_snapshot_root=None, darwin_snapshot=None):
        return HolderScan(
            holders={"same-native-id": {tui}}, complete=True,
            passive_holders={"same-native-id": set()},
            client_proxies={tui: 10},
            private_holders={"same-native-id": set()},
            logical_holders={"same-native-id": {tui}},
            darwin_snapshot=darwin_snapshot,
        )

    class Tracker:
        def bindings(self, paths, proxies):
            return {sid: set() for sid in paths}, True

    machine._codex_daemons["primary"]._ready = SimpleNamespace(
        socket_path="/tmp/primary.sock")
    machine._codex_daemons["stack"]._ready = SimpleNamespace(
        socket_path="/tmp/stack.sock")
    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    monkeypatch.setattr(
        machine_module, "codex_app_server_client_socket",
        lambda identity, **_kwargs: (
            True, str(Path("/tmp/primary.sock").resolve())
        ),
    )
    machine._codex_tui_log_trackers = {
        "primary": Tracker(), "stack": Tracker(),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: watch["path"] for sid, watch in machine._watch.items()
    }))

    assert scan.holders[primary_wire] == {tui}
    assert scan.holders[stack_wire] == set()


def test_default_reorder_keeps_restart_markers_bound_to_original_profiles(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    primary_home = tmp_path / "primary"
    stack_home = tmp_path / "stack"
    first_cfg = WrapperConfig()
    first_cfg.state_dir = state
    first_cfg.codex_profiles_json = _profiles(primary_home, stack_home)
    WrapperMachine(first_cfg, _StubTransport())

    reordered_cfg = WrapperConfig()
    reordered_cfg.state_dir = state
    reordered_cfg.codex_profiles_json = json.dumps({
        "primary": {"label": "Primary", "home": str(primary_home)},
        "stack": {
            "label": "Stack", "home": str(stack_home), "default": True,
        },
    })
    machine = WrapperMachine(reordered_cfg, _StubTransport())
    primary = _context("primary@sid-a", "sid-a", "primary")
    stack = _context("stack@sid-b", "sid-b", "stack")

    assert machine._codex_legacy_restart_profile_id == "primary"
    assert machine._codex_restart_path_for_ctx(primary) == (
        state / "codex-daemon-restart-primary.json")
    assert machine._codex_restart_path_for_ctx(stack) == (
        state / "codex-daemon-restart-stack.json")

    now = time.time()
    write_restart_state(
        state / "codex-daemon-restart.json", epoch="a" * 32,
        phase="ready", deadline_at=now + 60,
    )
    write_restart_state(
        state / "codex-daemon-restart-stack.json", epoch="b" * 32,
        phase="ready", deadline_at=now + 60,
    )
    primary_state = asyncio.run(machine._codex_restart_state_for_ctx(
        primary, wait=False))
    stack_state = asyncio.run(machine._codex_restart_state_for_ctx(
        stack, wait=False))
    assert primary_state is not None and primary_state.epoch == "a" * 32
    assert stack_state is not None and stack_state.epoch == "b" * 32
    assert machine._read_codex_restart_state_for_ctx(primary) == primary_state

    restarted = WrapperMachine(reordered_cfg, _StubTransport())
    assert restarted._codex_legacy_restart_profile_id == "primary"


def test_expired_newest_restart_alias_does_not_resurrect_older_ready(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary = _context("primary@sid-a", "sid-a", "primary")
    stable = machine._codex_restart_path_for_ctx(primary)
    legacy = machine._codex_legacy_daemon_restart_path
    write_restart_state(stable, epoch="c" * 32, phase="ready")
    write_restart_state(
        legacy, epoch="d" * 32, phase="failed", deadline_at=time.time())
    time.sleep(0.01)

    assert machine._read_codex_restart_state_for_ctx(primary) is None
    assert asyncio.run(machine._codex_restart_state_for_ctx(
        primary, wait=False)) is None


def test_active_restart_alias_is_not_hidden_by_other_ready_marker(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary = _context("primary@sid-a", "sid-a", "primary")
    legacy = machine._codex_legacy_daemon_restart_path
    stable = machine._codex_restart_path_for_ctx(primary)
    restarting = write_restart_state(
        legacy, epoch="e" * 32, phase="restarting",
        deadline_at=time.time() + 60,
    )
    write_restart_state(stable, epoch="f" * 32, phase="ready")

    assert machine._read_codex_restart_state_for_ctx(primary) == restarting


def test_legacy_restart_owner_remap_is_idempotent_during_pending_transition(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    first = CodexProfileRegistry.from_json(json.dumps({
        "a": {"label": "A", "home": str(tmp_path / "home-a"),
              "default": True},
        "b": {"label": "B", "home": str(tmp_path / "home-b")},
    }))
    store = CodexProfileTopologyStore(state)
    first_transition = store.prepare(first)
    assert first_transition is not None
    assert store.legacy_restart_profile_id(
        first, first_transition,
        profile_revision=first_transition.revision,
    ) == "a"
    store.complete(first, first_transition)

    swapped = CodexProfileRegistry.from_json(json.dumps({
        "a": {"label": "A", "home": str(tmp_path / "home-b")},
        "b": {"label": "B", "home": str(tmp_path / "home-a"),
              "default": True},
    }))
    pending = store.prepare(swapped)
    assert pending is not None
    assert pending.remaps == {"a": "b", "b": "a"}
    first_owner = store.legacy_restart_profile_id(
        swapped, pending, profile_revision=pending.revision)

    # Simulate a crash after this store commits but before topology completion.
    replayed = store.prepare(swapped)
    assert replayed is not None and replayed.revision == pending.revision
    second_owner = store.legacy_restart_profile_id(
        swapped, replayed, profile_revision=replayed.revision)
    assert first_owner == second_owner == "b"


def test_profile_id_cannot_silently_replace_its_account_home(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    original_home = tmp_path / "home-a"
    registry = CodexProfileRegistry.from_json(json.dumps({
        "a": {
            "label": "A",
            "home": str(original_home),
            "default": True,
        },
    }))
    store = CodexProfileTopologyStore(state)
    initial = store.prepare(registry)
    assert initial is not None
    store.complete(registry, initial)

    replacement = CodexProfileRegistry.from_json(json.dumps({
        "a": {
            "label": "Replacement",
            "home": str(tmp_path / "replacement-home"),
            "default": True,
        },
    }))
    with pytest.raises(ValueError, match="cannot replace its CODEX_HOME"):
        store.prepare(replacement)

    assert not (state / "codex-profile-transition.json").exists()
    persisted = json.loads(
        (state / "codex-profile-topology.json").read_text(encoding="utf-8"))
    assert persisted["profiles"] == [{
        "id": "a", "home": str(original_home.resolve()),
    }]


def test_profile_tui_trackers_receive_only_their_daemon_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine._watch = {
        "primary@primary-native": {
            "path": "/tmp/primary-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "primary",
        },
        "stack@stack-native": {
            "path": "/tmp/stack-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "stack",
        },
    }
    primary_proxy = ProcessIdentity(301, 3)
    stack_proxy = ProcessIdentity(302, 4)
    unknown_proxy = ProcessIdentity(303, 5)
    all_proxies = {
        primary_proxy: 10,
        stack_proxy: 20,
        unknown_proxy: 30,
    }
    observed: dict[str, set[ProcessIdentity]] = {}

    def holders(paths, _own, *, shell_snapshot_root=None):
        return HolderScan(
            holders={sid: set() for sid in paths},
            complete=True,
            passive_holders={sid: set() for sid in paths},
            client_proxies=dict(all_proxies),
            private_holders={sid: set() for sid in paths},
        )

    class Tracker:
        def __init__(self, profile_id: str) -> None:
            self.profile_id = profile_id

        def bindings(self, paths, proxies):
            observed[self.profile_id] = set(proxies)
            return {sid: set() for sid in paths}, True

    machine._codex_daemons["primary"]._ready = SimpleNamespace(
        socket_path="/tmp/primary.sock")
    machine._codex_daemons["stack"]._ready = SimpleNamespace(
        socket_path="/tmp/stack.sock")
    sockets = {
        primary_proxy: (
            True, str(Path("/tmp/primary.sock").resolve())),
        stack_proxy: (
            True, str(Path("/tmp/stack.sock").resolve())),
        unknown_proxy: (True, str(Path("/tmp/unregistered.sock").resolve())),
    }
    default_homes: list[str] = []

    def client_socket(identity, *, default_codex_home):
        default_homes.append(default_codex_home)
        return sockets.get(identity)

    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    monkeypatch.setattr(
        machine_module,
        "codex_app_server_client_socket",
        client_socket,
    )
    machine._codex_tui_log_trackers = {
        "primary": Tracker("primary"),
        "stack": Tracker("stack"),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: watch["path"] for sid, watch in machine._watch.items()
    }))

    assert scan.complete is True
    assert scan.client_proxies == {
        primary_proxy: 10,
        stack_proxy: 20,
    }
    assert observed == {
        "primary": {primary_proxy},
        "stack": {stack_proxy},
    }
    assert default_homes == [
        str((Path.home() / ".codex").resolve()),
    ] * len(all_proxies)


def test_cold_profile_ignores_sibling_proxy_without_blocking_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine._watch = {
        "primary@primary-native": {
            "path": "/tmp/primary-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "primary",
        },
        "stack@stack-native": {
            "path": "/tmp/stack-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "stack",
        },
    }
    primary_proxy = ProcessIdentity(311, 13)
    observed: dict[str, set[ProcessIdentity]] = {}

    def holders(paths, _own, *, shell_snapshot_root=None):
        return HolderScan(
            holders={sid: set() for sid in paths},
            complete=True,
            passive_holders={sid: set() for sid in paths},
            client_proxies={primary_proxy: 10},
            private_holders={sid: set() for sid in paths},
        )

    class Tracker:
        def __init__(self, profile_id: str) -> None:
            self.profile_id = profile_id

        def bindings(self, paths, proxies):
            observed[self.profile_id] = set(proxies)
            return {sid: set() for sid in paths}, True

    machine._codex_daemons["primary"]._ready = SimpleNamespace(
        socket_path="/tmp/primary.sock")
    assert machine._codex_daemons["stack"].socket_path is None
    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    monkeypatch.setattr(
        machine_module,
        "codex_app_server_client_socket",
        lambda identity, **_kwargs: (
            (True, str(Path("/tmp/primary.sock").resolve()))
            if identity == primary_proxy else (True, None)
        ),
    )
    machine._codex_tui_log_trackers = {
        "primary": Tracker("primary"),
        "stack": Tracker("stack"),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: watch["path"] for sid, watch in machine._watch.items()
    }))

    assert scan.complete is True
    assert scan.client_proxies == {primary_proxy: 10}
    assert observed == {
        "primary": {primary_proxy},
        "stack": set(),
    }


def test_unreadable_client_profile_keeps_owner_scan_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    sid = "primary@primary-native"
    machine._watch = {
        sid: {
            "path": "/tmp/primary-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "primary",
        },
    }
    client = ProcessIdentity(315, 13)

    def holders(paths, _own, *, shell_snapshot_root=None):
        return HolderScan(
            holders={native_sid: {client} for native_sid in paths},
            complete=True,
            passive_holders={native_sid: set() for native_sid in paths},
            client_proxies={client: 10},
            private_holders={native_sid: set() for native_sid in paths},
            logical_holders={native_sid: {client} for native_sid in paths},
        )

    class Tracker:
        def bindings(self, paths, proxies):
            assert proxies == {client: 10}
            return {native_sid: set() for native_sid in paths}, True

    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    monkeypatch.setattr(
        machine_module,
        "codex_app_server_client_socket",
        lambda _identity, **_kwargs: (False, None),
    )
    machine._codex_tui_log_trackers = {
        "primary": Tracker(),
        "stack": Tracker(),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: machine._watch[sid]["path"],
    }))

    assert scan.complete is False
    assert scan.incomplete_sids == {sid}
    assert scan.holders[sid] == set()
    assert scan.client_proxies == {}


def test_unreadable_logical_tui_does_not_freeze_unrelated_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary_sid = "primary@primary-native"
    stack_sid = "stack@stack-native"
    machine._watch = {
        primary_sid: {
            "path": "/tmp/primary-rollout.jsonl", "engine": "codex",
            "codex_profile_id": "primary",
        },
        stack_sid: {
            "path": "/tmp/stack-rollout.jsonl", "engine": "codex",
            "codex_profile_id": "stack",
        },
    }
    client = ProcessIdentity(316, 14)

    def holders(paths, _own, *, shell_snapshot_root=None, darwin_snapshot=None):
        logical = {
            native_sid: ({client} if native_sid == "primary-native" else set())
            for native_sid in paths
        }
        return HolderScan(
            holders={native_sid: set(value) for native_sid, value in logical.items()},
            complete=True,
            passive_holders={native_sid: set() for native_sid in paths},
            client_proxies={client: 10},
            private_holders={native_sid: set() for native_sid in paths},
            logical_holders=logical,
            darwin_snapshot=darwin_snapshot,
        )

    class Tracker:
        def bindings(self, paths, proxies):
            assert proxies == {client: 10}
            return {native_sid: set() for native_sid in paths}, True

    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    monkeypatch.setattr(
        machine_module, "codex_app_server_client_socket",
        lambda _identity, **_kwargs: (False, None),
    )
    machine._codex_tui_log_trackers = {
        "primary": Tracker(), "stack": Tracker(),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: watch["path"] for sid, watch in machine._watch.items()
    }))

    assert scan.complete is False
    assert scan.incomplete_sids == {primary_sid}
    assert machine._codex_scan_complete_for_sid(scan, primary_sid) is False
    assert machine._codex_scan_complete_for_sid(scan, stack_sid) is True


def test_bound_tui_does_not_depend_on_structured_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    sid = "primary@primary-native"
    machine._watch = {
        sid: {
            "path": "/tmp/primary-rollout.jsonl",
            "engine": "codex",
            "codex_profile_id": "primary",
        },
    }
    client = ProcessIdentity(317, 15)

    def holders(paths, _own, *, shell_snapshot_root=None):
        return HolderScan(
            holders={native_sid: {client} for native_sid in paths},
            complete=True,
            passive_holders={native_sid: set() for native_sid in paths},
            client_proxies={client: 10},
            private_holders={native_sid: set() for native_sid in paths},
            logical_holders={native_sid: set() for native_sid in paths},
        )

    class Tracker:
        def bindings(self, paths, proxies):
            assert proxies == {}
            return {native_sid: set() for native_sid in paths}, False

    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    expected_socket = str(
        machine._codex_profiles.get("primary").home
        / "app-server-control"
        / "app-server-control.sock"
    )
    monkeypatch.setattr(
        machine_module,
        "codex_app_server_client_socket",
        lambda _identity, **_kwargs: (True, expected_socket),
    )
    machine._codex_tui_log_trackers = {
        "primary": Tracker(), "stack": Tracker(),
    }

    scan = asyncio.run(machine._probe_codex_holders({
        sid: machine._watch[sid]["path"],
    }))

    assert scan.complete is True
    assert scan.incomplete_sids == set()
    assert scan.holders[sid] == {client}


def test_cold_profile_does_not_preserve_sibling_stale_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary_path = tmp_path / "primary-rollout.jsonl"
    stack_path = tmp_path / "stack-rollout.jsonl"
    primary_path.write_text("", encoding="utf-8")
    stack_path.write_text("", encoding="utf-8")
    paths = {
        "primary@primary-native": str(primary_path),
        "stack@stack-native": str(stack_path),
    }
    monkeypatch.setattr(
        machine,
        "_codex_rollout_for_wire",
        lambda sid: paths.get(sid),
    )
    for sid in paths:
        machine._watch_session(sid)

    stale_holder = ProcessIdentity(320, 14)
    primary_watch = machine._watch["primary@primary-native"]
    primary_watch.update({
        "external": True,
        "holders": {stale_holder},
        "writers": {stale_holder},
    })
    primary_proxy = ProcessIdentity(321, 15)

    def holders(profile_paths, _own, *, shell_snapshot_root=None):
        return HolderScan(
            holders={sid: set() for sid in profile_paths},
            complete=True,
            passive_holders={sid: set() for sid in profile_paths},
            client_proxies={primary_proxy: 10},
            private_holders={sid: set() for sid in profile_paths},
        )

    class Tracker:
        def bindings(self, paths, proxies):
            return {sid: set() for sid in paths}, True

    async def ignore_history(_sid: str) -> None:
        return None

    machine._codex_daemons["primary"]._ready = SimpleNamespace(
        socket_path="/tmp/primary.sock")
    monkeypatch.setattr(machine_module, "writable_rollout_holders", holders)
    monkeypatch.setattr(
        machine_module,
        "codex_app_server_client_socket",
        lambda identity, **_kwargs: (
            (True, str(Path("/tmp/primary.sock").resolve()))
            if identity == primary_proxy else (True, None)
        ),
    )
    machine._codex_tui_log_trackers = {
        "primary": Tracker(),
        "stack": Tracker(),
    }
    monkeypatch.setattr(machine, "_push_mirrored_history", ignore_history)

    asyncio.run(machine._poll_watches_once())

    assert primary_watch["external"] is False
    assert primary_watch["holders"] == set()
    assert primary_watch["writers"] == set()


def test_profile_fork_recovery_stays_in_parent_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    calls: list[tuple[str, str, str, tuple[str, str]]] = []

    def find_fork(thread_source, parent_sid, cwd, *, roots):
        calls.append((thread_source, parent_sid, cwd, roots))
        return {
            "session_id": "child-native",
            "forked_from_id": parent_sid,
        }

    monkeypatch.setattr(machine_module, "find_rollout_fork", find_fork)

    result = machine._codex_fork_meta_for_wire(
        "fork-marker", "stack@parent-native", "/tmp/project",
    )

    assert calls == [(
        "fork-marker",
        "parent-native",
        "/tmp/project",
        (
            str((tmp_path / "stack" / "sessions").resolve()),
            str((tmp_path / "stack" / "archived_sessions").resolve()),
        ),
    )]
    assert result == {
        "session_id": "stack@child-native",
        "forked_from_id": "stack@parent-native",
    }


def test_hidden_resident_and_fork_catalog_rows_stay_profile_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary = _context(
        "primary@same-native-id", "same-native-id", "primary")
    machine.sessions = {primary.key: primary}
    machine._codex_forks.begin(
        "fork-request", "stack@parent-native", "turn-1", "/repo/stack")
    machine._codex_forks.complete(
        "fork-request", "stack@same-native-id")
    # A corrupt/stale cross-profile journal record must not be repaired into
    # either account even if its child happens to exist in one state DB.
    machine._codex_forks.begin(
        "cross-request", "stack@other-parent", "turn-2", "/repo/stack")
    machine._codex_forks.complete(
        "cross-request", "primary@cross-child")

    calls: list[tuple[str, tuple[str, ...]]] = []

    async def empty_list(_limit, *, codex_home=None):
        assert codex_home is not None
        return []

    def exact_rows(session_ids, *, codex_home=None):
        home = str(Path(codex_home).resolve())
        native_ids = tuple(session_ids)
        calls.append((home, native_ids))
        if Path(home).name == "primary":
            # thread/start returned the durable id, but the exact DB metadata
            # transaction is still lagging. The live resident itself keeps the
            # row visible without borrowing anything from Stack.
            return []
        return [{
            "session_id": native_sid,
            "summary": None,
            "first_prompt": None,
            "cwd": f"/repo/{Path(home).name}",
            "last_modified": "20",
            "git_branch": None,
            "forked_from_id": None,
            "status": None,
            "tag": None,
        } for native_sid in native_ids]

    monkeypatch.setattr(machine_module, "list_codex_sessions", empty_list)
    monkeypatch.setattr(
        machine_module, "codex_exact_catalog_rows", exact_rows)

    rows = asyncio.run(machine._read_all_codex_profile_sessions())

    by_wire = {row["session_id"]: row for row in rows}
    assert set(by_wire) == {
        "primary@same-native-id",
        "stack@same-native-id",
    }
    assert by_wire["primary@same-native-id"]["codex_profile_id"] == "primary"
    assert by_wire["primary@same-native-id"]["forked_from_id"] is None
    assert by_wire["primary@same-native-id"]["cwd"] == (
        "/tmp/cc-remote-profile-test")
    assert by_wire["stack@same-native-id"]["codex_profile_id"] == "stack"
    assert by_wire["stack@same-native-id"]["forked_from_id"] == (
        "stack@parent-native")
    assert by_wire["stack@same-native-id"]["summary"] == "派生会话"
    assert all("cross-child" not in native_ids for _home, native_ids in calls)
    assert set(calls) == {
        (str((tmp_path / "primary").resolve()), ("same-native-id",)),
        (str((tmp_path / "stack").resolve()), ("same-native-id",)),
    }


def test_old_schema_catalog_fallback_is_batched_and_profile_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine.sessions = {}
    for profile_id in ("primary", "stack"):
        profile_home = tmp_path / profile_id
        profile_home.mkdir(parents=True, exist_ok=True)
        (profile_home / "config.toml").write_text(
            'model_provider = "openai"\n', encoding="utf-8")
        request_id = f"fork-{profile_id}"
        machine._codex_forks.begin(
            request_id,
            f"{profile_id}@parent-native",
            f"turn-{profile_id}",
            f"/repo/{profile_id}",
        )
        machine._codex_forks.complete(
            request_id, f"{profile_id}@same-native-id")

    async def empty_list(_limit, *, codex_home=None):
        assert codex_home is not None
        return []

    def uncertain_exact_rows(session_ids, *, codex_home=None):
        assert tuple(session_ids) == ("same-native-id",)
        assert codex_home is not None
        return None

    calls: list[tuple[str, list[tuple[str, object]], float]] = []

    async def batch(requests, cwd=None, *, timeout, codex_home=None):
        assert cwd is None
        assert codex_home is not None
        profile_id = Path(codex_home).name
        calls.append((profile_id, requests, timeout))
        return [{"thread": {
            "id": request[1]["threadId"],
            "cwd": f"/repo/{profile_id}",
            "preview": f"{profile_id} hidden fork",
            "updatedAt": 42,
            "modelProvider": "openai",
        }} for request in requests]

    monkeypatch.setattr(machine_module, "list_codex_sessions", empty_list)
    monkeypatch.setattr(
        machine_module, "codex_exact_catalog_rows", uncertain_exact_rows)
    monkeypatch.setattr(machine_module, "codex_rpc_batch", batch)

    rows = asyncio.run(machine._read_all_codex_profile_sessions())

    by_wire = {row["session_id"]: row for row in rows}
    assert set(by_wire) == {
        "primary@same-native-id", "stack@same-native-id",
    }
    assert by_wire["primary@same-native-id"]["cwd"] == "/repo/primary"
    assert by_wire["stack@same-native-id"]["cwd"] == "/repo/stack"
    assert by_wire["primary@same-native-id"]["forked_from_id"] == (
        "primary@parent-native")
    assert by_wire["stack@same-native-id"]["forked_from_id"] == (
        "stack@parent-native")
    assert {profile_id for profile_id, _requests, _timeout in calls} == {
        "primary", "stack",
    }
    assert all(requests == [(
        "thread/read",
        {"threadId": "same-native-id", "includeTurns": False},
    )] for _profile_id, requests, _timeout in calls)
    assert all(0 < timeout <= machine.CODEX_EXACT_CATALOG_RPC_TIMEOUT_SECONDS
               for _profile_id, _requests, timeout in calls)


def test_exact_catalog_rpc_fallback_is_bounded_and_provider_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary_home = tmp_path / "primary"
    primary_home.mkdir(parents=True, exist_ok=True)
    (primary_home / "config.toml").write_text(
        'model_provider = "openai"\n', encoding="utf-8")
    captured: list[tuple[str, object]] = []
    batch_sizes: list[int] = []
    timeouts: list[float] = []

    async def batch(requests, cwd=None, *, timeout, codex_home=None):
        del cwd
        assert Path(codex_home) == primary_home
        batch_sizes.append(len(requests))
        timeouts.append(timeout)
        captured.extend(requests)
        return [{"thread": {
            "id": params["threadId"],
            "modelProvider": "different-provider",
        }} for _method, params in requests]

    monkeypatch.setattr(machine_module, "codex_rpc_batch", batch)
    rows = asyncio.run(machine._codex_exact_catalog_rpc_rows(
        machine._codex_profile("primary"),
        [f"thread-{index}" for index in range(600)],
    ))

    assert rows == []
    assert len(captured) == machine_module.CODEX_EXACT_CATALOG_MAX_IDS
    assert batch_sizes == [
        machine.CODEX_EXACT_CATALOG_RPC_BATCH_IDS,
    ] * (
        machine_module.CODEX_EXACT_CATALOG_MAX_IDS
        // machine.CODEX_EXACT_CATALOG_RPC_BATCH_IDS
    )
    assert all(0 < timeout <= machine.CODEX_EXACT_CATALOG_RPC_TIMEOUT_SECONDS
               for timeout in timeouts)


def test_resident_catalog_repair_survives_the_global_row_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    resident = _context(
        "primary@resident-old", "resident-old", "primary")
    machine.sessions = {resident.key: resident}

    async def full_list(_limit, *, codex_home=None):
        profile_id = Path(codex_home).name
        return [{
            "session_id": f"listed-{profile_id}-{index}",
            "summary": f"row {index}",
            "last_modified": str(10_000 - index),
        } for index in range(200)]

    def exact_rows(session_ids, *, codex_home=None):
        if Path(codex_home).name != "primary":
            return []
        return [{
            "session_id": "resident-old",
            "summary": "old resident",
            "first_prompt": None,
            "cwd": "/repo/old",
            "last_modified": "1",
            "git_branch": None,
            "forked_from_id": None,
            "status": None,
            "tag": None,
        }] if "resident-old" in session_ids else []

    monkeypatch.setattr(machine_module, "list_codex_sessions", full_list)
    monkeypatch.setattr(
        machine_module, "codex_exact_catalog_rows", exact_rows)

    rows = asyncio.run(machine._read_all_codex_profile_sessions())

    assert len(rows) == machine.CODEX_SESSION_LIST_MAX_ROWS
    assert any(
        row["session_id"] == "primary@resident-old" for row in rows
    )
    assert all(
        machine_module._CODEX_CATALOG_PRIORITY not in row for row in rows
    )


def test_profile_list_failure_still_returns_resident_exact_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    resident = _context(
        "primary@resident-during-error", "resident-during-error", "primary")
    machine.sessions = {resident.key: resident}

    async def list_sessions(_limit, *, codex_home=None):
        if Path(codex_home).name == "primary":
            raise RuntimeError("app-server list unavailable")
        return []

    monkeypatch.setattr(machine_module, "list_codex_sessions", list_sessions)
    monkeypatch.setattr(
        machine_module,
        "codex_exact_catalog_rows",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        machine_module,
        "codex_rpc_batch",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )

    rows, errors = asyncio.run(machine._read_codex_profile_catalog())

    assert [row["session_id"] for row in rows] == [
        "primary@resident-during-error"
    ]
    assert errors == (("primary", "会话列表暂不可用"),)


def test_profile_list_failure_cache_merge_keeps_old_resident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    resident = _context(
        "primary@resident-during-error", "resident-during-error", "primary")
    machine.sessions = {resident.key: resident}
    cached_rows = [
        {
            "session_id": f"primary@cached-primary-{index}",
            "native_session_id": f"cached-primary-{index}",
            "codex_profile_id": "primary",
            "codex_profile_label": "primary",
            "summary": "cached",
            "last_modified": str(10_000 - index),
        }
        for index in range(machine.CODEX_SESSION_LIST_MAX_ROWS)
    ]
    machine._codex_session_list_cache = (
        time.monotonic(), cached_rows, (),
    )

    async def list_sessions(_limit, *, codex_home=None):
        if Path(codex_home).name == "primary":
            raise RuntimeError("app-server list unavailable")
        return []

    def exact_rows(session_ids, *, codex_home=None):
        if (
            Path(codex_home).name != "primary"
            or "resident-during-error" not in session_ids
        ):
            return []
        return [{
            "session_id": "resident-during-error",
            "summary": "old resident",
            "first_prompt": None,
            "cwd": "/repo/old",
            "last_modified": "1",
            "git_branch": None,
            "forked_from_id": None,
            "status": None,
            "tag": None,
        }]

    monkeypatch.setattr(machine_module, "list_codex_sessions", list_sessions)
    monkeypatch.setattr(
        machine_module, "codex_exact_catalog_rows", exact_rows)

    rows = asyncio.run(machine._refresh_codex_session_catalog())

    assert len(rows) == machine.CODEX_SESSION_LIST_MAX_ROWS
    assert any(
        row["session_id"] == "primary@resident-during-error"
        for row in rows
    )
    assert machine._codex_session_profile_errors == (
        ("primary", "会话列表暂不可用"),
    )
    assert all(
        machine_module._CODEX_CATALOG_PRIORITY not in row for row in rows
    )


def test_profile_history_rpc_uses_native_uuid_and_matching_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CodexProfileRegistry.from_json(
        _profiles(tmp_path / "primary", tmp_path / "stack"),
    )
    calls: list[tuple[str, dict, str | None]] = []

    async def rpc(method, params, cwd=None, codex_home=None):
        calls.append((method, dict(params or {}), codex_home))
        return {}

    monkeypatch.setattr(machine_module, "codex_rpc", rpc)
    history = _CodexHistoryProfiles(
        registry,
        explicit=True,
        tool_result_max=1024,
        recover_user=None,
        recover_users=None,
    )

    async def run() -> None:
        await history._readers["primary"]._call(
            "thread/turns/list", {"threadId": "primary@same-native-id"})
        await history._readers["stack"]._call(
            "thread/turns/list", {"threadId": "stack@same-native-id"})
        with pytest.raises(ValueError, match="crossed account boundary"):
            await history._readers["primary"]._call(
                "thread/turns/list", {"threadId": "stack@same-native-id"})

    asyncio.run(run())
    assert calls == [
        (
            "thread/turns/list",
            {"threadId": "same-native-id"},
            str((tmp_path / "primary").resolve()),
        ),
        (
            "thread/turns/list",
            {"threadId": "same-native-id"},
            str((tmp_path / "stack").resolve()),
        ),
    ]


def test_profile_history_rollout_fallback_isolated_for_same_native_uuid(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    primary_sid = "primary@same-native-id"
    stack_sid = "stack@same-native-id"

    primary_revision = machine._activate_codex_rollout_history(primary_sid)

    assert machine._codex_rollout_history_active(primary_sid) is True
    assert machine._codex_rollout_history_active(stack_sid) is False
    assert primary_revision == machine._history_revision(primary_sid)
    assert machine._history_revision(stack_sid) != primary_revision


def test_profile_catalog_failures_preserve_the_public_account_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, transport = _machine(tmp_path)
    primary_home = str((tmp_path / "primary").resolve())
    stack_home = str((tmp_path / "stack").resolve())
    failing: set[str] = set()
    empty: set[str] = set()

    async def list_sessions(_limit, *, codex_home=None):
        if codex_home in failing:
            raise RuntimeError("profile unavailable")
        if codex_home in empty:
            return []
        return [{
            "session_id": "same-native-id",
            "summary": f"from {codex_home}",
            "last_modified": "2026-08-08T12:00:00Z",
        }]

    monkeypatch.setattr(machine_module, "list_codex_sessions", list_sessions)

    rows = asyncio.run(machine._refresh_codex_session_catalog())
    assert {
        (row["session_id"], row["codex_profile_id"]) for row in rows
    } == {
        ("primary@same-native-id", "primary"),
        ("stack@same-native-id", "stack"),
    }

    failing.add(stack_home)
    rows = asyncio.run(machine._refresh_codex_session_catalog())
    # The healthy profile is fresh while the failed profile keeps its last
    # known projection instead of disappearing from the sidebar.
    assert {
        (row["session_id"], row["codex_profile_id"]) for row in rows
    } == {
        ("primary@same-native-id", "primary"),
        ("stack@same-native-id", "stack"),
    }
    assert machine._codex_session_profile_errors == (
        ("stack", "会话列表暂不可用"),
    )

    empty.add(primary_home)
    rows = asyncio.run(machine._refresh_codex_session_catalog())
    assert {row["session_id"] for row in rows} == {"stack@same-native-id"}
    empty.clear()
    rows = asyncio.run(machine._refresh_codex_session_catalog())
    assert {row["session_id"] for row in rows} == {
        "primary@same-native-id", "stack@same-native-id",
    }

    failing.add(primary_home)
    rows = asyncio.run(machine._refresh_codex_session_catalog())
    assert {row["session_id"] for row in rows} == {
        "primary@same-native-id", "stack@same-native-id",
    }
    assert dict(machine._codex_session_profile_errors) == {
        "primary": "会话列表暂不可用",
        "stack": "会话列表暂不可用",
    }

    transport.sent.clear()
    result = asyncio.run(machine._list_codex_sessions(SimpleNamespace(
        engine="codex", space="code", cmd_id="all-failed",
        client_id="client-1",
    )))
    assert isinstance(result, SessionList)
    assert {session.session_id for session in result.sessions} == {
        "primary@same-native-id", "stack@same-native-id",
    }
    assert {profile.id for profile in result.codex_profiles} == {
        "primary", "stack",
    }
    assert all(profile.error for profile in result.codex_profiles)
    assert transport.sent == [result]


def test_profile_catalog_all_failure_without_cache_returns_empty_registry_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)

    async def unavailable(_limit, *, codex_home=None):
        raise RuntimeError(f"profile unavailable: {codex_home}")

    monkeypatch.setattr(machine_module, "list_codex_sessions", unavailable)

    rows = asyncio.run(machine._refresh_codex_session_catalog())

    assert rows == []
    assert {profile_id for profile_id, _ in (
        machine._codex_session_profile_errors
    )} == {"primary", "stack"}


def test_stale_profile_catalog_generation_cannot_replace_current_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        machine, _transport = _machine(tmp_path)
        old_started = asyncio.Event()
        release_old = asyncio.Event()
        calls = 0

        async def read_catalog():
            nonlocal calls
            calls += 1
            if calls == 1:
                old_started.set()
                await release_old.wait()
                return ([{
                    "session_id": "stack@stale",
                    "native_session_id": "stale",
                    "codex_profile_id": "stack",
                }], (("primary", "旧代际失败"),))
            return ([{
                "session_id": "primary@fresh",
                "native_session_id": "fresh",
                "codex_profile_id": "primary",
            }], (("stack", "当前代际失败"),))

        monkeypatch.setattr(
            machine, "_read_codex_profile_catalog", read_catalog)
        stale = asyncio.create_task(
            machine._refresh_codex_session_catalog())
        await old_started.wait()
        machine._invalidate_codex_session_catalog()
        fresh = await machine._refresh_codex_session_catalog()
        release_old.set()
        reconciled = await stale

        assert [row["session_id"] for row in fresh] == ["primary@fresh"]
        assert [row["session_id"] for row in reconciled] == ["primary@fresh"]
        assert machine._codex_session_profile_errors == (
            ("stack", "当前代际失败"),
        )
        assert machine._codex_session_list_cache is not None
        assert machine._codex_session_list_cache[2] == (
            ("stack", "当前代际失败"),
        )

    asyncio.run(run())


def test_sidebar_watch_budget_is_fair_across_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine.WATCH_MAX = 8
    watched = []
    monkeypatch.setattr(
        machine,
        "_watch_session",
        lambda sid, sidebar=False: watched.append((sid, sidebar)),
    )
    rows = [
        {
            "session_id": f"primary@native-{index}",
            "codex_profile_id": "primary",
        }
        for index in range(12)
    ] + [{
        "session_id": "stack@native-only",
        "codex_profile_id": "stack",
    }]

    machine._prime_codex_sidebar_watches(rows)

    assert watched == [
        ("primary@native-0", True),
        ("stack@native-only", True),
        ("primary@native-1", True),
        ("primary@native-2", True),
    ]


def test_profile_control_and_turn_lease_keys_do_not_collide(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    assert machine._codex_controls is not None
    machine._codex_controls.update(
        "primary@same-native-id",
        approval_policy="never",
        permission_profile=":primary",
        web_search="cached",
    )
    machine._codex_controls.update(
        "stack@same-native-id",
        approval_policy="on-request",
        permission_profile=":stack",
        web_search="live",
    )
    assert machine._codex_controls.get(
        "primary@same-native-id").permission_profile == ":primary"
    assert machine._codex_controls.get(
        "stack@same-native-id").permission_profile == ":stack"

    machine._codex_turn_leases.claim(
        "primary@same-native-id", "turn-primary", "message-primary")
    machine._codex_turn_leases.claim(
        "stack@same-native-id", "turn-stack", "message-stack")
    assert machine._codex_turn_leases.get(
        "primary@same-native-id").turn_id == "turn-primary"
    assert machine._codex_turn_leases.get(
        "stack@same-native-id").turn_id == "turn-stack"


def test_enabling_multiple_profiles_migrates_default_profile_preferences(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    controls = CodexControlStore(state)
    controls.update(
        "legacy-native-id",
        approval_policy="never",
        permission_profile=":workspace",
        web_search="cached",
    )
    leases = CodexTurnLeaseStore(state)
    leases.claim("legacy-native-id", "turn-1", "message-1")
    pins = SessionPinStore(state)
    pins.set_pinned("codex", "legacy-native-id", True)

    machine, _transport = _machine(tmp_path)
    assert machine._codex_controls is not None
    assert machine._codex_controls.get(
        "primary@legacy-native-id").permission_profile == ":workspace"
    assert machine._codex_controls.get(
        "legacy-native-id").permission_profile is None
    assert machine._codex_turn_leases.get(
        "primary@legacy-native-id").turn_id == "turn-1"
    assert machine._codex_turn_leases.get("legacy-native-id") is None
    assert machine._session_pins is not None
    assert machine._session_pins.ids("codex") == frozenset({
        "primary@legacy-native-id",
    })


def test_enabling_profiles_keeps_legacy_state_with_original_home_when_default_changes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    original_home = tmp_path / "original"
    replacement_home = tmp_path / "replacement"
    single_cfg = WrapperConfig()
    single_cfg.state_dir = state
    single_cfg.claude_work_root = tmp_path / "work" / "claude"
    single_cfg.codex_work_root = tmp_path / "work" / "codex"
    single_cfg.codex_profiles_json = json.dumps({
        "main": {
            "label": "Main",
            "home": str(original_home),
            "default": True,
        },
    })
    single = WrapperMachine(single_cfg, _StubTransport())
    assert single._codex_controls is not None
    single._codex_controls.update(
        "legacy-native-id",
        approval_policy="never",
        permission_profile=":original",
        web_search="cached",
    )
    single._codex_turn_leases.claim(
        "legacy-native-id", "turn-original", "message-original")

    multi_cfg = WrapperConfig()
    multi_cfg.state_dir = state
    multi_cfg.claude_work_root = tmp_path / "work" / "claude"
    multi_cfg.codex_work_root = tmp_path / "work" / "codex"
    multi_cfg.codex_profiles_json = json.dumps({
        "replacement": {
            "label": "Replacement",
            "home": str(replacement_home),
            "default": True,
        },
        "main": {
            "label": "Main",
            "home": str(original_home),
        },
    })
    multi = WrapperMachine(multi_cfg, _StubTransport())

    assert multi._codex_controls is not None
    assert multi._codex_controls.get(
        "main@legacy-native-id").permission_profile == ":original"
    assert multi._codex_controls.get(
        "replacement@legacy-native-id").permission_profile is None
    assert multi._codex_turn_leases.get(
        "main@legacy-native-id").turn_id == "turn-original"
    assert multi._codex_turn_leases.get(
        "replacement@legacy-native-id") is None


def test_profile_transition_migrates_alias_fork_and_checkpoint_recovery_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    home = tmp_path / "main"
    single_cfg = WrapperConfig()
    single_cfg.state_dir = state
    single_cfg.claude_work_root = tmp_path / "work" / "claude"
    single_cfg.codex_work_root = tmp_path / "work" / "codex"
    single_cfg.codex_profiles_json = json.dumps({
        "main": {
            "label": "Main",
            "home": str(home),
            "default": True,
        },
    })
    WrapperMachine(single_cfg, _StubTransport())

    alias_path = state / "session-aliases.json"
    alias_path.write_text(json.dumps({
        "tmp-" + "a" * 32: {
            "session_id": "parent-native",
            "cwd": str(tmp_path),
            "created_at": time.time(),
        },
    }), encoding="utf-8")
    forks = CodexForkJournal(state)
    forks.begin(
        "fork-request", "parent-native", "turn-1", str(tmp_path))
    forks.complete("fork-request", "child-native")

    repository_bucket = state / "codex-checkpoints" / "repository"
    old_key = hashlib.sha256(b"parent-native").hexdigest()[:24]
    checkpoint = repository_bucket / old_key
    checkpoint.mkdir(parents=True)
    (checkpoint / "manifest.json").write_text(json.dumps({
        "version": 2,
        "repository_root": str(tmp_path / "repo"),
        "session_id": "parent-native",
        "active": None,
        "restore": None,
        "turns": [],
    }), encoding="utf-8")

    multi_cfg = WrapperConfig()
    multi_cfg.state_dir = state
    multi_cfg.claude_work_root = tmp_path / "work" / "claude"
    multi_cfg.codex_work_root = tmp_path / "work" / "codex"
    multi_cfg.codex_profiles_json = json.dumps({
        "main": {
            "label": "Main",
            "home": str(home),
            "default": True,
        },
        "stack": {
            "label": "Stack",
            "home": str(tmp_path / "stack"),
        },
    })
    migrated = WrapperMachine(multi_cfg, _StubTransport())

    assert migrated._resolve_session_alias(
        "tmp-" + "a" * 32) == "main@parent-native"
    fork = migrated._codex_forks.get("fork-request")
    assert fork is not None
    assert fork["parent_session_id"] == "main@parent-native"
    assert fork["session_id"] == "main@child-native"
    new_key = hashlib.sha256(b"main@parent-native").hexdigest()[:24]
    migrated_manifest = json.loads(
        (repository_bucket / new_key / "manifest.json").read_text(
            encoding="utf-8"))
    assert migrated_manifest["session_id"] == "main@parent-native"
    assert not checkpoint.exists()


def test_profile_remap_retries_do_not_apply_completed_store_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine._codex_turn_leases.claim(
        "primary@same-native-id", "turn-primary", "message-primary")
    machine._codex_turn_leases.claim(
        "stack@same-native-id", "turn-stack", "message-stack")
    assert machine._codex_controls is not None
    machine._codex_controls.update(
        "primary@same-native-id", approval_policy=None,
        permission_profile=":primary", web_search=None)
    machine._codex_controls.update(
        "stack@same-native-id", approval_policy=None,
        permission_profile=":stack", web_search=None)

    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "primary": {
            "label": "Former Stack",
            "home": str(tmp_path / "stack"),
            "default": True,
        },
        "stack": {
            "label": "Former Primary",
            "home": str(tmp_path / "primary"),
        },
    })

    original = CodexControlStore.migrate_profile_sessions

    def fail_controls(self, transform, *, profile_revision):
        raise RuntimeError("simulated controls persistence failure")

    monkeypatch.setattr(
        CodexControlStore, "migrate_profile_sessions", fail_controls)
    failed = WrapperMachine(cfg, _StubTransport())
    assert not failed._codex_profile_migration_ok
    assert not failed._codex_work_profile_migration_ok
    assert (tmp_path / "state" / "codex-profile-transition.json").exists()
    drifted, _transport = _machine(tmp_path)
    assert not drifted._codex_profile_migration_ok
    assert not drifted._codex_work_profile_migration_ok
    with pytest.raises(RuntimeError, match="migration is incomplete"):
        drifted._codex_profile()
    monkeypatch.setattr(
        CodexControlStore, "migrate_profile_sessions", original)

    recovered = WrapperMachine(cfg, _StubTransport())
    assert recovered._codex_profile_migration_ok
    assert recovered._codex_work_profile_migration_ok
    assert recovered._codex_turn_leases.get(
        "stack@same-native-id").turn_id == "turn-primary"
    assert recovered._codex_turn_leases.get(
        "primary@same-native-id").turn_id == "turn-stack"
    assert recovered._codex_controls is not None
    assert recovered._codex_controls.get(
        "stack@same-native-id").permission_profile == ":primary"
    assert recovered._codex_controls.get(
        "primary@same-native-id").permission_profile == ":stack"
    assert not (
        tmp_path / "state" / "codex-profile-transition.json").exists()


@pytest.mark.parametrize(
    ("store_type", "method_name", "attribute"),
    [
        (SessionPlanStore, "migrate_profile_sessions", "_session_plans"),
        (
            SessionPresentationStore,
            "migrate_codex_profile_sessions",
            "_session_presentation",
        ),
    ],
)
def test_optional_presentation_migration_does_not_disable_codex_or_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_type,
    method_name: str,
    attribute: str,
) -> None:
    machine, _transport = _machine(tmp_path)
    state = tmp_path / "state"
    previous = json.loads(
        (state / "codex-profile-topology.json").read_text(encoding="utf-8")
    )
    renamed_profiles = {}
    for profile in previous["profiles"]:
        target = "renamed" if profile["id"] == "primary" else profile["id"]
        renamed_profiles[target] = {
            "label": target,
            "home": profile["home"],
            "default": profile["id"] == previous["default_id"],
        }

    def fail_optional(self, transform, *, profile_revision):
        raise RuntimeError("simulated optional cache failure")

    monkeypatch.setattr(store_type, method_name, fail_optional)
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps(renamed_profiles)

    migrated = WrapperMachine(cfg, _StubTransport())

    assert migrated._codex_profile_migration_ok
    assert migrated._codex_work_profile_migration_ok
    assert not migrated._codex_presentation_profile_migration_ok
    assert getattr(migrated, attribute) is None
    assert migrated._codex_profile().id == "renamed"
    assert (state / "codex-profile-transition.json").exists()
    # The pending transition is retained so the optional projection can catch
    # up after restart without revoking access to the already-migrated engines.
    assert machine._codex_profiles.default.id == "primary"


@pytest.mark.parametrize(
    ("filename", "attribute"),
    [
        ("session-plans.json", "_session_plans"),
        ("session-presentation.json", "_session_presentation"),
    ],
)
def test_corrupt_rebuildable_projection_does_not_pin_profile_transition(
    tmp_path: Path,
    filename: str,
    attribute: str,
) -> None:
    _machine(tmp_path)
    state = tmp_path / "state"
    previous = json.loads(
        (state / "codex-profile-topology.json").read_text(encoding="utf-8")
    )
    renamed_profiles = {}
    for profile in previous["profiles"]:
        target = "renamed" if profile["id"] == "primary" else profile["id"]
        renamed_profiles[target] = {
            "label": target,
            "home": profile["home"],
            "default": profile["id"] == previous["default_id"],
        }

    projection = state / filename
    projection.write_text("{not-json", encoding="utf-8")
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps(renamed_profiles)

    migrated = WrapperMachine(cfg, _StubTransport())

    assert migrated._codex_profile_migration_ok
    assert migrated._codex_work_profile_migration_ok
    assert migrated._codex_presentation_profile_migration_ok
    assert getattr(migrated, attribute) is not None
    assert not (state / "codex-profile-transition.json").exists()
    quarantined = list(state.glob(f"{filename}.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not-json"

    next_profiles = dict(renamed_profiles)
    next_profiles["third"] = {
        "label": "third",
        "home": str(tmp_path / "third"),
    }
    cfg.codex_profiles_json = json.dumps(next_profiles)
    advanced = WrapperMachine(cfg, _StubTransport())

    assert advanced._codex_profile_migration_ok
    assert advanced._codex_work_profile_migration_ok
    assert advanced._codex_presentation_profile_migration_ok
    assert not (state / "codex-profile-transition.json").exists()


def test_work_profile_migration_failure_does_not_disable_codex_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machine(tmp_path)
    state = tmp_path / "state"
    previous = json.loads(
        (state / "codex-profile-topology.json").read_text(encoding="utf-8")
    )
    renamed_profiles = {}
    for profile in previous["profiles"]:
        target = "renamed" if profile["id"] == "primary" else profile["id"]
        renamed_profiles[target] = {
            "label": target,
            "home": profile["home"],
            "default": profile["id"] == previous["default_id"],
        }

    def fail_work(*_args, **_kwargs):
        raise RuntimeError("simulated Work ownership failure")

    monkeypatch.setattr(
        WorkRegistry, "migrate_codex_profiles", fail_work)
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps(renamed_profiles)

    migrated = WrapperMachine(cfg, _StubTransport())

    assert migrated._codex_profile_migration_ok
    assert not migrated._codex_work_profile_migration_ok
    assert migrated._codex_profile().id == "renamed"
    assert (state / "codex-profile-transition.json").exists()


def test_v1_topology_is_upgraded_before_old_checkpoint_is_opened(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    home = (tmp_path / "main").resolve()
    state.mkdir()
    (state / "codex-profile-topology.json").write_text(json.dumps({
        "version": 1,
        "profiles": [{"id": "main", "home": str(home)}],
    }), encoding="utf-8")
    repository_bucket = state / "codex-checkpoints" / "repository"
    session_key = hashlib.sha256(b"native-id").hexdigest()[:24]
    checkpoint = repository_bucket / session_key
    checkpoint.mkdir(parents=True)
    (checkpoint / "manifest.json").write_text(json.dumps({
        "version": 2,
        "repository_root": str(tmp_path / "repo"),
        "session_id": "native-id",
        "active": None,
        "restore": None,
        "turns": [],
    }), encoding="utf-8")
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "main": {
            "label": "Main",
            "home": str(home),
            "default": True,
        },
    })

    machine = WrapperMachine(cfg, _StubTransport())

    manifest = json.loads(
        (checkpoint / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile_revision"] == machine._codex_profile_revision
    topology = json.loads(
        (state / "codex-profile-topology.json").read_text(encoding="utf-8"))
    assert topology["version"] == 2


def test_disabling_and_reenabling_profiles_preserves_account_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    controls = CodexControlStore(state)
    controls.update(
        "primary@same-native-id",
        approval_policy="never",
        permission_profile=":primary",
        web_search="cached",
    )
    controls.update(
        "stack@same-native-id",
        approval_policy="on-request",
        permission_profile=":stack",
        web_search="live",
    )
    leases = CodexTurnLeaseStore(state)
    leases.claim("primary@same-native-id", "turn-primary", "message-primary")
    leases.claim("stack@same-native-id", "turn-stack", "message-stack")
    pins = SessionPinStore(state)
    pins.set_pinned("codex", "primary@same-native-id", True)
    pins.set_pinned("codex", "stack@same-native-id", True)

    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "primary": {
            "label": "主账号",
            "home": str(tmp_path / "primary"),
            "default": True,
        },
    })
    single = WrapperMachine(cfg, _StubTransport())
    assert single._codex_controls is not None
    assert single._codex_controls.get(
        "same-native-id").permission_profile == ":primary"
    assert single._codex_controls.get(
        "stack@same-native-id").permission_profile == ":stack"
    assert single._codex_turn_leases.get(
        "same-native-id").turn_id == "turn-primary"
    assert single._codex_turn_leases.get(
        "stack@same-native-id").turn_id == "turn-stack"
    assert single._session_pins is not None
    assert single._session_pins.ids("codex") == frozenset({
        "same-native-id", "stack@same-native-id",
    })

    restored, _transport = _machine(tmp_path)
    assert restored._codex_controls is not None
    assert restored._codex_controls.get(
        "primary@same-native-id").permission_profile == ":primary"
    assert restored._codex_controls.get(
        "stack@same-native-id").permission_profile == ":stack"
    assert restored._codex_turn_leases.get(
        "primary@same-native-id").turn_id == "turn-primary"
    assert restored._codex_turn_leases.get(
        "stack@same-native-id").turn_id == "turn-stack"


def test_inactive_profile_turn_lease_is_preserved_during_single_profile_startup(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    leases = CodexTurnLeaseStore(state)
    leases.claim("stack@native-id", "turn-stack", "message-stack")
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "primary": {
            "label": "主账号",
            "home": str(tmp_path / "primary"),
            "default": True,
        },
    })
    machine = WrapperMachine(cfg, _StubTransport())

    asyncio.run(machine._restore_codex_owned_turns())

    assert machine._codex_turn_leases.get(
        "stack@native-id").turn_id == "turn-stack"


def test_same_home_profile_id_rename_migrates_persisted_identity(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    primary_home = tmp_path / "primary"
    stack_home = tmp_path / "stack"
    initial_cfg = WrapperConfig()
    initial_cfg.state_dir = state
    initial_cfg.codex_work_root = tmp_path / "work"
    initial_cfg.codex_profiles_json = _profiles(primary_home, stack_home)
    initial = WrapperMachine(initial_cfg, _StubTransport())
    assert initial._codex_controls is not None
    initial._codex_controls.update(
        "stack@same-native-id",
        approval_policy="on-request",
        permission_profile=":stack",
        web_search="live",
    )
    initial._codex_turn_leases.claim(
        "stack@same-native-id", "turn-stack", "message-stack")
    assert initial._session_pins is not None
    initial._session_pins.set_pinned(
        "codex", "stack@same-native-id", True)
    page_scope = PageScope(engine="codex", space="code", sid="stack@same-native-id")
    page = PageRef(machine_id="device", site_id="demo", entry="/index.html", label="Demo")
    initial.viewer_pages.store.associate(page_scope, [page], automatic=False)
    work = initial._work.for_engine("codex")
    record = work.create_session(codex_profile_id="stack")
    work.bind_session(
        record.work_id, "same-native-id", codex_profile_id="stack")

    renamed_cfg = WrapperConfig()
    renamed_cfg.state_dir = state
    renamed_cfg.codex_work_root = tmp_path / "work"
    renamed_cfg.codex_profiles_json = json.dumps({
        "primary": {
            "label": "主账号",
            "home": str(primary_home),
            "default": True,
        },
        "luna": {
            "label": "Luna",
            "home": str(stack_home),
        },
    })
    renamed = WrapperMachine(renamed_cfg, _StubTransport())

    assert renamed._codex_controls is not None
    assert renamed._codex_controls.get(
        "luna@same-native-id").permission_profile == ":stack"
    assert renamed._codex_turn_leases.get(
        "luna@same-native-id").turn_id == "turn-stack"
    assert renamed._session_pins is not None
    assert "luna@same-native-id" in renamed._session_pins.ids("codex")
    assert renamed.viewer_pages.store.list(page_scope) == []
    assert renamed.viewer_pages.store.list(
        page_scope.model_copy(update={"sid": "luna@same-native-id"})) == [page.public()]
    migrated_work = renamed._work.for_engine("codex").get_by_session(
        "same-native-id", codex_profile_id="luna")
    assert migrated_work is not None
    assert migrated_work.work_id == record.work_id


def test_profile_home_replacement_fails_closed_before_state_is_reowned(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    old_home = tmp_path / "old-home"
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.codex_work_root = tmp_path / "work"
    cfg.codex_profiles_json = json.dumps({
        "a": {
            "label": "A",
            "home": str(old_home),
            "default": True,
        },
    })
    original = WrapperMachine(cfg, _StubTransport())
    assert original._codex_controls is not None
    original._codex_controls.update(
        "native-id", approval_policy="on-request",
        permission_profile=":old-account", web_search=None)
    original._codex_turn_leases.claim(
        "native-id", "turn-old", "message-old")
    assert original._session_pins is not None
    original._session_pins.set_pinned("codex", "native-id", True)
    work = original._work.for_engine("codex")
    record = work.create_session(codex_profile_id="a")
    work.bind_session(record.work_id, "native-id", codex_profile_id="a")

    replacement_cfg = WrapperConfig()
    replacement_cfg.state_dir = state
    replacement_cfg.codex_work_root = tmp_path / "work"
    replacement_cfg.codex_profiles_json = json.dumps({
        "a": {
            "label": "Replacement",
            "home": str(tmp_path / "new-home"),
            "default": True,
        },
    })
    replacement = WrapperMachine(replacement_cfg, _StubTransport())

    assert not replacement._codex_profile_migration_ok
    assert not replacement._codex_work_profile_migration_ok
    with pytest.raises(RuntimeError, match="migration is incomplete"):
        replacement._codex_profile("a")
    assert replacement._codex_controls is not None
    assert replacement._codex_controls.get(
        "native-id").permission_profile == ":old-account"
    assert replacement._codex_turn_leases.get(
        "native-id").turn_id == "turn-old"
    assert replacement._session_pins is not None
    assert replacement._session_pins.ids("codex") == frozenset({"native-id"})
    assert replacement._work.for_engine("codex").get_by_session(
        "native-id", codex_profile_id="a").work_id == record.work_id


def test_profile_id_swap_keeps_same_native_uuid_state_with_its_home(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    assert machine._codex_controls is not None
    machine._codex_controls.update(
        "primary@same-native-id", approval_policy=None,
        permission_profile=":primary", web_search=None)
    machine._codex_controls.update(
        "stack@same-native-id", approval_policy=None,
        permission_profile=":stack", web_search=None)
    machine._codex_turn_leases.claim(
        "primary@same-native-id", "turn-primary", "message-primary")
    machine._codex_turn_leases.claim(
        "stack@same-native-id", "turn-stack", "message-stack")
    assert machine._session_pins is not None
    machine._session_pins.set_pinned(
        "codex", "primary@same-native-id", True)
    work = machine._work.for_engine("codex")
    primary_work = work.create_session(codex_profile_id="primary")
    stack_work = work.create_session(codex_profile_id="stack")
    work.bind_session(
        primary_work.work_id, "same-native-id", codex_profile_id="primary")
    work.bind_session(
        stack_work.work_id, "same-native-id", codex_profile_id="stack")

    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "primary": {
            "label": "原 Stack",
            "home": str(tmp_path / "stack"),
            "default": True,
        },
        "stack": {
            "label": "原主账号",
            "home": str(tmp_path / "primary"),
        },
    })
    swapped = WrapperMachine(cfg, _StubTransport())

    assert swapped._codex_controls is not None
    assert swapped._codex_controls.get(
        "stack@same-native-id").permission_profile == ":primary"
    assert swapped._codex_controls.get(
        "primary@same-native-id").permission_profile == ":stack"
    assert swapped._codex_turn_leases.get(
        "stack@same-native-id").turn_id == "turn-primary"
    assert swapped._codex_turn_leases.get(
        "primary@same-native-id").turn_id == "turn-stack"
    assert swapped._session_pins is not None
    assert swapped._session_pins.ids("codex") == frozenset({
        "stack@same-native-id",
    })
    swapped_work = swapped._work.for_engine("codex")
    assert swapped_work.get_by_session(
        "same-native-id", codex_profile_id="stack").work_id == (
            primary_work.work_id)
    assert swapped_work.get_by_session(
        "same-native-id", codex_profile_id="primary").work_id == (
            stack_work.work_id)


def test_profile_id_swap_moves_recovery_journals_without_path_collision(
    tmp_path: Path,
) -> None:
    machine, _transport = _machine(tmp_path)
    state = tmp_path / "state"
    now = time.time()
    (state / "session-aliases.json").write_text(json.dumps({
        "tmp-" + "a" * 32: {
            "session_id": "primary@same-native-id",
            "cwd": str(tmp_path),
            "created_at": now,
        },
        "tmp-" + "b" * 32: {
            "session_id": "stack@same-native-id",
            "cwd": str(tmp_path),
            "created_at": now,
        },
    }), encoding="utf-8")
    forks = CodexForkJournal(state)
    forks.begin(
        "fork-primary", "primary@same-native-id", "turn-primary",
        str(tmp_path))
    forks.complete("fork-primary", "primary@child-native-id")
    forks.begin(
        "fork-stack", "stack@same-native-id", "turn-stack", str(tmp_path))
    forks.complete("fork-stack", "stack@child-native-id")

    repository_bucket = state / "codex-checkpoints" / "repository"
    for session_id in (
        "primary@same-native-id", "stack@same-native-id",
    ):
        session_key = hashlib.sha256(
            session_id.encode("utf-8")).hexdigest()[:24]
        checkpoint = repository_bucket / session_key
        checkpoint.mkdir(parents=True)
        (checkpoint / "manifest.json").write_text(json.dumps({
            "version": 2,
            "repository_root": str(tmp_path / "repo"),
            "session_id": session_id,
            "source_owner": session_id,
            "profile_revision": machine._codex_profile_revision,
            "active": None,
            "restore": None,
            "turns": [],
        }), encoding="utf-8")

    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "primary": {
            "label": "Former Stack",
            "home": str(tmp_path / "stack"),
            "default": True,
        },
        "stack": {
            "label": "Former Primary",
            "home": str(tmp_path / "primary"),
        },
    })
    swapped = WrapperMachine(cfg, _StubTransport())

    assert swapped._resolve_session_alias(
        "tmp-" + "a" * 32) == "stack@same-native-id"
    assert swapped._resolve_session_alias(
        "tmp-" + "b" * 32) == "primary@same-native-id"
    assert swapped._codex_forks.get(
        "fork-primary")["session_id"] == "stack@child-native-id"
    assert swapped._codex_forks.get(
        "fork-stack")["session_id"] == "primary@child-native-id"
    for session_id, source_owner in (
        ("primary@same-native-id", "stack@same-native-id"),
        ("stack@same-native-id", "primary@same-native-id"),
    ):
        session_key = hashlib.sha256(
            session_id.encode("utf-8")).hexdigest()[:24]
        manifest = json.loads(
            (repository_bucket / session_key / "manifest.json").read_text(
                encoding="utf-8"))
        assert manifest["session_id"] == session_id
        assert manifest["source_owner"] == source_owner


def test_profile_rename_displaces_removed_target_without_crossing_accounts(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    work_root = tmp_path / "work"
    initial_cfg = WrapperConfig()
    initial_cfg.state_dir = state
    initial_cfg.claude_work_root = work_root / "claude"
    initial_cfg.codex_work_root = work_root / "codex"
    initial_cfg.codex_profiles_json = json.dumps({
        "a": {
            "label": "A",
            "home": str(tmp_path / "home-a"),
            "default": True,
        },
        "b": {
            "label": "B",
            "home": str(tmp_path / "home-b"),
        },
    })
    initial = WrapperMachine(initial_cfg, _StubTransport())
    assert initial._codex_controls is not None
    initial._codex_controls.update(
        "a@same-native-id", approval_policy=None,
        permission_profile=":a", web_search=None)
    initial._codex_controls.update(
        "b@same-native-id", approval_policy=None,
        permission_profile=":b", web_search=None)
    initial._codex_turn_leases.claim(
        "a@same-native-id", "turn-a", "message-a")
    initial._codex_turn_leases.claim(
        "b@same-native-id", "turn-b", "message-b")
    assert initial._session_pins is not None
    initial._session_pins.set_pinned(
        "codex", "a@same-native-id", True)
    work = initial._work.for_engine("codex")
    work_a = work.create_session(codex_profile_id="a")
    work_b = work.create_session(codex_profile_id="b")
    work.bind_session(
        work_a.work_id, "same-native-id", codex_profile_id="a")
    work.bind_session(
        work_b.work_id, "same-native-id", codex_profile_id="b")
    work.create_schedule(
        "A schedule", "from A", time.time() + 3600,
        codex_profile_id="a",
    )
    work.create_schedule(
        "B schedule", "from B", time.time() + 3600,
        codex_profile_id="b",
    )

    renamed_cfg = WrapperConfig()
    renamed_cfg.state_dir = state
    renamed_cfg.claude_work_root = work_root / "claude"
    renamed_cfg.codex_work_root = work_root / "codex"
    renamed_cfg.codex_profiles_json = json.dumps({
        "b": {
            "label": "A renamed",
            "home": str(tmp_path / "home-a"),
            "default": True,
        },
    })
    renamed = WrapperMachine(renamed_cfg, _StubTransport())

    assert renamed._codex_profile_migration_ok
    assert renamed._codex_controls is not None
    assert renamed._codex_controls.get(
        "same-native-id").permission_profile == ":a"
    assert renamed._codex_controls.get(
        "a@same-native-id").permission_profile == ":b"
    assert renamed._codex_turn_leases.get(
        "same-native-id").turn_id == "turn-a"
    assert renamed._codex_turn_leases.get(
        "a@same-native-id").turn_id == "turn-b"
    assert renamed._session_pins is not None
    assert renamed._session_pins.ids("codex") == frozenset({
        "same-native-id",
    })
    renamed_work = renamed._work.for_engine("codex")
    assert renamed_work.get_by_session(
        "same-native-id", codex_profile_id="b").work_id == work_a.work_id
    assert renamed_work.get_by_session(
        "same-native-id", codex_profile_id="a").work_id == work_b.work_id
    assert {
        row["title"]: row["codex_profile_id"]
        for row in renamed_work.dashboard()["schedules"]
    } == {
        "A schedule": "b",
        "B schedule": "a",
    }


def test_missing_profile_file_is_a_controlled_single_profile_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    primary_home = tmp_path / "primary"
    initial_cfg = WrapperConfig()
    initial_cfg.state_dir = state
    initial_cfg.claude_work_root = tmp_path / "work" / "claude"
    initial_cfg.codex_work_root = tmp_path / "work" / "codex"
    initial_cfg.codex_profiles_json = json.dumps({
        "main": {
            "label": "主账号",
            "home": str(primary_home),
            "default": True,
        },
        "stack": {
            "label": "Stack",
            "home": str(tmp_path / "stack"),
        },
    })
    initial = WrapperMachine(initial_cfg, _StubTransport())
    assert initial._codex_controls is not None
    initial._codex_controls.update(
        "main@native-id", approval_policy=None,
        permission_profile=":main", web_search=None)
    initial._codex_controls.update(
        "stack@native-id", approval_policy=None,
        permission_profile=":stack", web_search=None)

    monkeypatch.setenv("CODEX_HOME", str(primary_home))
    fallback_cfg = WrapperConfig()
    fallback_cfg.state_dir = state
    fallback_cfg.claude_work_root = tmp_path / "work" / "claude"
    fallback_cfg.codex_work_root = tmp_path / "work" / "codex"
    fallback_cfg.codex_profiles_json = ""
    fallback = WrapperMachine(fallback_cfg, _StubTransport())

    assert fallback._codex_controls is not None
    assert fallback._codex_controls.get(
        "native-id").permission_profile == ":main"
    assert fallback._codex_controls.get(
        "stack@native-id").permission_profile == ":stack"


def test_codex_work_wire_id_cannot_bypass_work_guard_or_artifact_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, transport = _machine(tmp_path)
    store = machine._work.for_engine("codex")
    record = store.create_session(codex_profile_id="primary")
    store.bind_session(
        record.work_id, "native-work-id", codex_profile_id="primary",
    )
    (Path(record.cwd) / "result.txt").write_text("done", encoding="utf-8")

    async def is_codex(_sid: str) -> bool:
        return True

    async def forbidden_rpc(*_args, **_kwargs):
        raise AssertionError("Code delete must stop at the Work guard")

    monkeypatch.setattr(machine, "_is_codex_session", is_codex)
    monkeypatch.setattr(machine, "_codex_rpc_for_wire", forbidden_rpc)

    async def run() -> None:
        blocked = await machine._handle_delete_session(SimpleNamespace(
            session_id="primary@native-work-id",
            engine="codex",
            space="code",
            client_id="client-1",
        ))
        assert isinstance(blocked, Error)
        assert blocked.code == ERR_AUTH

        transport.sent.clear()
        result = await machine._handle_get_work_artifacts(SimpleNamespace(
            session_id="primary@native-work-id",
            engine="codex",
            client_id="client-1",
        ))
        assert isinstance(result, WorkArtifacts)
        assert result.session_id == "primary@native-work-id"
        assert [item.path for item in result.artifacts] == ["result.txt"]

        transport.sent.clear()
        invalid = await machine._handle_get_work_artifacts(SimpleNamespace(
            session_id="missing@native-work-id",
            engine="codex",
            client_id="client-1",
        ))
        assert isinstance(invalid, Error)
        assert invalid.code == ERR_AUTH
        assert transport.sent == [invalid]

        transport.sent.clear()
        await machine._handle_pin_session(SimpleNamespace(
            session_id="primary@native-work-id",
            engine="codex",
            space="work",
            pinned=True,
            cmd_id="pin-1",
            client_id="client-1",
        ))
        assert machine._session_pins is not None
        assert "primary@native-work-id" in machine._session_pins.ids("codex")

    asyncio.run(run())


def test_legacy_unbound_codex_work_never_recovers_from_secondary_profile(
    tmp_path: Path,
) -> None:
    machine, transport = _machine(tmp_path)
    store = machine._work.for_engine("codex")
    record = store.create_session()
    raw = [{
        "session_id": "stack@secondary-native-id",
        "native_session_id": "secondary-native-id",
        "codex_profile_id": "stack",
        "codex_profile_label": "Stack",
        "cwd": record.cwd,
        "summary": "secondary",
        "last_modified": "2026-08-06T00:00:00Z",
    }]

    result = asyncio.run(machine._send_codex_session_list(
        SimpleNamespace(space="work", client_id="client-1", cmd_id="list-1"),
        raw,
    ))

    assert result.sessions == []
    assert store.get_by_work_id(record.work_id).session_id is None
    assert transport.sent == [result]


def test_codex_profile_catalog_has_one_fair_global_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine.CODEX_SESSION_LIST_MAX_ROWS = 4
    machine.CODEX_SESSION_LIST_MAX_BYTES = 1024 * 1024
    calls: list[tuple[str | None, int]] = []

    async def list_sessions(limit: int, *, codex_home=None):
        calls.append((codex_home, limit))
        prefix = "primary" if codex_home == str(
            (tmp_path / "primary").resolve()) else "stack"
        return [{
            "session_id": f"{prefix}-{index}",
            "summary": prefix,
            "last_modified": 100 - index,
        } for index in range(2)]

    monkeypatch.setattr(machine_module, "list_codex_sessions", list_sessions)

    rows = asyncio.run(machine._read_all_codex_profile_sessions())

    assert calls == [
        (str((tmp_path / "primary").resolve()), 1),
        (str((tmp_path / "stack").resolve()), 1),
    ]
    assert len(rows) == 4
    assert [row["codex_profile_id"] for row in rows].count("primary") == 2
    assert [row["codex_profile_id"] for row in rows].count("stack") == 2


def test_codex_profile_catalog_stays_within_one_serialized_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    machine.CODEX_SESSION_LIST_MAX_ROWS = 20
    machine.CODEX_SESSION_LIST_MAX_BYTES = 900

    async def list_sessions(_limit: int, *, codex_home=None):
        prefix = "primary" if codex_home == str(
            (tmp_path / "primary").resolve()) else "stack"
        return [{
            "session_id": f"{prefix}-{index}",
            "summary": f"{prefix}-" + ("x" * 160),
            "last_modified": 100 - index,
        } for index in range(10)]

    monkeypatch.setattr(machine_module, "list_codex_sessions", list_sessions)

    rows = asyncio.run(machine._read_all_codex_profile_sessions())
    encoded = sum(
        len(json.dumps(
            row, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")) + 64
        for row in rows
    )

    assert rows
    assert len(rows) < 20
    assert encoded <= machine.CODEX_SESSION_LIST_MAX_BYTES
    assert {row["codex_profile_id"] for row in rows} == {"primary", "stack"}


def test_unknown_profile_switch_fails_closed_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, transport = _machine(tmp_path)

    async def forbidden_spawn(**_kwargs):
        raise AssertionError("unknown profile must not reach _spawn")

    monkeypatch.setattr(machine, "_spawn", forbidden_spawn)
    result = asyncio.run(machine._handle_switch_session(SimpleNamespace(
        session_id="missing@same-native-id",
        engine="codex",
        space="code",
        cmd_id="switch-1",
        client_id="client-1",
    )))

    assert result.code == ERR_AUTH
    assert result.request_id == "switch-1"
    assert result.to == "client-1"
    assert transport.sent == [result]


def test_profile_model_reads_use_catalog_default_from_matching_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    stack_home = str((tmp_path / "stack").resolve())
    calls: list[tuple[str, str | None]] = []

    async def catalog(*, codex_home=None):
        calls.append(("models", codex_home))
        return [{
            "id": "stack-model",
            "display_name": "Stack model",
            "description": "",
            "efforts": ["high"],
            "default_effort": "high",
            "is_default": True,
        }]

    def configured_model(_default="", *, codex_home=None):
        calls.append(("default", codex_home))
        # A newly added account commonly has no model in config.toml. Its own
        # app-server catalog default must win; never borrow the primary account
        # or CodexHandle's historical fallback.
        return ""

    def configured_effort(_default="", *, codex_home=None):
        calls.append(("effort", codex_home))
        return ""

    async def permission_profiles(_cwd, *, codex_home=None):
        calls.append(("permissions", codex_home))
        return [{"id": ":stack", "description": "Stack", "allowed": True}]

    async def capabilities(
        _engine, _cwd, _space, _claude_bin, *, skills_only=False,
        codex_home=None,
    ):
        calls.append(("capabilities", codex_home))
        return [], [], []

    monkeypatch.setattr(machine_module, "codex_catalog", catalog)
    monkeypatch.setattr(machine_module, "codex_model", configured_model)
    monkeypatch.setattr(machine_module, "codex_effort", configured_effort)
    monkeypatch.setattr(
        machine_module, "codex_permission_profiles", permission_profiles)
    monkeypatch.setattr(machine_module, "engine_capabilities", capabilities)

    async def run() -> None:
        models = await machine._handle_get_models(SimpleNamespace(
            engine="codex", codex_profile_id="stack",
            cmd_id="models-1", client_id="client-1", cwd=None,
        ))
        assert models.codex_profile_id == "stack"
        assert models.default_model == "stack-model"
        assert models.default_effort == "high"
        permissions = await machine._handle_get_permission_profiles(
            SimpleNamespace(
                sid=None,
                cwd=str(tmp_path),
                codex_profile_id="stack",
                cmd_id="permissions-1",
                client_id="client-1",
            ))
        assert permissions.codex_profile_id == "stack"
        report = await machine._handle_get_engine_capabilities(
            SimpleNamespace(
                engine="codex",
                space="work",
                cwd=str(tmp_path),
                codex_profile_id="stack",
                skills_only=True,
                cmd_id="capabilities-1",
                client_id="client-1",
            ))
        assert report.codex_profile_id == "stack"

    asyncio.run(run())
    assert calls[0] == ("models", stack_home)
    assert set(calls[1:3]) == {
        ("default", stack_home),
        ("effort", stack_home),
    }
    assert calls[3:] == [
        ("permissions", stack_home),
        ("capabilities", stack_home),
    ]


def test_profile_model_resolution_isolated_and_fails_closed_for_explicit_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    calls: list[str] = []

    async def catalog(*, codex_home=None):
        profile_id = Path(codex_home).name
        calls.append(profile_id)
        return [{
            "id": f"{profile_id}-default",
            "efforts": ["low"],
            "default_effort": "low",
            "is_default": True,
        }]

    def provider(*, codex_home=None):
        return "cubence" if Path(codex_home).name == "primary" else ""

    monkeypatch.setattr(machine_module, "codex_catalog", catalog)
    monkeypatch.setattr(machine_module, "codex_current_provider", provider)

    async def run() -> None:
        primary = machine._codex_profile("primary")
        stack = machine._codex_profile("stack")
        assert await machine._resolve_codex_profile_model(
            primary, None,
        ) == ("primary-default", True)
        assert await machine._resolve_codex_profile_model(
            primary, "custom-provider-model",
        ) == ("custom-provider-model", False)
        assert await machine._resolve_codex_profile_model(
            stack, "retired-model",
        ) == ("stack-default", True)
        with pytest.raises(machine_module._UnsupportedCodexModel):
            await machine._resolve_codex_profile_model(
                stack, "primary-default", explicit=True)

        # An empty result is availability uncertainty. Do not resurrect an old
        # hardcoded model when no configured/session value exists.
        assert await machine._resolve_codex_profile_model(
            stack, None, catalog=[],
        ) == (None, False)

    asyncio.run(run())
    assert calls == ["primary", "primary", "stack", "stack"]


def test_new_stack_session_applies_its_catalog_default_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    created = []

    class FakeCodexHandle:
        def __init__(self, _cfg, **kwargs):
            self.init = kwargs
            self.model = "gpt-5-codex"
            self.effort = None
            self.applied_effort = None
            self.display_effort = None
            self.display_effort_model = None
            self.display_effort_cwd = None
            self.display_effort_generation = None
            self._display_effort_retry_at = None
            self._cwd = kwargs["cwd"]
            self.cwd = kwargs["cwd"]
            self._generation = 1
            self._approval = "never"
            self.approval_policy = "never"
            self.permission_profile = None
            self.web_search = "cached"
            self.web_search_override = None
            self.collaboration_mode = "default"
            self.service_tier = None
            self.shared_daemon_affinity = False
            self.using_daemon_proxy = False
            self.thread_id = None
            self.connect_model = None
            self.connect_effort = None
            created.append(self)

        @property
        def approval(self):
            return self._approval

        @approval.setter
        def approval(self, value):
            self._approval = value
            self.approval_policy = value

        async def connect(self, **_kwargs):
            self.connect_model = self.model
            self.connect_effort = self.effort
            self.thread_id = "stack-native"

        async def disconnect(self):
            return None

    stack_home = str((tmp_path / "stack").resolve())

    async def catalog(*, codex_home=None):
        assert codex_home == stack_home
        return [{
            "id": "stack-default",
            "efforts": ["low"],
            "default_effort": "low",
            "is_default": True,
        }]

    def configured_model(_default="", *, codex_home=None):
        assert codex_home == stack_home
        return ""

    async def clamp(_model, effort, *, codex_home=None):
        assert _model == "stack-default"
        assert codex_home == stack_home
        return effort

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(machine_module, "CodexHandle", FakeCodexHandle)
    monkeypatch.setattr(machine_module, "codex_catalog", catalog)
    monkeypatch.setattr(machine_module, "codex_model", configured_model)
    monkeypatch.setattr(machine_module, "clamp_effort", clamp)
    monkeypatch.setattr(machine, "_resolve_codex_session_effort", no_op)
    monkeypatch.setattr(machine, "_stamp_codex_daemon_epoch", no_op)
    monkeypatch.setattr(machine, "_persist_codex_session_controls", no_op)
    monkeypatch.setattr(machine, "_load_history", no_op)

    ctx = asyncio.run(machine._spawn(
        resume_id=None,
        cwd=str(tmp_path),
        engine="codex",
        codex_profile_id="stack",
        raise_on_failure=True,
    ))

    assert ctx is not None
    assert ctx.key == "stack@stack-native"
    assert ctx.codex_profile_id == "stack"
    assert created[0].init["codex_home"] == stack_home
    assert created[0].connect_model == "stack-default"
    assert created[0].connect_effort is None


def test_explicit_stack_model_is_rejected_before_handle_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    stack_home = str((tmp_path / "stack").resolve())

    async def catalog(*, codex_home=None):
        assert codex_home == stack_home
        return [{
            "id": "stack-default",
            "efforts": ["low"],
            "default_effort": "low",
            "is_default": True,
        }]

    monkeypatch.setattr(machine_module, "codex_catalog", catalog)
    monkeypatch.setattr(
        machine_module,
        "CodexHandle",
        lambda *_args, **_kwargs: pytest.fail(
            "unsupported model must fail before handle creation"),
    )

    with pytest.raises(machine_module._SpawnFailure) as caught:
        asyncio.run(machine._spawn(
            resume_id=None,
            cwd=str(tmp_path),
            engine="codex",
            codex_profile_id="stack",
            model="primary-only-model",
            raise_on_failure=True,
        ))

    assert caught.value.code == machine_module.ERR_BAD_PROMPT


def test_new_codex_work_session_freezes_selected_profile_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, transport = _machine(tmp_path)
    captured: dict[str, object] = {}

    async def activate_runtime_events() -> None:
        return None

    async def spawn(**kwargs):
        captured.update(kwargs)
        ctx = SessionContext(
            session_id=None,
            sdk=SimpleNamespace(
                collaboration_mode="default",
                service_tier=None,
                activate_runtime_events=activate_runtime_events,
            ),
            buffer=RingBuffer(100, 1024 * 1024),
            cwd=str(kwargs["cwd"]),
            key="tmp-stack-work",
            engine="codex",
            space="work",
            work_id=str(kwargs["work_id"]),
            codex_profile_id=str(kwargs["codex_profile_id"]),
        )
        machine.sessions[ctx.key] = ctx
        return ctx

    monkeypatch.setattr(machine, "_spawn", spawn)

    async def run() -> None:
        await machine._handle_new_session(SimpleNamespace(
            engine="codex",
            space="work",
            codex_profile_id="stack",
            project_id=None,
            request_id="work-stack-1",
            client_id="client-1",
            images=None,
            files=None,
            model=None,
            effort=None,
            collaboration_mode=None,
            permission_mode="never",
            permission_profile=None,
            web_search=None,
            service_tier=None,
            prompt=None,
            msg_id=None,
        ))

    asyncio.run(run())

    assert captured["codex_profile_id"] == "stack"
    record = machine._work.for_engine("codex").get_by_work_id(
        str(captured["work_id"])
    )
    assert record is not None
    assert record.codex_profile_id == "stack"
    assert machine.focused_sid == "tmp-stack-work"
    assert not [item for item in transport.sent if isinstance(item, Error)]


def test_codex_work_schedule_runs_only_on_its_bound_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    store = machine._work.for_engine("codex")
    now = time.time()
    store.create_schedule(
        "Stack schedule", "run once", now - 1,
        codex_profile_id="stack",
    )
    schedule = store.claim_due_schedules(now)[0]
    captured: dict[str, object] = {}

    async def spawn(**kwargs):
        captured.update(kwargs)
        ctx = SessionContext(
            session_id="scheduled-native",
            sdk=object(),
            buffer=RingBuffer(100, 1024 * 1024),
            cwd=str(kwargs["cwd"]),
            key="stack@scheduled-native",
            engine="codex",
            space="work",
            work_id=str(kwargs["work_id"]),
            codex_profile_id="stack",
        )
        store.bind_session(
            ctx.work_id, ctx.session_id, codex_profile_id="stack",
        )
        machine.sessions[ctx.key] = ctx
        return ctx

    async def query(command):
        ctx = machine.sessions[command.sid]
        ctx.turn_task = asyncio.create_task(asyncio.sleep(0))
        return SimpleNamespace(type="command_ack")

    async def broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(machine, "_spawn", spawn)
    monkeypatch.setattr(machine, "_handle_query", query)
    monkeypatch.setattr(machine, "_broadcast_work_schedule_state", broadcast)

    asyncio.run(machine._run_work_schedule("codex", schedule))

    assert captured["codex_profile_id"] == "stack"
    result = store.dashboard()["schedules"][0]
    assert result["codex_profile_id"] == "stack"
    assert result["last_codex_profile_id"] == "stack"
    assert result["last_run_status"] == "succeeded"


def test_codex_work_schedule_mutation_freezes_selected_profile(
    tmp_path: Path,
) -> None:
    machine, transport = _machine(tmp_path)

    result = asyncio.run(machine._handle_work_mutation(SimpleNamespace(
        type="create_work_schedule",
        engine="codex",
        title="Stack schedule",
        prompt="run on stack",
        next_run_at=time.time() + 3600,
        repeat_seconds=None,
        project_id=None,
        codex_profile_id="stack",
        client_id="client-1",
    )))

    assert isinstance(result, WorkDashboard)
    assert len(result.schedules) == 1
    assert result.schedules[0].codex_profile_id == "stack"
    assert transport.sent == [result]


def test_codex_work_schedule_missing_profile_fails_without_retry_or_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _transport = _machine(tmp_path)
    store = machine._work.for_engine("codex")
    now = time.time()
    store.create_schedule(
        "Removed account", "must not run", now - 1,
        codex_profile_id="removed",
    )
    schedule = store.claim_due_schedules(now)[0]

    async def forbidden_spawn(**_kwargs):
        raise AssertionError("a removed profile must not reach app-server")

    async def broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(machine, "_spawn", forbidden_spawn)
    monkeypatch.setattr(machine, "_broadcast_work_schedule_state", broadcast)

    asyncio.run(machine._run_work_schedule("codex", schedule))

    result = store.dashboard()["schedules"][0]
    assert result["codex_profile_id"] == "removed"
    assert result["last_codex_profile_id"] == "removed"
    assert result["last_run_status"] == "failed"
    assert result["last_run_attempt"] == 1
    assert "账号不可用" in result["last_error"]


def test_native_start_hint_without_catalog_does_not_publish_empty_session(tmp_path, monkeypatch):
    async def run():
        machine, _ = _machine(tmp_path)
        primary = _context("primary@current", "current", "primary")
        machine.sessions[primary.key] = primary

        async def no_catalog(_limit, *, codex_home=None):
            return []

        monkeypatch.setattr(machine_module, "list_codex_sessions", no_catalog)
        monkeypatch.setattr(machine_module, "codex_exact_catalog_rows", lambda *a, **kw: [])
        machine._on_codex_thread_started_hint(primary, "unmaterialized-helper")
        await asyncio.gather(*tuple(machine._codex_catalog_hint_tasks))
        rows = await machine._read_all_codex_profile_sessions()
        assert all(row["native_session_id"] != "unmaterialized-helper" for row in rows)
        assert any(row["native_session_id"] == "current" for row in rows)

    asyncio.run(run())
