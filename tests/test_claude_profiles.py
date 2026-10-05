"""Claude account registry, topology, and explicit catalog regressions."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cc_remote.claude_profiles import (
    ClaudeProfileRegistry,
    ClaudeProfileTopologyTransition,
    ClaudeProfileTopologyStore,
)
from cc_remote import config as config_module
from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    ClaudeProfileInfo,
    CreateWorkSchedule,
    Error,
    ListSessions,
    NewSession,
    SessionList,
    deserialize,
    serialize,
)
from cc_remote.wrapper import claude_catalog, machine as machine_module
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.session_presentation import SessionPresentationStore
from cc_remote.viewer_pages import PageRef, PageScope, ViewerPageStore
from tests.test_multisession import _mk_ctx


NATIVE_ID = "11111111-1111-4111-8111-111111111111"


class _StubTransport:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.on_connected = None

    async def send(self, message: object) -> None:
        self.sent.append(message)


def _profiles(personal: Path, company: Path) -> str:
    return json.dumps({
        "personal": {
            "label": "Personal",
            "config_dir": str(personal),
            "default": True,
        },
        "company": {
            "label": "Company",
            "config_dir": str(company),
        },
    })


def _write_transcript(
    root: Path,
    prompt: str,
    *,
    native_id: str = NATIVE_ID,
    cwd: str = "/repo",
) -> None:
    project_name = "-" + cwd.strip("/").replace("/", "-")
    path = root / "projects" / project_name / f"{native_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "user",
            "uuid": "22222222-2222-4222-8222-222222222222",
            "parentUuid": None,
            "sessionId": native_id,
            "cwd": cwd,
            "timestamp": "2026-08-30T00:00:00.000Z",
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "uuid": "33333333-3333-4333-8333-333333333333",
            "parentUuid": "22222222-2222-4222-8222-222222222222",
            "sessionId": native_id,
            "cwd": cwd,
            "timestamp": "2026-08-30T00:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "world"}],
                "model": "claude-test",
                "stop_reason": "end_turn",
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_empty_configuration_preserves_single_account_compatibility(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / ".claude"
    registry = ClaudeProfileRegistry.from_json(
        "", default_config_dir=legacy)

    assert registry.default.id == "primary"
    assert registry.default.config_dir == legacy.resolve()
    assert registry.public_profiles() == [{
        "id": "primary",
        "label": "默认账号",
    }]
    assert registry.wire_session_id("primary", NATIVE_ID) == NATIVE_ID
    assert registry.resolve_wire_session_id(NATIVE_ID) == (
        registry.default,
        NATIVE_ID,
    )


def test_two_accounts_namespace_identical_native_session_ids(
    tmp_path: Path,
) -> None:
    registry = ClaudeProfileRegistry.from_json(_profiles(
        tmp_path / "personal", tmp_path / "company"))

    assert registry.wire_session_id("personal", NATIVE_ID) == (
        f"personal@{NATIVE_ID}"
    )
    assert registry.wire_session_id("company", NATIVE_ID) == (
        f"company@{NATIVE_ID}"
    )
    profile, native_id = registry.resolve_wire_session_id(
        f"company@{NATIVE_ID}")
    assert (profile.id, native_id) == ("company", NATIVE_ID)
    assert registry.public_profiles() == [
        {"id": "personal", "label": "Personal"},
        {"id": "company", "label": "Company"},
    ]
    assert all(
        "config_dir" not in profile
        for profile in registry.public_profiles()
    )


def test_claude_profile_protocol_roundtrip_and_engine_boundary() -> None:
    profiles = [
        ClaudeProfileInfo(id=f"account-{index}", label=f"Account {index}")
        for index in range(14)
    ]
    listing = deserialize(serialize(SessionList(
        engine="claude",
        sessions=[],
        claude_profiles=profiles,
        default_claude_profile_id="account-0",
    )))
    assert isinstance(listing, SessionList)
    assert [profile.id for profile in listing.claude_profiles] == [
        f"account-{index}" for index in range(14)
    ]

    new_session = NewSession(
        engine="claude", claude_profile_id="account-1")
    assert deserialize(serialize(new_session)) == new_session
    schedule = CreateWorkSchedule(
        engine="claude",
        claude_profile_id="account-1",
        title="Report",
        prompt="Build it",
        next_run_at=1,
    )
    assert deserialize(serialize(schedule)) == schedule

    with pytest.raises(ValidationError):
        NewSession(engine="codex", claude_profile_id="account-1")
    with pytest.raises(ValidationError):
        CreateWorkSchedule(
            engine="codex",
            claude_profile_id="account-1",
            title="Report",
            prompt="Build it",
            next_run_at=1,
        )


@pytest.mark.parametrize("raw", ["", "explicit"])
def test_single_account_rejects_namespaced_session_ids(
    raw: str,
    tmp_path: Path,
) -> None:
    registry = ClaudeProfileRegistry.from_json(
        "" if raw == "" else json.dumps({
            "solo": {
                "label": "Solo",
                "config_dir": str(tmp_path / "solo"),
                "default": True,
            },
        }),
        default_config_dir=tmp_path / "default",
    )

    with pytest.raises(ValueError, match="must not be namespaced"):
        registry.resolve_wire_session_id(
            f"{registry.default.id}@{NATIVE_ID}")


def test_multi_account_rejects_ambiguous_or_unknown_session_ids(
    tmp_path: Path,
) -> None:
    registry = ClaudeProfileRegistry.from_json(_profiles(
        tmp_path / "personal", tmp_path / "company"))

    with pytest.raises(ValueError, match="must be namespaced"):
        registry.resolve_wire_session_id(NATIVE_ID)
    with pytest.raises(ValueError, match="unknown Claude profile"):
        registry.resolve_wire_session_id(f"missing@{NATIVE_ID}")
    with pytest.raises(ValueError, match="invalid native Claude session id"):
        registry.wire_session_id("personal", f"{NATIVE_ID}@ambiguous")


@pytest.mark.parametrize("profile_id", ["bad@id", "bad:id", " has-space", ""])
def test_profile_ids_are_wire_safe(
    profile_id: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="profile id"):
        ClaudeProfileRegistry.from_json(json.dumps({
            profile_id: {
                "label": "Bad",
                "config_dir": str(tmp_path / "bad"),
                "default": True,
            },
        }))


def test_registry_rejects_duplicate_roots_unknown_fields_and_defaults(
    tmp_path: Path,
) -> None:
    same = tmp_path / "same"
    with pytest.raises(ValueError, match="unique"):
        ClaudeProfileRegistry.from_json(json.dumps({
            "one": {
                "label": "One",
                "config_dir": str(same),
                "default": True,
            },
            "two": {
                "label": "Two",
                "config_dir": str(same / "."),
            },
        }))
    with pytest.raises(ValueError, match="unknown fields"):
        ClaudeProfileRegistry.from_json(json.dumps({
            "one": {
                "label": "One",
                "config_dir": str(tmp_path / "one"),
                "default": True,
                "token": "must-never-be-accepted",
            },
        }))
    with pytest.raises(ValueError, match="exactly one default"):
        ClaudeProfileRegistry.from_json(json.dumps({
            "one": {
                "label": "One",
                "config_dir": str(tmp_path / "one"),
                "default": True,
            },
            "two": {
                "label": "Two",
                "config_dir": str(tmp_path / "two"),
                "default": True,
            },
        }))


def test_profile_file_is_separate_and_inline_configuration_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_file = tmp_path / "claude-profiles.json"
    payload = _profiles(tmp_path / "personal", tmp_path / "company")
    profile_file.write_text(payload, encoding="utf-8")
    monkeypatch.delenv("CC_REMOTE_CLAUDE_PROFILES_JSON", raising=False)
    monkeypatch.setenv("CC_REMOTE_CLAUDE_PROFILES_FILE", str(profile_file))

    assert WrapperConfig().claude_profiles_json == payload

    inline = json.dumps({
        "solo": {
            "label": "Solo",
            "config_dir": str(tmp_path / "solo"),
            "default": True,
        },
    })
    monkeypatch.setenv("CC_REMOTE_CLAUDE_PROFILES_JSON", inline)
    assert WrapperConfig().claude_profiles_json == inline


def test_macos_default_profile_file_survives_an_older_launchagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_file = tmp_path / ".cc-remote" / "claude-profiles.json"
    profile_file.parent.mkdir()
    payload = _profiles(tmp_path / "personal", tmp_path / "company")
    profile_file.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(
        "XPC_SERVICE_NAME", "com.mugglepro.cc-remote-wrapper")
    monkeypatch.delenv("CC_REMOTE_CLAUDE_PROFILES_JSON", raising=False)
    monkeypatch.delenv("CC_REMOTE_CLAUDE_PROFILES_FILE", raising=False)

    assert WrapperConfig().claude_profiles_json == payload


def test_unrelated_process_does_not_implicitly_enable_claude_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_file = tmp_path / ".cc-remote" / "claude-profiles.json"
    profile_file.parent.mkdir()
    profile_file.write_text(
        _profiles(tmp_path / "personal", tmp_path / "company"),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.example.unrelated")
    monkeypatch.delenv("CC_REMOTE_CLAUDE_PROFILES_JSON", raising=False)
    monkeypatch.delenv("CC_REMOTE_CLAUDE_PROFILES_FILE", raising=False)

    assert WrapperConfig().claude_profiles_json == ""


def test_initial_topology_requires_the_effective_legacy_account_once(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    registry = ClaudeProfileRegistry.from_json(_profiles(
        tmp_path / "personal", tmp_path / "company"))
    store = ClaudeProfileTopologyStore(tmp_path / "state")

    with pytest.raises(ValueError, match="legacy CLAUDE_CONFIG_DIR exactly once"):
        store.prepare(registry, legacy_config_dir=legacy)
    assert not store.pending_path.exists()


def test_initial_single_account_baseline_survives_bad_optional_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "claude-session-controls.json").write_text(
        "not-json", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(legacy))
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"

    machine = WrapperMachine(cfg, _StubTransport())

    assert machine._claude_controls is None
    assert machine._claude_profile_migration_ok is True
    assert ClaudeProfileTopologyStore(state).path.exists()


def test_multi_account_migration_stays_closed_without_control_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal"
    company = tmp_path / "company"
    personal.mkdir()
    company.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "claude-session-controls.json").write_text(
        "not-json", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    cfg = WrapperConfig()
    cfg.state_dir = state
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = _profiles(personal, company)

    machine = WrapperMachine(cfg, _StubTransport())
    topology = ClaudeProfileTopologyStore(state)

    assert machine._claude_controls is None
    assert machine._claude_profile_migration_ok is False
    assert topology.pending_path.exists()
    assert not topology.path.exists()


def test_topology_tracks_account_identity_by_config_dir(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    personal = tmp_path / "personal"
    company = tmp_path / "company"
    initial = ClaudeProfileRegistry.from_json(_profiles(personal, company))
    store = ClaudeProfileTopologyStore(state)
    first = store.prepare(initial, legacy_config_dir=personal)
    assert first is not None
    assert first.legacy_profile_id == "personal"
    store.complete(initial, first)

    renamed = ClaudeProfileRegistry.from_json(json.dumps({
        "personal": {
            "label": "Personal",
            "config_dir": str(personal),
            "default": True,
        },
        "work": {
            "label": "Work",
            "config_dir": str(company),
        },
    }))
    transition = store.prepare(renamed, legacy_config_dir=personal)

    assert transition is not None
    assert transition.remaps == {"company": "work"}
    assert transition.wire_session_id(
        f"company@{NATIVE_ID}") == f"work@{NATIVE_ID}"
    assert transition.wire_session_id(
        f"personal@{NATIVE_ID}") == f"personal@{NATIVE_ID}"


def test_persisted_bootstrap_route_is_translated_before_current_resolution(
    tmp_path: Path,
) -> None:
    account_a = tmp_path / "account-a"
    account_b = tmp_path / "account-b"
    original = ClaudeProfileRegistry.from_json(json.dumps({
        "a": {
            "label": "A",
            "config_dir": str(account_a),
            "default": True,
        },
        "b": {"label": "B", "config_dir": str(account_b)},
    }))
    store = ClaudeProfileTopologyStore(tmp_path / "state")
    initial = store.prepare(original, legacy_config_dir=account_a)
    assert initial is not None
    store.complete(original, initial)

    swapped = ClaudeProfileRegistry.from_json(json.dumps({
        "b": {
            "label": "A renamed",
            "config_dir": str(account_a),
            "default": True,
        },
        "a": {"label": "B renamed", "config_dir": str(account_b)},
    }))
    transition = store.prepare(swapped, legacy_config_dir=account_a)
    assert transition is not None
    machine = WrapperMachine.__new__(WrapperMachine)
    machine._claude_profiles = swapped
    machine._claude_profile_transition = transition

    profile, native_id, routed = machine._claude_bootstrap_target(
        f"a@{NATIVE_ID}", persisted=True)

    assert profile.id == "b"
    assert native_id == NATIVE_ID
    assert routed == f"b@{NATIVE_ID}"

    # A Code session saved after core migration already belongs to the target
    # revision even if an independent Work migration leaves the marker pending.
    # Never replay the swap over that current route.
    machine._claude_profile_revision = transition.revision
    profile, native_id, routed = machine._claude_bootstrap_target(
        f"a@{NATIVE_ID}",
        persisted=True,
        persisted_profile_id="a",
        persisted_profile_revision=transition.revision,
    )
    assert (profile.id, native_id, routed) == (
        "a", NATIVE_ID, f"a@{NATIVE_ID}")

    # An explicit current setting keeps the historical convenience of using
    # an unqualified UUID for the configured default profile.
    profile, native_id, routed = machine._claude_bootstrap_target(
        NATIVE_ID, persisted=False)
    assert (profile.id, native_id, routed) == (
        "b", NATIVE_ID, f"b@{NATIVE_ID}")


def test_legacy_bootstrap_after_topology_commit_uses_legacy_root_owner(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    other = tmp_path / "other"
    registry = ClaudeProfileRegistry.from_json(json.dumps({
        "other": {
            "label": "Other",
            "config_dir": str(other),
            "default": True,
        },
        "legacy": {
            "label": "Legacy",
            "config_dir": str(legacy),
        },
    }))
    machine = WrapperMachine.__new__(WrapperMachine)
    machine._claude_profiles = registry
    machine._claude_profile_transition = None
    machine._claude_profile_revision = 1
    machine._claude_legacy_config_dir = legacy

    profile, native_id, routed = machine._claude_bootstrap_target(
        NATIVE_ID, persisted=True)

    assert (profile.id, native_id, routed) == (
        "legacy", NATIVE_ID, f"legacy@{NATIVE_ID}")


def test_topology_rejects_reusing_an_id_for_another_account(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    old_root = tmp_path / "old"
    original = ClaudeProfileRegistry.from_json(json.dumps({
        "account": {
            "label": "Account",
            "config_dir": str(old_root),
            "default": True,
        },
    }))
    store = ClaudeProfileTopologyStore(state)
    initial = store.prepare(original, legacy_config_dir=old_root)
    assert initial is not None
    store.complete(original, initial)

    replacement = ClaudeProfileRegistry.from_json(json.dumps({
        "account": {
            "label": "Replacement",
            "config_dir": str(tmp_path / "replacement"),
            "default": True,
        },
    }))
    with pytest.raises(ValueError, match="cannot replace its config_dir"):
        store.prepare(replacement, legacy_config_dir=old_root)


def test_completed_topology_recovers_a_crash_before_pending_unlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    registry = ClaudeProfileRegistry.from_json(json.dumps({
        "account": {
            "label": "Account",
            "config_dir": str(root),
            "default": True,
        },
    }))
    store = ClaudeProfileTopologyStore(tmp_path / "state")
    transition = store.prepare(registry, legacy_config_dir=root)
    assert transition is not None and store.pending_path.exists()

    # Simulate complete() crashing after the durable topology replace but
    # before removing its replay marker.
    store.persist(registry, revision=transition.revision)

    assert store.prepare(registry, legacy_config_dir=root) is None
    assert not store.pending_path.exists()


def test_presentation_failure_keeps_claude_topology_replay_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal"
    company = tmp_path / "company"
    personal.mkdir()
    company.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    monkeypatch.setattr(
        SessionPresentationStore,
        "migrate_claude_profile_sessions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("temporary presentation failure")
        ),
    )
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = _profiles(personal, company)

    machine = WrapperMachine(cfg, _StubTransport())
    topology = ClaudeProfileTopologyStore(cfg.state_dir)

    assert machine._claude_profile_migration_ok is True
    assert machine._claude_work_profile_migration_ok is True
    assert machine._claude_presentation_profile_migration_ok is False
    assert topology.pending_path.exists()
    assert not topology.path.exists()


def test_private_btw_profile_revision_prevents_swap_replay(
    tmp_path: Path,
) -> None:
    transition = ClaudeProfileTopologyTransition(
        revision=2,
        previous=(("a", "/account-a"), ("b", "/account-b")),
        previous_default_id="a",
        current=(("b", "/account-a"), ("a", "/account-b")),
        current_default_id="b",
        remaps={"a": "b", "b": "a"},
    )
    machine = WrapperMachine.__new__(WrapperMachine)
    machine.cfg = SimpleNamespace(state_dir=tmp_path)
    machine._private_btw_profile_revision = 1
    machine._private_btw_sessions = OrderedDict({
        f"a@{NATIVE_ID}": {"cwd": "/repo", "created_at": 1},
    })
    machine._persist_private_btw_sessions()

    assert machine._migrate_private_btw_sessions(transition) == 1
    assert list(machine._private_btw_sessions) == [f"b@{NATIVE_ID}"]

    # Reload the exact crash state and replay the still-pending transition.
    reloaded = WrapperMachine.__new__(WrapperMachine)
    reloaded.cfg = SimpleNamespace(state_dir=tmp_path)
    reloaded._private_btw_profile_revision = 0
    reloaded._private_btw_sessions = reloaded._load_private_btw_sessions()
    assert reloaded._private_btw_profile_revision == 2
    assert reloaded._migrate_private_btw_sessions(transition) == 0
    assert list(reloaded._private_btw_sessions) == [f"b@{NATIVE_ID}"]


def test_explicit_catalog_mutations_never_cross_profile_roots(
    tmp_path: Path,
) -> None:
    personal_project = tmp_path / "personal" / "projects" / "-repo"
    company_project = tmp_path / "company" / "projects" / "-repo"
    personal_project.mkdir(parents=True)
    company_project.mkdir(parents=True)
    personal_transcript = personal_project / f"{NATIVE_ID}.jsonl"
    company_transcript = company_project / f"{NATIVE_ID}.jsonl"
    original = json.dumps({
        "type": "user",
        "sessionId": NATIVE_ID,
        "uuid": "22222222-2222-4222-8222-222222222222",
        "message": {"role": "user", "content": "hello"},
    }) + "\n"
    personal_transcript.write_text(original, encoding="utf-8")
    company_transcript.write_text(original, encoding="utf-8")

    claude_catalog.rename_session(
        tmp_path / "company", NATIVE_ID, "Company title")

    assert personal_transcript.read_text(encoding="utf-8") == original
    assert "Company title" in company_transcript.read_text(encoding="utf-8")

    claude_catalog.delete_session(tmp_path / "company", NATIVE_ID)

    assert personal_transcript.exists()
    assert not company_transcript.exists()


def test_explicit_catalog_can_exclude_git_worktrees_for_continue_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "profile"
    other_id = "44444444-4444-4444-8444-444444444444"
    _write_transcript(root, "main", cwd="/repo")
    _write_transcript(
        root,
        "worktree",
        native_id=other_id,
        cwd="/other",
    )
    monkeypatch.setattr(
        claude_catalog,
        "_get_worktree_paths",
        lambda _cwd: ["/repo", "/other"],
    )

    exact = claude_catalog.list_sessions(
        root,
        directory="/repo",
        include_worktrees=False,
    )
    with_worktrees = claude_catalog.list_sessions(
        root,
        directory="/repo",
        include_worktrees=True,
    )

    assert {item.session_id for item in exact} == {NATIVE_ID}
    assert {item.session_id for item in with_worktrees} == {
        NATIVE_ID,
        other_id,
    }


def test_machine_lists_duplicate_native_ids_in_both_claude_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal"
    company = tmp_path / "company"
    _write_transcript(personal, "personal prompt")
    _write_transcript(company, "company prompt")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = _profiles(personal, company)
    transport = _StubTransport()
    machine = WrapperMachine(cfg, transport)

    asyncio.run(machine._handle_list_sessions(ListSessions(
        engine="claude", space="code", client_id="client-a")))

    listing = next(
        message for message in transport.sent
        if isinstance(message, SessionList)
    )
    assert listing.default_claude_profile_id == "personal"
    assert [profile.model_dump() for profile in listing.claude_profiles] == [
        {"id": "personal", "label": "Personal", "error": None},
        {"id": "company", "label": "Company", "error": None},
    ]
    assert {
        (
            session.session_id,
            session.native_session_id,
            session.claude_profile_id,
            session.first_prompt,
        )
        for session in listing.sessions
    } == {
        (
            f"personal@{NATIVE_ID}", NATIVE_ID,
            "personal", "personal prompt",
        ),
        (
            f"company@{NATIVE_ID}", NATIVE_ID,
            "company", "company prompt",
        ),
    }


@pytest.mark.parametrize("multi", [False, True])
def test_cold_viewer_scope_reads_the_owning_claude_catalog_without_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool,
) -> None:
    personal, company = tmp_path / "personal", tmp_path / "company"
    _write_transcript(personal, "personal", cwd="/personal-project")
    _write_transcript(company, "company", cwd="/company-project")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = _profiles(personal, company) if multi else ""
    machine = WrapperMachine(cfg, _StubTransport())
    expected = [(f"personal@{NATIVE_ID}", "/personal-project"),
                (f"company@{NATIVE_ID}", "/company-project")] if multi else [
                    (NATIVE_ID, "/personal-project")]

    for sid, cwd in expected:
        scope = PageScope(engine="claude", space="code", sid=sid)
        resolved, actual_cwd = asyncio.run(machine._viewer_page_scope(scope))
        assert resolved == scope
        assert actual_cwd == cwd
    assert machine.sessions == {}
    assert machine.focused_sid is None
    assert machine.transport.sent == []

    unknown = PageScope(engine="claude", space="code", sid=expected[0][0] + "-missing")
    with pytest.raises(ValueError, match="unknown session"):
        asyncio.run(machine._viewer_page_scope(unknown))


def test_claude_profile_transitions_migrate_viewer_scopes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal, company = tmp_path / "personal", tmp_path / "company"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = ""
    initial = WrapperMachine(cfg, _StubTransport())
    store = initial.viewer_pages.store
    scope = PageScope(engine="claude", space="code", sid=NATIVE_ID)
    page = PageRef(machine_id="device", site_id="demo", entry="/index.html", label="Personal")
    store.associate(scope, [page], automatic=False)
    # Code and Work are separate lists even for the same native session.
    work_scope = scope.model_copy(update={"space": "work"})
    store.associate(work_scope, [page], automatic=True)
    store.remove(work_scope, page.id)
    codex_scope = scope.model_copy(update={"engine": "codex"})
    store.associate(codex_scope, [page], automatic=False)

    cfg.claude_profiles_json = _profiles(personal, company)
    multi = WrapperMachine(cfg, _StubTransport())
    personal_scope = scope.model_copy(update={"sid": f"personal@{NATIVE_ID}"})
    company_scope = scope.model_copy(update={"sid": f"company@{NATIVE_ID}"})
    assert store.list(scope) == []
    assert store.list(personal_scope)[0]["label"] == "Personal"
    store.associate(company_scope, [page.model_copy(update={"label": "Company"})], automatic=False)

    # Swap both IDs and simulate a crash after store migration but before the
    # topology commit. A new Wrapper must not replay the non-idempotent swap.
    cfg.claude_profiles_json = _profiles(company, personal)
    complete = ClaudeProfileTopologyStore.complete
    monkeypatch.setattr(ClaudeProfileTopologyStore, "complete",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash")))
    failed = WrapperMachine(cfg, _StubTransport())
    assert not failed._claude_profile_migration_ok
    assert store.list(company_scope)[0]["label"] == "Personal"
    assert store.list(personal_scope)[0]["label"] == "Company"
    monkeypatch.setattr(ClaudeProfileTopologyStore, "complete", complete)
    recovered = WrapperMachine(cfg, _StubTransport())
    assert recovered._claude_profile_migration_ok
    assert store.list(company_scope)[0]["label"] == "Personal"
    assert store.list(personal_scope)[0]["label"] == "Company"

    # Back to one account: the other account's qualified key must stay private.
    cfg.claude_profiles_json = ""
    single = WrapperMachine(cfg, _StubTransport())
    assert single._claude_profile_migration_ok
    assert store.list(scope)[0]["label"] == "Personal"
    assert store.list(personal_scope)[0]["label"] == "Company"
    assert store.associate(work_scope, [page], automatic=True) == []
    assert store.list(codex_scope)[0]["label"] == "Personal"
    assert ViewerPageStore(store.path).list(scope) == store.list(scope)
    assert multi.sessions == recovered.sessions == single.sessions == {}


@pytest.mark.parametrize("late_read", ["presentation", "broker"])
@pytest.mark.parametrize("next_state", ["idle", "running", None])
def test_claude_catalog_samples_activity_after_async_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    late_read: str,
    next_state: str | None,
) -> None:
    personal, company = tmp_path / "personal", tmp_path / "company"
    _write_transcript(personal, "personal prompt")
    _write_transcript(company, "company prompt")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    # Exercise both namespaced accounts and the legacy broker's final await.
    cfg.claude_profiles_json = (
        _profiles(personal, company) if late_read == "presentation" else ""
    )
    transport = _StubTransport()
    machine = WrapperMachine(cfg, transport)

    async def run():
        sid = machine._claude_wire_sid(machine._claude_profiles.default, NATIVE_ID)
        ctx = _mk_ctx(sid, NATIVE_ID)
        ctx.claude_profile_id = machine._claude_profiles.default.id
        ctx.state = "idle" if next_state == "running" else "running"
        machine.sessions[sid] = ctx
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_read(*_args):
            entered.set()
            await release.wait()
            return {"sessions": []}

        if late_read == "presentation":
            monkeypatch.setattr(
                machine, "_claim_legacy_presentation_from_claude_catalog",
                delayed_read,
            )
        else:
            machine._claude_broker_enabled = True
            machine._claude_broker = SimpleNamespace(list=delayed_read)

        pending = asyncio.create_task(machine._handle_list_sessions(ListSessions(
            engine="claude", space="code", client_id="viewer")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            # The terminal/start/eviction reaches the viewer while the older
            # catalog request is still waiting on unrelated metadata I/O.
            if next_state is None:
                del machine.sessions[sid]
            else:
                await machine._set_state(ctx, next_state)
        finally:
            release.set()
        listing = await asyncio.wait_for(pending, timeout=5)
        assert isinstance(listing, SessionList)
        row = next(row for row in listing.sessions if row.session_id == sid)
        assert row.state == next_state
        assert transport.sent[-1] is listing
        if next_state is not None:
            assert transport.sent[-2].type == "state"
            assert transport.sent[-2].sid == sid
            assert transport.sent[-2].state == next_state
        if late_read == "presentation":
            other = next(row for row in listing.sessions
                         if row.session_id == f"company@{NATIVE_ID}")
            assert other.state is None

    asyncio.run(run())


def test_claude_session_list_fails_closed_during_profile_migration() -> None:
    machine = WrapperMachine.__new__(WrapperMachine)
    transport = _StubTransport()
    machine.transport = transport
    machine._claude_profile_migration_ok = False
    machine._claude_work_profile_migration_ok = False

    result = asyncio.run(machine._handle_list_sessions(SimpleNamespace(
        engine="claude",
        space="code",
        cmd_id="list-1",
        client_id="client-a",
    )))

    assert isinstance(result, Error)
    assert result.code == "internal"
    assert result.to == "client-a"
    assert transport.sent == [result]


def test_secondary_rate_limit_cache_follows_config_dir_not_profile_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal"
    company = tmp_path / "company"
    personal.mkdir()
    company.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))

    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = _profiles(personal, company)
    original = WrapperMachine(cfg, _StubTransport())
    original_path = original._claude_rate_limit_stores["company"].path

    cfg.claude_profiles_json = json.dumps({
        "personal": {
            "label": "Personal",
            "config_dir": str(personal),
            "default": True,
        },
        "work": {"label": "Work", "config_dir": str(company)},
    })
    renamed = WrapperMachine(cfg, _StubTransport())

    assert renamed._claude_rate_limit_stores["work"].path == original_path


def test_missing_secondary_history_never_falls_back_to_ambient_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal"
    company = tmp_path / "company"
    personal.mkdir()
    company.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = _profiles(personal, company)
    machine = WrapperMachine(cfg, _StubTransport())
    monkeypatch.setattr(
        machine_module,
        "transcript_compact_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambient Claude catalog must not be read")),
    )

    history = asyncio.run(machine._build_history(
        f"company@{NATIVE_ID}", limit=4, detail="summary"))

    assert history.authoritative is False
    assert history.error == "历史暂时不可用，请稍后重试"
    assert history.events == []


def test_missing_claude_schedule_account_fails_without_retry_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal"
    company = tmp_path / "company"
    personal.mkdir()
    company.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.claude_profiles_json = _profiles(personal, company)
    machine = WrapperMachine(cfg, _StubTransport())
    store = machine._work.for_engine("claude")
    schedule_id = store.create_schedule(
        "Company task",
        "Generate report",
        time.time() - 1,
        claude_profile_id="company",
    )
    run = store.claim_due_schedules(time.time())[0]
    company.rmdir()

    asyncio.run(machine._run_work_schedule("claude", run))

    schedule = next(
        item for item in store.dashboard()["schedules"]
        if item["schedule_id"] == schedule_id
    )
    assert schedule["last_run_status"] == "failed"
    assert schedule["last_error"] == (
        "绑定的 Claude 账号不可用，请恢复账号配置")
    assert store.unbound_records_by_cwd() == {}
