"""Zero-token tests for private Codex per-thread control durability."""

from __future__ import annotations

import os

import pytest

from cc_remote.wrapper.codex_controls import (
    CodexControlStore,
    CodexControlStoreError,
)


def test_codex_control_store_round_trips_and_clears_override(tmp_path):
    store = CodexControlStore(tmp_path)
    thread_id = "019f94fc-6230-7212-8a08-4e19bbc49104"

    assert store.get(thread_id).as_dict() == {}
    controls = store.update(
        thread_id,
        approval_policy="never",
        permission_profile=":danger-full-access",
        web_search="live",
    )
    assert controls.as_dict() == {
        "approval_policy": "never",
        "permission_profile": ":danger-full-access",
        "web_search": "live",
    }
    assert CodexControlStore(tmp_path).get(thread_id) == controls
    assert (os.stat(store.path).st_mode & 0o077) == 0

    store.update(
        thread_id,
        approval_policy=None,
        permission_profile=None,
        web_search=None,
    )
    assert CodexControlStore(tmp_path).get(thread_id).as_dict() == {}


def test_codex_control_store_preserves_explicit_cwd_across_control_updates(
    tmp_path,
):
    store = CodexControlStore(tmp_path)
    thread_id = "019fb107-c6bd-73c1-95ca-0130cfd71ccd"
    migrated = str(tmp_path / "migrated")

    store.set_cwd_override(thread_id, migrated)
    controls = store.update(
        thread_id,
        approval_policy="never",
        permission_profile=None,
        web_search="live",
    )

    assert controls.cwd_override == migrated
    assert store.cwd_overrides() == {thread_id: migrated}
    assert CodexControlStore(tmp_path).get(thread_id).as_dict() == {
        "approval_policy": "never",
        "web_search": "live",
        "cwd_override": migrated,
    }

    store.delete(thread_id)
    assert store.get(thread_id).as_dict() == {}


def test_codex_control_store_clears_only_the_expected_cwd_override(tmp_path):
    store = CodexControlStore(tmp_path)
    thread_id = "thread-cwd-compare-and-clear"
    old = str(tmp_path / "old")
    replacement = str(tmp_path / "replacement")
    store.update(
        thread_id,
        approval_policy="never",
        permission_profile=":workspace",
        web_search="live",
    )
    store.set_cwd_override(thread_id, old)
    store.set_cwd_override(thread_id, replacement)

    raced = store.clear_cwd_override_if_matches(thread_id, old)
    assert raced.cwd_override == replacement

    cleared = store.clear_cwd_override_if_matches(thread_id, replacement)
    assert cleared.as_dict() == {
        "approval_policy": "never",
        "permission_profile": ":workspace",
        "web_search": "live",
    }
    assert store.get(thread_id) == cleared


def test_codex_control_store_restores_a_post_replace_failed_cwd_write(
    monkeypatch, tmp_path,
):
    store = CodexControlStore(tmp_path)
    thread_id = "thread-partial-cwd-write"
    previous = str(tmp_path / "previous")
    attempted = str(tmp_path / "attempted")
    store.set_cwd_override(thread_id, previous)
    real_persist = store._persist
    injected = False

    def persist_then_fail(sessions):
        nonlocal injected
        real_persist(sessions)
        cwd = sessions.get(thread_id, {}).get("cwd_override")
        if not injected and cwd == attempted:
            injected = True
            raise CodexControlStoreError("post-replace failure")

    monkeypatch.setattr(store, "_persist", persist_then_fail)

    with pytest.raises(CodexControlStoreError, match="post-replace"):
        store.set_cwd_override(thread_id, attempted)

    # The failed setter has not published its in-memory projection, but the
    # replacement already reached disk.
    assert store.get(thread_id).cwd_override == previous
    assert CodexControlStore(tmp_path).get(
        thread_id).cwd_override == attempted

    restored = store.restore_cwd_override_after_failed_set(
        thread_id, attempted, previous)

    assert restored.cwd_override == previous
    assert store.get(thread_id).cwd_override == previous
    assert CodexControlStore(tmp_path).get(
        thread_id).cwd_override == previous


def test_codex_control_store_rejects_relative_cwd_override(tmp_path):
    store = CodexControlStore(tmp_path)
    with pytest.raises(CodexControlStoreError, match="cwd override"):
        store.set_cwd_override("thread-1", "relative/path")


def test_codex_control_store_loads_legacy_search_only_entry(tmp_path):
    path = tmp_path / "codex-session-controls.json"
    path.write_text(
        '{"version":1,"sessions":{"legacy":{"web_search":"live"}}}')
    path.chmod(0o600)

    assert CodexControlStore(tmp_path).get("legacy").as_dict() == {
        "web_search": "live",
    }


def test_codex_control_store_rejects_unsafe_file(tmp_path):
    path = tmp_path / "codex-session-controls.json"
    path.write_text('{"version":1,"sessions":{}}')
    path.chmod(0o644)
    with pytest.raises(CodexControlStoreError, match="private"):
        CodexControlStore(tmp_path)
