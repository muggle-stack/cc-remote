"""Legacy migration must bind to the actual service; never silently adopt."""
from pathlib import Path

import pytest

from deploy import adopt_wrapper


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "legacy"
    old = root / "releases/old"
    old.mkdir(parents=True)
    (old / "release-manifest.json").write_text("{}")
    (root / "current").symlink_to(old)
    service = tmp_path.resolve() / "wrapper.service"
    service.write_text(f"""[Service]
User=operator
WorkingDirectory={root}/current
ExecStart={root}/current/.venv/bin/python -m cc_remote.wrapper
EnvironmentFile=/etc/cc-remote/wrapper.env
EnvironmentFile=-/etc/cc-remote/device.env
Restart=on-failure
""")
    monkeypatch.setattr(adopt_wrapper, "root_owned", lambda p: None)
    monkeypatch.setattr(adopt_wrapper.subprocess, "check_output", lambda *a, **k:
                        f"User=operator\nWorkingDirectory={root}/current\nFragmentPath={service}\nDropInPaths=\n")
    return root, old, service, tmp_path.resolve() / "managed"


def test_explicit_migration_validates_without_modifying_legacy(legacy):
    root, old, service, destination = legacy
    before = service.read_bytes()
    assert adopt_wrapper.validate(root, "operator", destination, service) == old
    assert service.read_bytes() == before and not destination.exists()
    assert (root / "current").resolve() == old


def test_migrated_service_keeps_custom_policy_on_subsequent_upgrade(legacy):
    root, _, service, destination = legacy
    original = service.read_text() + 'Environment="HTTPS_PROXY=http://127.0.0.1:8888"\nMemoryHigh=5G\n'
    moved = adopt_wrapper.rebase_service(original, root / "current", destination / "current", "operator")
    assert moved == original.replace(str(root / "current"), str(destination / "current"))
    assert adopt_wrapper.rebase_service(moved, destination / "current", destination / "current", "operator") == moved


@pytest.mark.parametrize("mismatch", ["user", "command", "env", "dropin", "destination", "symlink", "ownership"])
def test_migration_refuses_ambiguous_or_unrelated_installations(legacy, monkeypatch, mismatch):
    root, _, service, destination = legacy
    if mismatch == "user":
        service.write_text(service.read_text().replace("User=operator", "User=other"))
    elif mismatch == "command":
        service.write_text(service.read_text().replace("-m cc_remote.wrapper", "-m another_app"))
    elif mismatch == "env":
        service.write_text(service.read_text().replace("/etc/cc-remote/wrapper.env", "/private/custom.env"))
    elif mismatch == "dropin":
        monkeypatch.setattr(adopt_wrapper.subprocess, "check_output", lambda *a, **k:
                            f"User=operator\nWorkingDirectory={root}/current\nFragmentPath={service}\nDropInPaths=/etc/override.conf\n")
    elif mismatch == "destination":
        destination.mkdir()
        (destination / "installation.json").write_text("{}")
    elif mismatch == "symlink":
        link = root.with_name("alias")
        link.symlink_to(root)
        root = link
    else:
        def unsafe(path: Path):
            raise ValueError("unsafe ownership")
        monkeypatch.setattr(adopt_wrapper, "root_owned", unsafe)
    with pytest.raises(ValueError):
        adopt_wrapper.validate(root, "operator", destination, service)
