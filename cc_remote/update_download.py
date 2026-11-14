"""Bounded, checksum-bound release downloads shared by update attempts."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from cc_remote.update_errors import DownloadError, UpdateError, download_reason, endpoint
from cc_remote.log import logger


CACHE_DIRECTORY = "download-cache"
CACHE_TTL = 24 * 60 * 60
CACHE_LIMIT = 512 * 1024 * 1024
_SUFFIXES = (".headers", ".part", ".tar.gz", ".json")
_ASSET = re.compile(r"cc-remote-(relay|wrapper)-v[0-9]+\.[0-9]+\.[0-9]+-(linux|darwin)-(arm64|x86_64)\.tar\.gz")


def _regular(path: Path, owner: int) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise UpdateError("unsafe release download cache file")
    return info.st_size


def _etag(headers: Path) -> str | None:
    # A redirect can introduce several header blocks. Only the final response
    # describes the bytes in the partial file. Signed Location values are never
    # retained as download identities or surfaced to clients.
    if not headers.exists() or headers.stat().st_size > 65536:
        return None
    value = None
    for line in headers.read_text(errors="replace").splitlines():
        if line.startswith("HTTP/"):
            value = None
        if line.lower().startswith("etag:"):
            candidate = line[5:].strip()
            if (len(candidate) <= 512 and candidate.startswith('"') and candidate.endswith('"')
                    and all(32 <= ord(char) < 127 for char in candidate)):
                value = candidate
    return value


def download_file(url: str, destination: Path, limit: int, *, resume: bool = False,
                  headers: Path | None = None, lock_descriptor: int | None = None) -> None:
    owner = destination.parent.stat().st_uid
    for attempt in range(3):
        offset = _regular(destination, owner) if resume else 0
        if offset > limit:
            destination.unlink()
            offset = 0
        arguments = [
            "curl", "--fail", "--location", "--proto", "=https,file", "--proto-redir", "=https",
            "--tlsv1.2", "--connect-timeout", "15", "--max-time", "300", "--max-filesize", str(limit),
            "--speed-limit", "1", "--speed-time", "15",
            "--silent", "--show-error", "--write-out", "%{http_code}", "--output", str(destination),
        ]
        etag = None
        if headers is not None:
            _regular(headers, owner)
            etag = _etag(headers)
            if offset and etag:
                arguments += ["--header", f"If-Range: {etag}"]
            arguments += ["--dump-header", str(headers)]
        if offset:
            arguments += ["--continue-at", str(offset)]
        # A surviving curl keeps the same cache lock if its Python controller
        # exits. Its replacement cannot append to the file concurrently.
        result = subprocess.run([*arguments, url], check=False, capture_output=True, text=True,
                                pass_fds=() if lock_descriptor is None else (lock_descriptor,))
        size = _regular(destination, owner)
        if headers is not None and _regular(headers, owner) > 65536:
            headers.unlink()
        if headers is not None and etag and size and _etag(headers) is None:
            # An error response must not replace the validator of bytes that
            # were accepted before the outage.
            headers.write_text(f"ETag: {etag}\n")
        if size > limit:
            destination.unlink()
            raise DownloadError("release download exceeds the permitted size")
        status = result.stdout.strip()
        if result.returncode == 0:
            if not destination.is_file():
                raise DownloadError("download did not produce a regular file")
            return
        # curl refuses to append a full 200 response to an existing partial.
        # Changed entities, ignored Range, and invalid offsets restart cleanly.
        if offset and (result.returncode in {33, 36} or status == "416"):
            destination.unlink(missing_ok=True)
            if headers is not None:
                headers.unlink(missing_ok=True)
            if attempt < 2:
                continue
        transient = (result.returncode in {5, 6, 7, 18, 28, 35, 52, 55, 56}
                     or result.returncode == 22 and status in {"408", "429", "500", "502", "503", "504"})
        if not transient or attempt == 2:
            reason = download_reason(result.returncode, status)
            raise DownloadError(
                f"download failed for {destination.name} from {endpoint(url)}: {reason}; no service was changed",
                transient=transient,
            )
        time.sleep(0.5 * (attempt + 1))


class DownloadCache:
    def __init__(self, root: Path):
        self.root = root
        self.owner = root.stat().st_uid
        self.folder = root / CACHE_DIRECTORY

    @contextmanager
    def lock(self, *, blocking: bool = True):
        self.folder.mkdir(mode=0o700, exist_ok=True)
        info = self.folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner
                or info.st_mode & 0o077 or self.folder.resolve() != self.folder):
            raise UpdateError("unsafe release download cache directory")
        descriptor = os.open(self.folder / "cache.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner or info.st_mode & 0o077:
                raise UpdateError("unsafe release download cache lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def identity(url: str, name: str, checksum: str) -> tuple[str, dict]:
        if not _ASSET.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise UpdateError("invalid release download identity")
        source = urlsplit(url)
        if (source.scheme not in {"https", "file"} or source.username or source.password
                or source.query or source.fragment or source.path.rsplit("/", 1)[-1] != name):
            raise UpdateError("invalid release download source")
        binding = {"schema": 1, "source": url, "name": name, "sha256": checksum}
        key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        return key, binding

    def _write(self, key: str, binding: dict) -> None:
        from cc_remote.update_jobs import write_json

        write_json(self.folder / f"{key}.json", {**binding, "accessed_at": time.time()}, mode=0o600)

    def _remove(self, key: str) -> None:
        for suffix in _SUFFIXES:
            path = self.folder / (key + suffix)
            _regular(path, self.owner)
        for suffix in _SUFFIXES:
            (self.folder / (key + suffix)).unlink(missing_ok=True)

    def _binding(self, path: Path) -> dict:
        if _regular(path, self.owner) > 8192:
            raise UpdateError("oversized release download cache receipt")
        try:
            binding = json.loads(path.read_text())
            observed, _ = self.identity(binding["source"], binding["name"], binding["sha256"])
            accessed = binding.get("accessed_at")
            if (binding.get("schema") != 1 or observed != path.stem or type(accessed) not in (int, float)
                    or not math.isfinite(accessed) or accessed < 0):
                raise ValueError("identity or timestamp mismatch")
        except (ValueError, KeyError, TypeError) as exc:
            raise UpdateError("invalid release download cache receipt") from exc
        return binding

    def _prune(self, *, keep: str | None = None, reserve: int = 0) -> None:
        self._prune_stages()
        for path in self.folder.glob('.update-*'):
            if re.fullmatch(r'\.update-[a-z0-9_]{8}', path.name) and _regular(path, self.owner) <= 32768:
                path.unlink()
        entries = []
        for path in self.folder.glob("*.json"):
            key = path.stem
            if not re.fullmatch(r"[0-9a-f]{64}", key):
                continue
            try:
                binding = self._binding(path)
            except UpdateError:
                self._remove(key)
                continue
            size = sum(_regular(self.folder / (key + suffix), self.owner) for suffix in _SUFFIXES)
            accessed = binding.get("accessed_at", 0)
            if key != keep and accessed < time.time() - CACHE_TTL:
                self._remove(key)
            else:
                entries.append((accessed, key, size))
        total = sum(size for _, key, size in entries if key != keep)
        count = len(entries) + int(keep is not None and all(key != keep for _, key, _ in entries))
        for _, key, size in sorted(entries):
            if key != keep and (total + reserve > CACHE_LIMIT or count > 8):
                self._remove(key)
                total -= size
                count -= 1

    @contextmanager
    def staging(self):
        from cc_remote.update_jobs import write_json
        from cc_remote.wrapper.process_scan import process_identity
        process = process_identity(os.getpid())
        if process is None:
            raise UpdateError("cannot identify release download worker")
        with self.lock():
            self._prune()
            temporary = tempfile.TemporaryDirectory(prefix="extract-", dir=self.folder)
            stage = Path(temporary.name)
            receipt = self.folder / f'{stage.name}.json'
            write_json(receipt, {"schema": 1, "root": str(self.root), "name": stage.name,
                                 "worker_pid": os.getpid(), "worker_start": process.start_ticks}, mode=0o600)
        try:
            with temporary:
                yield stage
        finally:
            if not stage.exists():
                receipt.unlink(missing_ok=True)

    def _prune_stages(self) -> None:
        from cc_remote.update_jobs import read_json, worker_alive
        for path in self.folder.glob("extract-*"):
            if not re.fullmatch(r"extract-[a-z0-9_]{8}", path.name):
                continue
            receipt = self.folder / f'{path.name}.json'
            try:
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner or info.st_mode & 0o077:
                    raise UpdateError("unsafe release extraction directory")
                try:
                    record = read_json(receipt, self.owner)
                except FileNotFoundError:
                    if not any(path.iterdir()):
                        path.rmdir()
                    continue
                if record.get("schema") != 1 or record.get("root") != str(self.root) or record.get("name") != path.name:
                    raise UpdateError("release extraction directory has no matching owner")
                if (type(record.get('worker_pid')) is not int or record['worker_pid'] < 1
                        or type(record.get('worker_start')) is not int or record['worker_start'] < 1):
                    raise UpdateError("release extraction process identity is unavailable")
                if worker_alive(record):
                    continue
                parent = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    shutil.rmtree(path.name, dir_fd=parent)
                finally:
                    os.close(parent)
                receipt.unlink()
            except (OSError, ValueError):
                logger('cc_remote.updates').warning('unverified update extraction retained for inspection')
        # The outside receipt survives an interrupted tree deletion. A crash
        # after its last directory removal can leave only this small receipt.
        for receipt in self.folder.glob('extract-*.json'):
            if not re.fullmatch(r'extract-[a-z0-9_]{8}\.json', receipt.name):
                continue
            try:
                record = read_json(receipt, self.owner)
                if (record.get('schema') == 1 and record.get('root') == str(self.root)
                        and record.get('name') == receipt.stem and not (self.folder / receipt.stem).exists()):
                    receipt.unlink()
            except (OSError, ValueError):
                pass

    def checksum(self, url: str, name: str) -> str | None:
        if not self.folder.exists():
            return None
        with self.lock():
            self._prune()
            for path in self.folder.glob("*.json"):
                if re.fullmatch(r"[0-9a-f]{64}", path.stem):
                    binding = self._binding(path)
                    if binding["source"] == url and binding["name"] == name:
                        return binding["sha256"]
        return None

    def prune(self, *, blocking: bool = True) -> None:
        if self.folder.exists():
            with self.lock(blocking=blocking):
                self._prune()

    def discard(self, url: str, name: str, checksum: str) -> None:
        key, _ = self.identity(url, name, checksum)
        with self.lock():
            self._remove(key)

    def discard_version(self, version: str, *, blocking: bool = True) -> None:
        if not self.folder.exists():
            return
        with self.lock(blocking=blocking):
            for path in self.folder.glob("*.json"):
                if not re.fullmatch(r"[0-9a-f]{64}", path.stem):
                    continue
                binding = self._binding(path)
                key = path.stem
                if f"-v{version}-" in binding["name"]:
                    self._remove(key)

    def fetch(self, url: str, name: str, checksum: str, limit: int) -> Path:
        key, binding = self.identity(url, name, checksum)
        with self.lock() as descriptor:
            self._prune(keep=key, reserve=limit)
            self._write(key, binding)
            archive = self.folder / f"{key}.tar.gz"
            partial = self.folder / f"{key}.part"
            headers = self.folder / f"{key}.headers"
            for path in (archive, partial, headers):
                _regular(path, self.owner)
            if archive.exists():
                if archive.stat().st_size <= limit and self._matches(archive, checksum):
                    return archive
                archive.unlink()
            # A download may have completed immediately before its worker died.
            if partial.exists() and partial.stat().st_size <= limit and self._matches(partial, checksum):
                partial.replace(archive)
                headers.unlink(missing_ok=True)
                return archive
            for attempt in range(2):
                try:
                    download_file(url, partial, limit, resume=True, headers=headers, lock_descriptor=descriptor)
                except DownloadError as exc:
                    if not exc.transient:
                        self._remove(key)
                    raise
                if self._matches(partial, checksum):
                    partial.replace(archive)
                    headers.unlink(missing_ok=True)
                    self._write(key, binding)
                    return archive
                partial.unlink(missing_ok=True)
                headers.unlink(missing_ok=True)
                if attempt == 0:
                    continue
            self._remove(key)
            raise DownloadError("release SHA-256 verification failed; no service was changed")

    @staticmethod
    def _matches(path: Path, checksum: str) -> bool:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == checksum
