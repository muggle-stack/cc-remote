"""Suite-wide isolation from a developer's real engine account registries."""

from __future__ import annotations

import pytest


_ACCOUNT_PROFILE_ENV = (
    "CODEX_HOME",
    "CC_REMOTE_CODEX_PROFILES_JSON",
    "CC_REMOTE_CODEX_PROFILES_FILE",
    "CC_REMOTE_CLAUDE_PROFILES_JSON",
    "CC_REMOTE_CLAUDE_PROFILES_FILE",
    # A test launched from an installed Wrapper inherits launchd's identity.
    # That enables legacy Claude profile discovery from the real user's home.
    "XPC_SERVICE_NAME",
)


@pytest.fixture(autouse=True)
def _isolate_account_profile_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make every test opt in to account/profile configuration explicitly.

    ``WrapperConfig`` and account registries intentionally inherit the
    launching shell in production.  Unit tests must not inherit those values:
    otherwise a developer running pytest from a managed Wrapper/``CODEX_HOME`` gets
    a different registry and dozens of unrelated mock-signature failures.
    """
    for key in _ACCOUNT_PROFILE_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_native_picker_writes(monkeypatch, tmp_path_factory):
    """Legacy runtime mocks have no private account. Never write the real one.

    Settings integration tests opt in by giving their handle a tmp_path config
    root; those exercise the real writer, including errors and preservation.
    """
    from pathlib import Path
    from cc_remote.wrapper import sdk
    from cc_remote.wrapper.claude_settings import save_user_defaults

    temporary = tmp_path_factory.getbasetemp().resolve()

    def isolated_save(root, **choices):
        if Path(root).resolve().is_relative_to(temporary):
            return save_user_defaults(root, **choices)
        return None

    monkeypatch.setattr(sdk, "save_user_defaults", isolated_save)


@pytest.fixture
def browser_singletons():
    """Real Chromium-style IPC markers; keep Unix socket paths short on macOS."""
    from pathlib import Path
    import socket
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ccr-ipc-") as directory:
        temporary = Path(directory).resolve()
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(temporary / "SingletonSocket"))
            (temporary / "SingletonCookie").symlink_to("12345678901234567890")

            def install(root):
                (root / "SingletonCookie").symlink_to("12345678901234567890")
                (root / "SingletonLock").symlink_to("example.local-12345")
                (root / "SingletonSocket").symlink_to(temporary / "SingletonSocket")
                return temporary

            yield install
