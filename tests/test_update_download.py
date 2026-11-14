"""Real curl/TLS downloads with interrupted bodies, ranges and cache recovery."""
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import pwd
import ssl
import subprocess
import sys
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from cc_remote import update_download as downloads
from cc_remote.update_errors import DownloadError, UpdateError


NAME = "cc-remote-relay-v5.3.28-linux-x86_64.tar.gz"
PAYLOAD = bytes(range(256)) * 512
CHECKSUM = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def source(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
                   .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                  x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert = tmp_path / "certificate.pem"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private = tmp_path / "key.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    state = {"requests": [], "fail": False, "interrupt": False, "ignore_range": False,
             "etag": '"original"', "payload": PAYLOAD, "checksums": None, "range_416": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requested = self.headers.get("Range")
            state["requests"].append((requested, self.headers.get("If-Range")))
            if self.path.endswith('/SHA256SUMS') and state['checksums'] is not None:
                data = state['checksums'].encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if state["fail"]:
                self.send_response(503)
                self.end_headers()
                return
            data = state["payload"]
            offset = int(requested.removeprefix("bytes=").removesuffix("-")) if requested else 0
            if state["ignore_range"] or self.headers.get("If-Range", state["etag"]) != state["etag"]:
                offset = 0
            if offset >= len(data) or requested and state['range_416']:
                state['range_416'] = False
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            self.send_response(206 if offset else 200)
            self.send_header("ETag", state["etag"])
            self.send_header("Content-Length", str(len(data) - offset))
            if offset:
                self.send_header("Content-Range", f"bytes {offset}-{len(data)-1}/{len(data)}")
            self.end_headers()
            if state["interrupt"]:
                state["interrupt"] = False
                state["fail"] = True
                self.wfile.write(data[offset:offset + 4096])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(data[offset:])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("CURL_CA_BUNDLE", str(cert))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setattr(downloads.time, "sleep", lambda _: None)
    try:
        yield f"https://127.0.0.1:{server.server_port}/{NAME}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_download_resumes_after_a_new_worker_and_reuses_verified_bytes(tmp_path, source):
    url, state = source
    root = tmp_path / "install"
    root.mkdir()
    state["interrupt"] = True
    cache = downloads.DownloadCache(root)
    with pytest.raises(DownloadError) as failure:
        cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    assert failure.value.transient
    assert next(cache.folder.glob("*.part")).read_bytes() == PAYLOAD[:4096]
    state["fail"] = False
    path = downloads.DownloadCache(root).fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    assert path.read_bytes() == PAYLOAD
    assert state["requests"][-1][0] == "bytes=4096-"
    count = len(state["requests"])
    assert cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD)) == path
    assert len(state["requests"]) == count
    assert not list(cache.folder.glob("*.part"))


@pytest.mark.parametrize("change", ["ignore_range", "etag"])
def test_changed_or_unsupported_range_restarts_without_appending_full_body(tmp_path, source, change):
    url, state = source
    cache = downloads.DownloadCache(tmp_path)
    state["interrupt"] = True
    with pytest.raises(DownloadError):
        cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    state["fail"] = False
    state[change] = True if change == "ignore_range" else '"replacement"'
    assert cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD)).read_bytes() == PAYLOAD
    assert state["requests"][-2][0] == "bytes=4096-"
    assert state["requests"][-1][0] is None


def test_checksum_failure_removes_corrupt_archive_and_partial(tmp_path, source):
    url, state = source
    state["payload"] = b"wrong release bytes"
    cache = downloads.DownloadCache(tmp_path)
    with pytest.raises(DownloadError, match="SHA-256"):
        cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    assert not list(cache.folder.glob("*.part"))
    assert not list(cache.folder.glob("*.tar.gz"))
    assert not list(cache.folder.glob("*.json"))


def test_range_416_restarts_and_verifies_a_complete_file(tmp_path, source):
    url, state = source
    cache = downloads.DownloadCache(tmp_path)
    state['interrupt'] = True
    with pytest.raises(DownloadError):
        cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    state.update(fail=False, range_416=True)
    assert cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD)).read_bytes() == PAYLOAD
    assert state['requests'][-2][0] == 'bytes=4096-'
    assert state['requests'][-1][0] is None


def test_complete_partial_is_verified_after_crash_without_another_download(tmp_path, source):
    url, state = source
    cache = downloads.DownloadCache(tmp_path)
    archive = cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    archive.replace(archive.with_suffix("").with_suffix(".part"))
    count = len(state["requests"])
    assert cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD)).read_bytes() == PAYLOAD
    assert len(state["requests"]) == count


def test_cached_file_is_rechecked_before_reuse(tmp_path, source):
    url, state = source
    cache = downloads.DownloadCache(tmp_path)
    archive = cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    archive.write_bytes(b"changed cache bytes")
    assert cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD)).read_bytes() == PAYLOAD
    assert len(state["requests"]) == 2


def test_download_subprocess_inherits_the_cache_lock(tmp_path, source, monkeypatch):
    url, _ = source
    native_run = downloads.subprocess.run
    descriptors = []
    def run(arguments, **kwargs):
        descriptors.extend(kwargs.get('pass_fds', ()))
        return native_run(arguments, **kwargs)
    monkeypatch.setattr(downloads.subprocess, 'run', run)
    cache = downloads.DownloadCache(tmp_path)
    cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    assert len(descriptors) == 1


def test_cache_expires_and_cancellation_discards_only_the_selected_version(tmp_path, source):
    url, _ = source
    cache = downloads.DownloadCache(tmp_path)
    archive = cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    receipt = next(cache.folder.glob("*.json"))
    value = json.loads(receipt.read_text())
    receipt.write_text(json.dumps({**value, "accessed_at": 1}))
    cache.prune()
    assert not archive.exists()
    archive = cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    cache.discard_version("5.3.27")
    assert archive.exists()
    cache.discard_version("5.3.28")
    assert not archive.exists()


def test_cache_enforces_aggregate_capacity(tmp_path, source, monkeypatch):
    url, _ = source
    cache = downloads.DownloadCache(tmp_path)
    archive = cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    monkeypatch.setattr(downloads, "CACHE_LIMIT", 1)
    cache.prune()
    assert not archive.exists()


def test_corrupt_cache_receipt_is_reclaimed_before_a_fresh_download(tmp_path, source):
    url, _ = source
    cache = downloads.DownloadCache(tmp_path)
    cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    receipt = next(cache.folder.glob('*.json'))
    receipt.write_text('{')
    assert cache.checksum(url, NAME) is None
    assert cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD)).read_bytes() == PAYLOAD


@pytest.mark.parametrize("entry", ["directory", "archive", "partial", "receipt", "lock"])
def test_cache_rejects_symlink_aliases_without_modifying_the_target(tmp_path, source, entry):
    url, _ = source
    root = tmp_path / "root"
    root.mkdir()
    cache = downloads.DownloadCache(root)
    key, _ = cache.identity(url, NAME, CHECKSUM)
    victim = tmp_path / "private"
    victim.write_text("keep")
    if entry == "directory":
        cache.folder.symlink_to(tmp_path)
    else:
        cache.folder.mkdir(mode=0o700)
        suffix = {"archive": ".tar.gz", "partial": ".part", "receipt": ".json"}.get(entry)
        path = cache.folder / (key + suffix if suffix else "cache.lock")
        path.symlink_to(victim)
    with pytest.raises((UpdateError, OSError)):
        cache.fetch(url, NAME, CHECKSUM, len(PAYLOAD))
    assert victim.read_text() == "keep"


def test_live_extraction_is_retained_and_normal_exit_removes_it(tmp_path):
    cache = downloads.DownloadCache(tmp_path)
    with cache.staging() as stage:
        (stage / 'partial-extraction').write_text('working')
        cache.prune()
        assert stage.exists()
    assert not stage.exists()


def test_crashed_extraction_is_reclaimed_even_after_interrupted_cleanup(tmp_path, monkeypatch):
    root = str(tmp_path)
    repository = str(Path(__file__).resolve().parents[1])
    program = (
        'import os,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
        'from cc_remote.update_download import DownloadCache\n'
        'with DownloadCache(Path(sys.argv[2])).staging() as stage:\n'
        ' (stage/"partial-extraction").write_text("incomplete")\n'
        ' print(stage,flush=True)\n'
        ' os._exit(23)\n'
    )
    result = subprocess.run([sys.executable, '-I', '-c', program, repository, root], capture_output=True, text=True, timeout=10)
    assert result.returncode == 23, result.stderr
    stage = Path(result.stdout.strip())
    assert stage.is_dir()
    victim = tmp_path / 'private'
    victim.write_text('keep')
    (stage / 'external-link').symlink_to(victim)
    cache = downloads.DownloadCache(tmp_path)
    receipt = cache.folder / f'{stage.name}.json'
    native_remove = downloads.shutil.rmtree
    def interrupted_remove(*_args, **_kwargs):
        (stage / 'partial-extraction').unlink()
        raise OSError('cleanup interrupted')
    with monkeypatch.context() as fault:
        fault.setattr(downloads.shutil, 'rmtree', interrupted_remove)
        cache.prune()
    assert stage.exists() and receipt.exists()
    assert downloads.shutil.rmtree is native_remove
    cache.prune()
    assert not stage.exists() and victim.read_text() == 'keep'
    assert not receipt.exists()


def test_unfinished_extraction_receipt_is_reclaimed_but_unknown_files_are_retained(tmp_path):
    cache = downloads.DownloadCache(tmp_path)
    with cache.lock():
        stage = cache.folder / 'extract-abcdefgh'
        stage.mkdir(mode=0o700)
        interrupted_receipt = cache.folder / '.update-abcdefgh'
        interrupted_receipt.write_text('{')
        unknown = cache.folder / 'extract-ijklmnop'
        unknown.mkdir(mode=0o700)
        (unknown / 'operator-file').write_text('keep')
    cache.prune()
    assert not stage.exists()
    assert not interrupted_receipt.exists()
    assert (unknown / 'operator-file').read_text() == 'keep'


def test_ui_worker_downloads_resumes_validates_and_installs_after_outage(tmp_path, source, monkeypatch):
    from cc_remote import update as updater
    from cc_remote.update_jobs import UpdateJobs
    from deploy import ui_update_job as launcher
    from tests.test_update import _installation, _bundle

    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, name = _bundle(tmp_path, installation)
    url, state = source
    payload = (mirror / name).read_bytes()
    state.update(payload=payload, checksums=(mirror / 'SHA256SUMS').read_text(), interrupt=True)
    # Use a larger valid release so the interrupted response cannot accidentally
    # contain the complete small fixture archive.
    state['payload'] = payload + b'\0' * 8192
    digest = hashlib.sha256(state['payload']).hexdigest()
    state['checksums'] = f'{digest}  {name}\n'
    monkeypatch.setattr(updater, 'release_base', lambda *_: url.rsplit('/', 1)[0])
    account = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(launcher, 'installation', lambda _: (installation, account))
    monkeypatch.setattr(launcher, 'host_platform', lambda: ('darwin', 'arm64'))
    monkeypatch.setattr(launcher, 'retire', lambda *_: None)
    for variable in ('CC_REMOTE_UI_UPDATE_ID', 'CC_REMOTE_UI_ALLOW_RELAY_UPDATE',
                     'CC_REMOTE_GITHUB_REPOSITORY', 'CC_REMOTE_RELEASE_BASE_URL'):
        monkeypatch.setenv(variable, os.environ.get(variable, ''))
        monkeypatch.delenv(variable)
    launcher.configure(installation.root, enabled=True)
    store = UpdateJobs(installation.root)
    identity = 'c' * 32
    store.create(identity, '4.0.1', '4.0.0')
    waits = []
    def recover_network(_delay):
        if store.active()['phase'] == 'waiting_network':
            waits.append(True)
            state['fail'] = False
    monkeypatch.setattr(launcher.time, 'sleep', recover_network)
    launcher.run(installation.root, identity)
    assert waits and store.active()['phase'] == 'succeeded'
    assert marker.exists()
    assert any(requested == 'bytes=4096-' for requested, _ in state['requests'])
    assert not list((installation.root / 'download-cache').glob('*.part'))
    assert not list((installation.root / 'download-cache').glob('*.tar.gz'))
    assert not list((installation.root / 'download-cache').glob('extract-*'))
