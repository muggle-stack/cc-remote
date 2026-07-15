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


def test_setup_script_is_valid_shell_and_keeps_safe_install_order():
    script = ROOT / "deploy" / "setup-vps.sh"
    transaction = ROOT / "deploy" / "setup_transaction.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    subprocess.run(["bash", "-n", str(transaction)], check=True)
    source = script.read_text()

    validate = source.index('caddy validate --config "$CADDY_CANDIDATE"')
    backup = source.index('cp -a "$CADDYFILE" "$CADDY_BACKUP"')
    install = source.index(
        'install -o root -g root -m 0644 "$CADDY_CANDIDATE" "$CADDYFILE"'
    )
    assert validate < backup < install
    assert "require_secret LOGIN_PASSWORD 16" in source
    assert "require_secret SESSION_SECRET 32" in source
    assert "require_secret WRAPPER_TOKEN 32" in source
    assert 'TARGET="${TARGET_INPUT,,}"' in source
    assert 'PUBLIC_SCHEME=http' in source
    assert 'CADDY_TEMPLATE="$APPDIR/deploy/Caddyfile.insecure"' in source
    assert "address.version == 4 and address.is_global" in source
    assert "not address.is_multicast" in source
    assert '"$CONFIGURED_ORIGIN" == "$PUBLIC_SCHEME://$TARGET"' in source
    assert '[[ "$CONFIGURED_RELAY_HOST" == "127.0.0.1" ]]' in source
    assert '[[ "$CONFIGURED_RELAY_PORT" == "8765" ]]' in source
    assert '[[ "$CONFIGURED_STATIC_DIR" == "$APPDIR/web/dist" ]]' in source
    assert 'chmod 0600 "$ENV_FILE"' in source
    assert "systemctl restart cc-remote-relay" in source
    assert 'python3 "$APPDIR/deploy/caddy_managed_block.py"' in source
    assert 'source "$APPDIR/deploy/setup_transaction.sh"' in source
    assert "UNIT_BACKUP=" in source
    assert 'cp -a "$RELAY_UNIT_FILE" "$UNIT_BACKUP"' in source
    assert "RELAY_SERVICE_TOUCHED=1" in source


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


def test_setup_does_not_make_network_service_owner_of_root_executed_code():
    source = (ROOT / "deploy" / "setup-vps.sh").read_text()
    assert 'chown -R root:ccremote "$APPDIR"' in source
    assert 'chown root:ccremote "$ENV_FILE"' in source
    assert 'chmod 0640 "$ENV_FILE"' in source
    assert 'chown -R ccremote:ccremote "$APPDIR"' not in source
    assert "sudo -u ccremote" not in source
    assert 'VENV_STAGE="$(mktemp -d "$APPDIR/.venv.new.XXXXXX")"' in source
    assert 'mv "$VENV_STAGE" "$APPDIR/.venv"' in source
    transaction_source = (ROOT / "deploy" / "setup_transaction.sh").read_text()
    assert "rollback_venv" in transaction_source
    assert "rollback_deployment" in transaction_source
    assert 'cp -a "$CADDY_BACKUP" "$CADDYFILE"' in transaction_source
    assert 'cp -a "$UNIT_BACKUP" "$RELAY_UNIT_FILE"' in transaction_source
    assert "systemctl daemon-reload" in transaction_source
    assert "previous relay passed /healthz" in transaction_source
    assert "--require-hashes" in source
    assert "--only-binary=:all:" in source
    assert 'requirements.lock' in source
    assert "apt-get install -y" in source and "gnupg" in source
    assert "command -v gpg" in source
    lock = (ROOT / "requirements.lock").read_text()
    assert "--hash=sha256:" in lock
    assert "cryptography==" in lock
    assert "Python 3.10 or newer is required" in source


def test_injected_post_swap_failure_restores_venv_caddy_and_relay_unit(tmp_path):
    appdir = tmp_path / "app"
    current_venv = appdir / ".venv"
    previous_venv = appdir / ".venv.previous"
    current_venv.mkdir(parents=True)
    previous_venv.mkdir()
    (current_venv / "marker").write_text("new")
    (previous_venv / "marker").write_text("old")

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
VENV_BACKUP="$APPDIR/.venv.previous"
VENV_STAGE=""
VENV_SWAPPED=1
DEPLOY_READY=0
CADDYFILE="$3"
CADDY_BACKUP="$4"
CADDY_SITE=""
CADDY_CANDIDATE=""
CADDY_CHANGED=1
CADDY_HAD_CONFIG=1
CADDY_SERVICE_TOUCHED=1
RELAY_UNIT_FILE="$5"
UNIT_BACKUP="$6"
UNIT_CHANGED=1
UNIT_HAD_FILE=1
RELAY_SERVICE_TOUCHED=1
ROLLBACK_DONE=0
SYSTEMCTL_LOG="$7"
systemctl() {
  printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
  return 0
}
curl() {
  printf '%s\n' "$*" >> "$CURL_LOG"
  return 0
}
trap cleanup EXIT
false  # injected relay readiness failure after every staged swap
'''
    result = subprocess.run(
        [
            "bash", "-c", harness, "rollback-test",
            str(ROOT / "deploy" / "setup_transaction.sh"),
            str(appdir), str(caddyfile), str(caddy_backup),
            str(unit), str(unit_backup), str(systemctl_log),
        ],
        env={**os.environ, "CURL_LOG": str(tmp_path / "curl.log")},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert (appdir / ".venv" / "marker").read_text() == "old"
    assert not previous_venv.exists()
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


def test_web_build_manifest_matches_both_protocol_implementations():
    manifest = json.loads(
        (ROOT / "web" / "public" / "cc-remote-build.json").read_text())
    from cc_remote.protocol import PROTOCOL_VERSION

    ts = (ROOT / "web" / "src" / "protocol.ts").read_text()
    match = re.search(r"PROTOCOL_VERSION\s*=\s*(\d+)", ts)
    assert match
    assert manifest["protocol"] == PROTOCOL_VERSION == int(match.group(1))


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
