"""Offline checks for test discovery and production deploy guardrails."""

from __future__ import annotations

import ast
import configparser
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from deploy.caddy_managed_block import (
    BEGIN_MARKER,
    END_MARKER,
    GLOBAL_BEGIN_MARKER,
    GLOBAL_END_MARKER,
    CaddyMergeError,
    render_managed_caddyfile,
)
from deploy.validate_protocol_bundle import (
    ProtocolBundleError,
    validate_protocol_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


def _is_main_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def _contains_asyncio_run(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "asyncio"
        and child.func.attr == "run"
        for child in ast.walk(node)
    )


def test_pytest_only_discovers_offline_test_tree():
    config = configparser.ConfigParser()
    config.read(ROOT / "pytest.ini")
    assert config["pytest"]["testpaths"].split() == ["tests"]
    assert config["pytest"]["python_files"].split() == ["test_*.py"]
    assert not list(ROOT.glob("test_*_live.py"))


def test_live_scripts_require_an_explicit_main_entrypoint():
    live_scripts = sorted((ROOT / "scripts" / "live").glob("test_*_live.py"))
    assert live_scripts
    for path in live_scripts:
        tree = ast.parse(path.read_text(), filename=str(path))
        guards = [node for node in tree.body if _is_main_guard(node)]
        assert len(guards) == 1, f"{path.name} must have one __main__ guard"
        assert _contains_asyncio_run(guards[0])
        for node in tree.body:
            if node is guards[0] or isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            assert not _contains_asyncio_run(node), (
                f"{path.name} starts a live run while being imported"
            )


def test_claude_pty_broker_is_not_a_documented_or_installed_feature():
    public_files = [
        ROOT / "README.md",
        ROOT / "README_en.md",
        ROOT / "deploy" / "README.md",
        ROOT / "deploy" / "env.wrapper.example",
    ]
    for path in public_files:
        source = path.read_text()
        assert "claude-remote" not in source
        assert "CC_REMOTE_CLAUDE_BROKER_SOCKET" not in source
    assert not (ROOT / "scripts" / "claude-remote").exists()

    experiment = ROOT / "experiments" / "claude-remote"
    result = subprocess.run(
        [str(experiment), "status"],
        env={
            key: value for key, value in os.environ.items()
            if key != "CC_REMOTE_EXPERIMENTAL_CLAUDE_BROKER"
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 64
    assert "unsupported experimental command" in result.stderr


def test_setup_script_is_valid_shell_and_keeps_safe_install_order():
    script = ROOT / "deploy" / "setup-vps.sh"
    transaction = ROOT / "deploy" / "setup_transaction.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    subprocess.run(["bash", "-n", str(transaction)], check=True)
    source = script.read_text()

    validate = source.index('caddy validate --config "$CADDY_CANDIDATE"')
    backup = source.index('cp -a "$CADDYFILE" "$CADDY_BACKUP"')
    changed = source.index("CADDY_CHANGED=1", backup)
    install = source.index(
        'atomic_install_file "$CADDY_CANDIDATE" "$CADDYFILE" root root 0644'
    )
    assert validate < backup < changed < install
    unit_backup = source.index('cp -a "$RELAY_UNIT_FILE" "$UNIT_BACKUP"')
    unit_changed = source.index("UNIT_CHANGED=1", unit_backup)
    unit_install = source.index("atomic_install_file", unit_changed)
    assert unit_backup < unit_changed < unit_install
    assert "require_secret LOGIN_PASSWORD 16" in source
    assert "require_secret SESSION_SECRET 32" in source
    assert "require_secret WRAPPER_TOKEN 32" in source
    assert 'TARGET="${TARGET_INPUT,,}"' in source
    assert 'PUBLIC_SCHEME=http' in source
    assert 'CADDY_TEMPLATE="Caddyfile.insecure"' in source
    assert '"$SOURCE_DIR/deploy/$CADDY_TEMPLATE"' in source
    assert '"$NEW_RELEASE_DIR/deploy/$CADDY_TEMPLATE"' in source
    assert "address.version == 4 and address.is_global" in source
    assert "not address.is_multicast" in source
    assert '"$CONFIGURED_ORIGIN" == "$PUBLIC_SCHEME://$TARGET"' in source
    assert '[[ "$CONFIGURED_RELAY_HOST" == "0.0.0.0" ]]' in source
    assert '[[ "$CONFIGURED_RELAY_HOST" == "127.0.0.1" ]]' in source
    assert 'if [[ "$PRIVATE_DIRECT" -eq 1 ]]' in source
    assert '[[ "$CONFIGURED_RELAY_PORT" == "8765" ]]' in source
    assert '[[ "$CONFIGURED_STATIC_DIR" == "$CURRENT_LINK/web/dist" ]]' in source
    assert 'chmod 0600 "$ENV_FILE"' in source
    assert "systemctl restart cc-remote-relay" in source
    assert 'python3 "$NEW_RELEASE_DIR/deploy/caddy_managed_block.py"' in source
    assert 'source "$SOURCE_DIR/deploy/setup_transaction.sh"' in source
    assert "UNIT_BACKUP=" in source
    assert 'cp -a "$RELAY_UNIT_FILE" "$UNIT_BACKUP"' in source
    assert "RELAY_SERVICE_TOUCHED=1" in source
    assert "flock -n 9" in source
    assert 'RELEASES_DIR="$APPDIR/releases"' in source
    assert 'STATE_DIR="$APPDIR/state"' in source
    assert 'RUNTIMES_DIR="$APPDIR/runtimes"' in source
    assert 'mkdir -p "$RELEASES_DIR" "$STATE_DIR"' in source
    assert 'chown ccremote:ccremote "$STATE_DIR"' in source
    assert 'CURRENT_LINK="$APPDIR/current"' in source
    assert 'NEW_RELEASE_DIR="$(mktemp -d "$RELEASES_DIR/release-' in source
    assert 'atomic_release_link "$NEW_RELEASE_DIR" "$CURRENT_LINK"' in source
    assert 'atomic_release_link "$PREVIOUS_RELEASE" "$CURRENT_LINK"' in source
    previous_owner = source.index('chown -R root:ccremote "$PREVIOUS_RELEASE"')
    previous_harden = source.index(
        'harden_release_permissions "$PREVIOUS_RELEASE"'
    )
    new_owner = source.index('chown -R root:ccremote "$NEW_RELEASE_DIR"')
    new_harden = source.index('harden_release_permissions "$NEW_RELEASE_DIR"')
    assert previous_owner < previous_harden
    assert new_owner < new_harden
    assert "rsync" not in source

    staged_validate = source.index(
        'python3 "$NEW_RELEASE_DIR/deploy/validate_protocol_bundle.py"'
    )
    staged_import = source.index("validate_relay_config(relay_config())")
    activate = source.index(
        'atomic_release_link "$NEW_RELEASE_DIR" "$CURRENT_LINK"'
    )
    unit_verify = source.index(
        'systemd-analyze verify "$UNIT_VERIFY_DIR/cc-remote-relay.service"'
    )
    relay_stop = source.index("systemctl stop cc-remote-relay")
    relay_restart = source.index("systemctl restart cc-remote-relay")
    legacy_baseline = source.index(
        'atomic_release_link "$PREVIOUS_RELEASE" "$CURRENT_LINK"'
    )
    release_stage = source.index('echo "==> staging immutable release"')
    assert (
        staged_validate
        < staged_import
        < unit_verify
        < relay_stop
        < activate
        < relay_restart
    )
    assert legacy_baseline < release_stage


def test_insecure_caddy_template_is_explicit_http_without_tls_headers():
    source = (ROOT / "deploy" / "Caddyfile.insecure").read_text()
    assert "http://cc-remote.example.com {" in source
    assert "ws://cc-remote.example.com" in source
    assert "reverse_proxy 127.0.0.1:8765" in source
    assert "Strict-Transport-Security" not in source
    assert "https://" not in source


def test_deploy_examples_configure_insecure_flag_on_both_sides():
    relay = (ROOT / "deploy" / "env.relay.example").read_text()
    wrapper = (ROOT / "deploy" / "env.wrapper.example").read_text()
    assert "ALLOW_INSECURE_HTTP=0" in relay
    assert "ALLOW_INSECURE_HTTP=0" in wrapper
    assert "ALLOW_PRIVATE_ORIGINS=0" in relay


def test_setup_does_not_make_network_service_owner_of_root_executed_code():
    source = (ROOT / "deploy" / "setup-vps.sh").read_text()
    assert 'chown -R root:ccremote "$PREVIOUS_RELEASE"' in source
    assert 'chown -R root:ccremote "$NEW_RELEASE_DIR"' in source
    assert 'chown root:ccremote "$ENV_FILE"' in source
    assert 'chmod 0640 "$ENV_FILE"' in source
    assert 'chown -R ccremote:ccremote "$APPDIR"' not in source
    assert "sudo -u ccremote" not in source
    assert 'python3 -m venv "$NEW_RELEASE_DIR/.venv"' in source
    assert "VENV_STAGE" not in source
    assert "VENV_BACKUP" not in source
    assert 'UV_PYTHON_INSTALL_DIR="$RUNTIMES_DIR"' in source
    assert '--python "$PYTHON_RUNTIME"' in source
    assert "--exclude='./runtimes'" in source
    assert "--exclude='./state'" in source
    transaction_source = (ROOT / "deploy" / "setup_transaction.sh").read_text()
    assert "rollback_release" in transaction_source
    assert "rollback_deployment" in transaction_source
    assert 'cp -a "$CADDY_BACKUP" "$CADDYFILE"' in transaction_source
    assert 'cp -a "$UNIT_BACKUP" "$RELAY_UNIT_FILE"' in transaction_source
    assert "systemctl daemon-reload" in transaction_source
    assert "previous relay passed /healthz" in transaction_source
    assert "--require-hashes" in source
    assert "--only-binary=:all:" in source
    # pywebpush's pure-Python http-ece dependency publishes no wheel. Keep the
    # hash-locked exception narrow instead of disabling the wheel-only policy.
    assert "--no-binary=http-ece" in source
    assert 'requirements-relay.lock' in source
    assert "apt-get install -y" in source and "gnupg" in source
    assert "command -v gpg" in source
    lock = (ROOT / "requirements-relay.lock").read_text()
    assert "--hash=sha256:" in lock
    assert "cryptography==" in lock
    assert "Python 3.10 or newer is required" in source


@pytest.mark.parametrize("destination_exists", [False, True])
def test_atomic_install_failure_never_mutates_destination(
    tmp_path, destination_exists,
):
    source = tmp_path / "candidate"
    destination = tmp_path / "installed"
    source.write_text("complete new config")
    if destination_exists:
        destination.write_text("original config")

    harness = r'''
set -euo pipefail
source "$1"
install() {
  local destination="${@: -1}"
  printf '%s' 'partial config' > "$destination"
  return 1
}
atomic_install_file "$2" "$3" root root 0644
'''
    result = subprocess.run(
        [
            "bash",
            "-c",
            harness,
            "atomic-install-test",
            str(ROOT / "deploy" / "setup_transaction.sh"),
            str(source),
            str(destination),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    if destination_exists:
        assert destination.read_text() == "original config"
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(".installed.cc-remote.*"))


def test_atomic_install_replaces_destination_with_requested_mode(tmp_path):
    source = tmp_path / "candidate"
    destination = tmp_path / "installed"
    source.write_text("complete new config")
    destination.write_text("original config")

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; atomic_install_file "$2" "$3" "$(id -un)" "$(id -gn)" 0640',
            "atomic-install-test",
            str(ROOT / "deploy" / "setup_transaction.sh"),
            str(source),
            str(destination),
        ],
        check=True,
    )

    assert destination.read_text() == "complete new config"
    assert destination.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".installed.cc-remote.*"))


def test_release_permissions_remove_inherited_group_write_and_other_access(
    tmp_path,
):
    release = tmp_path / "release"
    directory = release / "package"
    directory.mkdir(parents=True)
    regular = directory / "module.py"
    executable = directory / "tool"
    regular.write_text("value = 1\n")
    executable.write_text("#!/bin/sh\n")
    release.chmod(0o775)
    directory.chmod(0o775)
    regular.chmod(0o664)
    executable.chmod(0o775)

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; harden_release_permissions "$2"',
            "permission-test",
            str(ROOT / "deploy" / "setup_transaction.sh"),
            str(release),
        ],
        check=True,
    )

    assert release.stat().st_mode & 0o777 == 0o750
    assert directory.stat().st_mode & 0o777 == 0o750
    assert regular.stat().st_mode & 0o777 == 0o640
    assert executable.stat().st_mode & 0o777 == 0o750


def test_injected_post_switch_failure_restores_full_release_caddy_and_unit(
    tmp_path,
):
    appdir = tmp_path / "app"
    releases = appdir / "releases"
    old_release = releases / "release-old"
    new_release = releases / "release-new"
    for release, marker in ((old_release, "old"), (new_release, "new")):
        for relative in ("cc_remote", "web/dist", ".venv"):
            directory = release / relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "marker").write_text(marker)
    current = appdir / "current"
    current.symlink_to(old_release, target_is_directory=True)

    caddyfile = tmp_path / "Caddyfile"
    caddy_backup = tmp_path / "Caddyfile.backup"
    caddyfile.write_text("new caddy")
    caddy_backup.write_text("old caddy")

    unit = tmp_path / "cc-remote-relay.service"
    unit_backup = tmp_path / "cc-remote-relay.service.backup"
    unit.write_text("new unit")
    unit_backup.write_text("old unit")
    systemctl_log = tmp_path / "systemctl.log"

    harness = r'''
set -euo pipefail
source "$1"
APPDIR="$2"
RELEASES_DIR="$APPDIR/releases"
CURRENT_LINK="$APPDIR/current"
PREVIOUS_RELEASE="$3"
NEW_RELEASE_DIR="$4"
RELEASE_SWITCHED=0
DEPLOY_READY=0
CADDYFILE="$5"
CADDY_BACKUP="$6"
CADDY_SITE=""
CADDY_CANDIDATE=""
CADDY_CHANGED=1
CADDY_HAD_CONFIG=1
CADDY_SERVICE_TOUCHED=1
RELAY_UNIT_FILE="$7"
UNIT_BACKUP="$8"
UNIT_CHANGED=1
UNIT_HAD_FILE=1
RELAY_SERVICE_TOUCHED=1
ROLLBACK_DONE=0
SYSTEMCTL_LOG="$9"
systemctl() {
  printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
  return 0
}
curl() {
  printf '%s\n' "$*" >> "$CURL_LOG"
  return 0
}
trap cleanup EXIT
RELEASE_SWITCHED=1
atomic_release_link "$NEW_RELEASE_DIR" "$CURRENT_LINK"
false  # injected relay readiness failure after the complete release switch
'''
    result = subprocess.run(
        [
            "bash", "-c", harness, "rollback-test",
            str(ROOT / "deploy" / "setup_transaction.sh"),
            str(appdir), str(old_release), str(new_release),
            str(caddyfile), str(caddy_backup), str(unit), str(unit_backup),
            str(systemctl_log),
        ],
        env={**os.environ, "CURL_LOG": str(tmp_path / "curl.log")},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert current.resolve() == old_release.resolve()
    for relative in ("cc_remote", "web/dist", ".venv"):
        assert (old_release / relative / "marker").read_text() == "old"
    assert not new_release.exists()
    assert caddyfile.read_text() == "old caddy"
    assert unit.read_text() == "old unit"
    calls = systemctl_log.read_text().splitlines()
    assert calls == [
        "daemon-reload",
        "restart caddy",
        "restart cc-remote-relay",
    ]
    curl_call = (tmp_path / "curl.log").read_text()
    assert "--max-time 2" in curl_call
    assert "http://127.0.0.1:8765/healthz" in curl_call
    assert "previous relay passed /healthz" in result.stderr
    assert not caddy_backup.exists()
    assert not unit_backup.exists()


def test_failed_release_rollback_never_deletes_the_still_active_release(tmp_path):
    appdir = tmp_path / "app"
    releases = appdir / "releases"
    old_release = releases / "release-old"
    new_release = releases / "release-new"
    old_release.mkdir(parents=True)
    new_release.mkdir()
    current = appdir / "current"
    current.symlink_to(new_release, target_is_directory=True)

    harness = r'''
set -euo pipefail
source "$1"
APPDIR="$2"
RELEASES_DIR="$APPDIR/releases"
CURRENT_LINK="$APPDIR/current"
PREVIOUS_RELEASE="$3"
NEW_RELEASE_DIR="$4"
RELEASE_SWITCHED=1
DEPLOY_READY=0
CADDYFILE="$APPDIR/Caddyfile"
CADDY_BACKUP=""
CADDY_SITE=""
CADDY_CANDIDATE=""
CADDY_CHANGED=0
CADDY_HAD_CONFIG=0
CADDY_SERVICE_TOUCHED=0
RELAY_UNIT_FILE="$APPDIR/cc-remote-relay.service"
UNIT_BACKUP=""
UNIT_CHANGED=0
UNIT_HAD_FILE=0
RELAY_SERVICE_TOUCHED=0
ROLLBACK_DONE=0
systemctl() { return 0; }
curl() { return 0; }
# Simulate an I/O failure while trying to replace current with the old link.
atomic_release_link() { return 1; }
trap cleanup EXIT
false
'''
    result = subprocess.run(
        [
            "bash",
            "-c",
            harness,
            "rollback-test",
            str(ROOT / "deploy" / "setup_transaction.sh"),
            str(appdir),
            str(old_release),
            str(new_release),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert current.resolve() == new_release.resolve()
    assert new_release.is_dir()
    assert "retaining staged release because release rollback failed" in result.stderr


def test_web_build_manifest_matches_both_protocol_implementations():
    manifest = json.loads(
        (ROOT / "web" / "public" / "cc-remote-build.json").read_text())
    from cc_remote import __version__
    from cc_remote.protocol import PROTOCOL_VERSION

    ts = (ROOT / "web" / "src" / "protocol.ts").read_text()
    match = re.search(r"PROTOCOL_VERSION\s*=\s*(\d+)", ts)
    assert match
    assert manifest["protocol"] == PROTOCOL_VERSION == int(match.group(1))
    assert manifest["version"] == __version__


def test_deploy_protocol_validation_reads_backend_and_manifest(tmp_path):
    product = tmp_path / "__init__.py"
    backend = tmp_path / "protocol.py"
    manifest = tmp_path / "cc-remote-build.json"
    product.write_text('__version__ = "3.0.0"\n')
    backend.write_text("PROTOCOL_VERSION = 37\n")
    manifest.write_text('{"version":"3.0.0","protocol":37}\n')
    assert validate_protocol_bundle(backend, manifest) == 37

    manifest.write_text('{"version":"3.0.0","protocol":36}\n')
    with pytest.raises(ProtocolBundleError, match="backend v37, web v36"):
        validate_protocol_bundle(backend, manifest)

    manifest.write_text('{"version":"2.9.0","protocol":37}\n')
    with pytest.raises(
        ProtocolBundleError,
        match="backend v3.0.0, web v2.9.0",
    ):
        validate_protocol_bundle(backend, manifest)


def test_setup_protocol_gate_has_no_release_specific_literal():
    source = (ROOT / "deploy" / "setup-vps.sh").read_text()
    assert "validate_protocol_bundle.py" in source
    assert "web build protocol is not v" not in source
    assert not re.search(r'"protocol"[^\n]*[0-9]+', source)


def test_release_docs_and_examples_describe_one_atomic_v27_layout():
    deploy_readme = (ROOT / "deploy" / "README.md").read_text()
    readme = (ROOT / "README.md").read_text()
    readme_en = (ROOT / "README_en.md").read_text()
    claude = (ROOT / "CLAUDE.md").read_text()
    wrapper_env = (ROOT / "deploy" / "env.wrapper.example").read_text()
    wrapper_plist = (
        ROOT / "deploy" / "com.muggle.cc-remote.wrapper.plist.in"
    ).read_text()
    wrapper_installer = (ROOT / "deploy" / "install-wrapper.sh").read_text()
    relay_env = (ROOT / "deploy" / "env.relay.example").read_text()
    unit = (ROOT / "deploy" / "cc-remote-relay.service").read_text()

    assert "Protocol v27" in deploy_readme
    assert "v14" not in deploy_readme
    for document in (deploy_readme, readme, readme_en):
        assert "v27" in document
        assert "v16" not in document
        assert "v18" not in document
        assert "sudo rsync -a --delete" not in document
        assert "/opt/cc-remote/current" in document
        assert "/opt/cc-remote/releases" in document
    assert "CLAUDE_BIN=/home/youruser/.local/bin/claude" in wrapper_env
    assert "<key>CLAUDE_BIN</key>" in wrapper_plist
    assert "<string>__HOME__/.local/bin/claude</string>" in wrapper_plist
    assert "printf 'CLAUDE_BIN=%s/.local/bin/claude" in wrapper_installer
    assert "daily Claude Code executable is missing" in wrapper_installer
    assert "WEB_STATIC_DIR=/opt/cc-remote/current/web/dist" in relay_env
    assert "WorkingDirectory=/opt/cc-remote/current" in unit
    assert "ExecStart=/opt/cc-remote/current/.venv/bin/python" in unit
    assert "claude-agent-sdk==0.2.128" in claude
    assert "protocol v27" in claude
    assert "0.2.110" not in claude
    assert "protocol v10" not in claude


def test_example_wrapper_buffer_can_hold_one_maximum_websocket_frame():
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    assert int(values["RING_MAX_BYTES"]) >= int(values["WS_MAX_SIZE_BYTES"])
    assert int(values["WRAPPER_INBOX_BYTES"]) >= int(values["WS_MAX_SIZE_BYTES"])
    assert int(values["WRAPPER_SEND_QUEUE_BYTES"]) >= int(values["WS_MAX_SIZE_BYTES"])


def test_caddy_managed_block_preserves_other_sites_and_is_idempotent():
    current = "other.example.com {\n\trespond \"other\"\n}\n"
    site = "cc.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"

    merged = render_managed_caddyfile(current, site, "cc.example.com")
    assert current in merged
    assert merged.count(BEGIN_MARKER) == 1
    assert merged.count(END_MARKER) == 1
    assert merged.count(GLOBAL_BEGIN_MARKER) == 1
    assert merged.count(GLOBAL_END_MARKER) == 1
    assert "read_header 10s" in merged
    assert "read_body 15s" in merged
    assert "write 30s" in merged
    assert "idle 2m" in merged
    assert "max_header_size 64KB" in merged
    assert site.rstrip() in merged
    assert render_managed_caddyfile(merged, site, "cc.example.com") == merged


def test_caddy_managed_block_updates_only_its_marked_region():
    global_prefix = "{\n\temail ops@example.com\n"
    global_suffix = "}\n\n"
    suffix = "\nother.example.com {\n\trespond \"still here\"\n}\n"
    old = (
        global_prefix
        + global_suffix
        + BEGIN_MARKER
        + "\nold.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"
        + END_MARKER
        + "\n"
        + suffix
    )
    site = "new.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"

    merged = render_managed_caddyfile(old, site, "new.example.com")
    assert merged.startswith(global_prefix)
    assert GLOBAL_BEGIN_MARKER in merged
    assert global_suffix + BEGIN_MARKER in merged
    assert merged.endswith(suffix)
    assert "old.example.com" not in merged
    assert "new.example.com" in merged


def test_caddy_managed_block_migrates_exact_legacy_file():
    legacy = "cc.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"
    merged = render_managed_caddyfile(legacy, legacy, "cc.example.com")

    assert merged.startswith("{\n")
    assert GLOBAL_BEGIN_MARKER in merged
    assert merged.endswith(
        f"{BEGIN_MARKER}\n{legacy.rstrip()}\n{END_MARKER}\n")
    assert merged.count("cc.example.com") == 1


def test_caddy_limits_merge_into_shared_global_options_without_clobbering_them():
    current = (
        "# shared config\n"
        "{\n"
        "\temail ops@example.com\n"
        "\tlog {\n"
        "\t\tformat console\n"
        "\t}\n"
        "}\n\n"
        "other.example.com {\n\trespond \"{still shared}\"\n}\n"
    )
    site = "cc.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"

    merged = render_managed_caddyfile(current, site, "cc.example.com")

    assert merged.startswith(
        "# shared config\n{\n\temail ops@example.com\n\tlog {\n"
        "\t\tformat console\n\t}\n"
    )
    assert "}\n\nother.example.com" in merged
    assert "respond \"{still shared}\"" in merged
    assert merged.count(GLOBAL_BEGIN_MARKER) == 1
    assert render_managed_caddyfile(merged, site, "cc.example.com") == merged


def test_caddy_managed_limits_replace_only_their_marked_region():
    current = (
        "{\n"
        "\temail ops@example.com\n"
        f"\t{GLOBAL_BEGIN_MARKER}\n"
        "\tservers {\n\t\tmax_header_size 1MB\n\t}\n"
        f"\t{GLOBAL_END_MARKER}\n"
        "\tadmin localhost:2019\n"
        "}\n\n"
        f"{BEGIN_MARKER}\n"
        "cc.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"
        f"{END_MARKER}\n"
    )
    site = "cc.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"

    merged = render_managed_caddyfile(current, site, "cc.example.com")

    assert "\temail ops@example.com\n" in merged
    assert "\tadmin localhost:2019\n" in merged
    assert "max_header_size 1MB" not in merged
    assert "max_header_size 64KB" in merged
    assert render_managed_caddyfile(merged, site, "cc.example.com") == merged


@pytest.mark.parametrize(
    "current",
    [
        "{\n\tservers {\n\t\tprotocols h1 h2\n\t}\n}\n",
        f"{GLOBAL_BEGIN_MARKER}\nservers {{}}\n{GLOBAL_END_MARKER}\n",
        "{\n\t# BEGIN cc-remote managed server limits\n}\n",
    ],
)
def test_caddy_limits_reject_ambiguous_global_server_config(current):
    site = "cc.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"
    with pytest.raises(CaddyMergeError):
        render_managed_caddyfile(current, site, "cc.example.com")


@pytest.mark.parametrize(
    "current",
    [
        "# BEGIN cc-remote managed site\nmissing-end.example.com {}\n",
        "# END cc-remote managed site\n# BEGIN cc-remote managed site\n",
        "cc.example.com {\n\trespond \"manually managed\"\n}\n",
    ],
)
def test_caddy_managed_block_rejects_ambiguous_existing_config(current):
    site = "cc.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n"
    with pytest.raises(CaddyMergeError):
        render_managed_caddyfile(current, site, "cc.example.com")


def test_relay_service_is_read_only_but_can_read_static_files():
    source = (ROOT / "deploy" / "cc-remote-relay.service").read_text()
    assert "NoNewPrivileges=true" in source
    assert "PrivateTmp=true" in source
    assert "ProtectSystem=strict" in source
    assert "ReadWritePaths=/opt/cc-remote/state" in source
    assert "ProtectHome=true" in source
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in source
    assert "InaccessiblePaths=/opt" not in source


def test_caddy_site_sets_browser_security_headers():
    source = (ROOT / "deploy" / "Caddyfile").read_text()
    for header in (
        "Strict-Transport-Security",
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
    ):
        assert header in source
    assert "frame-ancestors 'none'" in source
    assert "connect-src 'self' wss://cc-remote.example.com" in source
    assert "request_body @login" in source
    assert "max_size 4KB" in source
    assert " ws:" not in source


@pytest.mark.parametrize("template", ["Caddyfile", "Caddyfile.insecure"])
def test_caddy_image_policy_allows_only_image_blob_urls(template):
    source = (ROOT / "deploy" / template).read_text()
    match = re.search(r'Content-Security-Policy "([^"]+)"', source)
    assert match is not None
    directives = {
        parts[0]: parts[1:]
        for directive in match.group(1).split(";")
        if (parts := directive.strip().split())
    }
    assert "blob:" in directives["img-src"]
    assert all(
        "blob:" not in sources
        for name, sources in directives.items()
        if name != "img-src"
    )
