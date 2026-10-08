"""Preview or retire explicitly inventoried deployment artifacts, never by age.

The private inventory supplies transaction provenance, the complete rollback
set, service dependencies and read-only acceptance commands. Live references
are checked independently. See deploy/README.md#deployment-backup-retention.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.install_lock import acquire_install_lock, verify_install_lock

JOURNAL = ".cleanup-transaction.json"
MAX_JSON = 1024 * 1024
MAX_SCAN = 32 * 1024 * 1024
MAX_DEPENDENCY_ENTRIES = 250_000
COMPLETE = {"committed", "complete", "deployed_verified", "ready"}


class CleanupError(ValueError):
    pass


def read_json(path: Path) -> tuple[dict, str]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
            raise CleanupError("unsafe or oversized inventory/transaction record")
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise CleanupError("oversized inventory/transaction record")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise CleanupError("inventory/transaction record must be an object")
    return data, hashlib.sha256(raw).hexdigest()


def absolute_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise CleanupError("expected an absolute path")
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.normpath(value)):
        raise CleanupError("paths must be absolute and normalized")
    # Reject symlinks in ancestors too. A symlink *inside* an artifact is removed
    # as a link by fd-based rmtree; it is never recursively followed.
    if path.resolve() != path:
        raise CleanupError(f"symlink/alias is not a cleanup boundary: {path}")
    return path


def identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise CleanupError(f"not a regular artifact: {path}")
    return info.st_dev, info.st_ino


def overlaps(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def load_inventory(path: Path) -> dict:
    data, digest = read_json(path)
    required = {"schema", "installation_root", "current_release", "rollback_paths",
                "cleanup_roots", "candidates", "transactions", "checks"}
    if (data.keys() - required - {"protected_paths", "service_context"} or required - data.keys()
            or data["schema"] != 1):
        raise CleanupError("invalid cleanup inventory schema")
    if "service_context" in data:
        context = data["service_context"]
        if (not isinstance(context, dict) or "home" not in context
                or context.keys() - {"home", "linux_service"}):
            raise CleanupError("invalid service discovery context")
        if not absolute_path(context["home"]).is_dir():
            raise CleanupError("service discovery HOME is unavailable")
        if "linux_service" in context:
            from deploy.linux_service import validate
            validate(context["linux_service"])
            if context["linux_service"]["home"] != context["home"]:
                raise CleanupError("service discovery HOME differs from the service binding")
    for key in ["rollback_paths", "cleanup_roots", "candidates", "transactions", "checks"]:
        if not isinstance(data[key], list) or not 0 < len(data[key]) <= 128:
            raise CleanupError(f"{key} must be a nonempty bounded list")
    root = absolute_path(data["installation_root"])
    if os.geteuid() != 0 and root.stat().st_uid != os.geteuid():
        raise CleanupError("shared installations require root process visibility")
    current = absolute_path(data["current_release"])
    if current.parent != root / "releases":
        raise CleanupError("current release must be a direct child of installation releases")
    rollback = [absolute_path(p) for p in data["rollback_paths"]]
    if sum(p.parent == root / "releases" for p in rollback) != 1 or current in rollback:
        raise CleanupError("inventory must retain exactly one previous release and its rollback files")
    protected = data.get("protected_paths", [])
    if not isinstance(protected, list) or len(protected) > 128:
        raise CleanupError("invalid protected paths")
    protected = [current, *rollback, *(absolute_path(p) for p in protected)]
    for p in protected:
        identity(p)
    roots = [absolute_path(p) for p in data["cleanup_roots"]]
    for p in roots:
        if (not p.is_dir() or p in {Path("/"), Path.home(), root}
                or p.name in {".codex", ".claude", ".cc-remote"}):
            raise CleanupError("cleanup roots must be dedicated artifact directories")
        if os.geteuid() != 0 and p.stat().st_uid != os.geteuid():
            raise CleanupError("shared artifact roots require root process visibility")
    candidates = [absolute_path(p) for p in data["candidates"]]
    if len(set(candidates)) != len(candidates):
        raise CleanupError("duplicate cleanup candidate")
    for p in candidates:
        if p.parent not in roots or any(p != q and overlaps(p, q) for q in candidates):
            raise CleanupError("candidates must be non-overlapping direct children of cleanup roots")
    for check in data["checks"]:
        if (not isinstance(check, dict) or set(check) != {"name", "argv", "cwd"}
                or not isinstance(check["name"], str) or not check["name"]
                or not isinstance(check["argv"], list) or not check["argv"]
                or not all(isinstance(s, str) and s and "\0" not in s for s in check["argv"])
                or not os.path.isabs(check["argv"][0])):
            raise CleanupError("checks require a name, absolute executable argv and stable cwd")
        cwd = absolute_path(check["cwd"])
        if not cwd.is_dir() or any(cwd == p or cwd.is_relative_to(p) for p in candidates):
            raise CleanupError("acceptance check cwd must be outside cleanup candidates")
    transactions = []
    for entry in data["transactions"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "field", "equals"}
                or entry["field"] not in {"phase", "status"} or entry["equals"] not in COMPLETE):
            raise CleanupError("transaction requires a known completed phase/status")
        record = absolute_path(entry["path"])
        value, checksum = read_json(record)
        if value.get(entry["field"]) != entry["equals"]:
            raise CleanupError(f"unfinished or unknown transaction: {record}")
        transactions.append((record, checksum))
        protected.append(record)
    protected.append(path.resolve())
    protected_identities = {p: identity(p) for p in protected}
    protected.extend([root / JOURNAL, root / ".update.lock"])
    return dict(data, root=root, current=current, protected=protected, candidates=candidates,
                transactions=transactions, digest=digest, inventory=path.resolve(),
                protected_identities=protected_identities)


def check_bindings(plan: dict) -> None:
    root = plan["root"]
    if not (root / "current").is_symlink() or (root / "current").resolve(strict=True) != plan["current"]:
        raise CleanupError("active release changed or current is not a managed symlink")
    if read_json(plan["inventory"])[1] != plan["digest"]:
        raise CleanupError("inventory changed during cleanup")
    for path, checksum in plan["transactions"]:
        if read_json(path)[1] != checksum:
            raise CleanupError("transaction changed during cleanup")
    for path, expected in plan["protected_identities"].items():
        if identity(absolute_path(str(path))) != expected:
            raise CleanupError(f"protected rollback/dependency replaced: {path}")


def scan_command(argv: list[str]) -> bytes:
    # Never print process argv or lsof output: either can contain private paths
    # or arguments. Only matched PID/reference categories enter the report.
    result = subprocess.run(argv, capture_output=True, timeout=30, check=False)
    if result.returncode or result.stderr.strip() or len(result.stdout) > MAX_SCAN:
        raise CleanupError("process reference scan incomplete; no deletion is safe")
    return result.stdout


def parse_lsof(raw: bytes) -> list[tuple[int, str, Path]]:
    pid = None
    descriptor = None
    references = []
    for field in raw.split(b"\0"):
        field = field.lstrip(b"\n")
        if not field:
            continue
        tag, value = field[:1], field[1:]
        if tag == b"p":
            pid = int(value)
            descriptor = None
        elif tag == b"f":
            descriptor = os.fsdecode(value)
            if descriptor in {"NOFD", "err"}:
                raise CleanupError("process files are not fully observable")
        elif tag == b"n":
            if pid is None or descriptor is None:
                raise CleanupError("incomplete lsof process/file record")
            name = os.fsdecode(value)
            if "Permission denied" in name or "Operation not permitted" in name:
                raise CleanupError("process files are not fully observable")
            if name.startswith("/"):
                references.append((pid, descriptor, Path(name.removesuffix(" (deleted)"))))
    if pid is None:
        raise CleanupError("empty process reference inventory")
    return references


def process_references(candidates: list[Path]) -> dict[Path, list[str]]:
    lsof = shutil.which("lsof")
    if not lsof:
        raise CleanupError("lsof is required; process arguments alone are insufficient")
    # User installations inspect their owner's processes; shared/system installs
    # must run as root to cover all service users. Never silently ignore stderr.
    argv = [lsof, "-nP", "-F0pfn"]
    if os.geteuid() != 0:
        argv.extend(["-a", "-u", str(os.geteuid())])
    found = {p: [] for p in candidates}
    for pid, descriptor, path in parse_lsof(scan_command(argv)):
        for candidate in candidates:
            if path == candidate or path.is_relative_to(candidate):
                found[candidate].append(f"pid {pid}: {descriptor}")
    ps = scan_command(["ps", "-axo", "pid=,uid=,args="]).decode(errors="surrogateescape")
    for line in ps.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            raise CleanupError("incomplete process argument inventory")
        try:
            pid, uid = map(int, parts[:2])
        except ValueError:
            raise CleanupError("incomplete process argument inventory") from None
        if os.geteuid() != 0 and uid != os.geteuid():
            continue
        arguments = parts[2] if len(parts) == 3 else ""
        for candidate in candidates:
            if str(candidate) in arguments:
                found[candidate].append(f"pid {pid}: argv")
    return {p: sorted(set(reasons)) for p, reasons in found.items()}


def artifact_entries(root: Path):
    """Do not follow links or cross mounts while scanning an artifact tree."""
    device = root.lstat().st_dev
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if info.st_dev != device or os.path.ismount(path):
            raise CleanupError(f"mounted artifact must be retained: {path}")
        yield path, info
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)


def symlink_dependencies(roots: list[Path], *, candidates: list[Path] | None = None) -> set[Path]:
    """Read a bounded dependency closure, including links through shared venvs.

    This traversal only reads. Deletion uses artifact_entries, which never
    follows links. Cycles and shared directories are visited just once.
    """
    pending = list(roots)
    queued_artifacts = set(roots)
    visited = set()
    targets = set()
    while pending:
        root = pending.pop()
        device = root.lstat().st_dev
        entries = [root]
        while entries:
            path = entries.pop()
            info = path.lstat()
            # Hard-linked relative symlinks can resolve differently by parent.
            key = (info.st_dev, info.st_ino, path if stat.S_ISLNK(info.st_mode) else None)
            if key in visited:
                continue
            visited.add(key)
            if len(visited) > MAX_DEPENDENCY_ENTRIES:
                raise CleanupError("dependency scan exceeds limit; retain artifacts")
            if info.st_dev != device or os.path.ismount(path):
                raise CleanupError("dependency scan crosses a mount; retain artifacts")
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                targets.add(target)
                pending.append(target)
                # Retaining a child retains the entire candidate. Its sibling
                # links may protect further artifacts outside the target tree.
                for candidate in candidates or []:
                    if (candidate not in queued_artifacts and overlaps(target, candidate)
                            and candidate.exists()):
                        queued_artifacts.add(candidate)
                        pending.append(candidate)
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as children:
                    entries.extend(Path(child.path) for child in children)
    return targets


def protections(plan: dict, paths: list[Path]) -> dict[Path, list[str]]:
    check_bindings(plan)
    observed = process_references(list(dict.fromkeys([
        *plan["candidates"], *plan.get("quarantined_paths", []), *paths,
    ])))
    found = {p: list(reasons) for p, reasons in observed.items()}
    for protected in plan["protected"]:
        for candidate in found:
            if overlaps(candidate, protected):
                found[candidate].append("active release, rollback, transaction or explicit dependency")
    # Every retained candidate protects its complete dependency closure, even
    # when this is a single-path rescan or only a nested file is protected.
    roots = [p for p in plan["protected"] if p.exists()]
    roots.extend(p for p, reasons in found.items() if reasons and p.exists())
    for target in symlink_dependencies(roots, candidates=list(found)):
        for candidate in paths:
            if overlaps(target, candidate):
                found[candidate].append("symlink dependency of retained artifact")
    if context := plan.get("service_context"):
        from deploy.release_retention import service_dependencies
        # Rediscover load paths, definitions, environment files and aliases, not
        # just files from the initial inventory. Include both original and
        # quarantined names. Abort on new dependencies so all intact artifacts
        # can be restored together, including a newly needed runtime closure.
        dependencies = service_dependencies(context, list(found))
        if any(not any(overlaps(Path(p), kept) for kept in plan["protected"]) for p in dependencies):
            raise CleanupError("service dependencies changed; regenerate the cleanup inventory")
    return {p: found[p] for p in paths}


def run_checks(plan: dict) -> None:
    for check in plan["checks"]:
        # Inventory commands are operator-owned read-only checks, with no shell
        # interpolation. Their output stays private; failures identify the check.
        try:
            result = subprocess.run(check["argv"], cwd=check["cwd"], timeout=60,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            # TimeoutExpired includes full argv by default (possibly auth flags).
            raise CleanupError(
                f"acceptance check did not complete: {check['name']} ({type(exc).__name__})"
            ) from None
        if result.returncode:
            raise CleanupError(f"acceptance check failed: {check['name']}")


def sync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_journal(path: Path, report: dict) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def cleanup(inventory: Path, *, apply: bool = False, lock_descriptor: int | None = None) -> dict:
    plan = load_inventory(inventory)
    journal = plan["root"] / JOURNAL
    # Share the installer's lock. A dropped control connection cannot overlap a
    # second installer/cleanup; leftover journals are inspected, never replayed.
    if lock_descriptor is None:
        lock = acquire_install_lock(plan["root"])
    else:
        verify_install_lock(plan["root"], lock_descriptor)
        lock = os.dup(lock_descriptor)
    moved: list[tuple[Path, Path, dict]] = []
    report = {"schema": 1, "inventory_sha256": plan["digest"], "started_at": time.time(),
              "mode": "apply" if apply else "preview", "status": "preview",
              "process_scope": "all users" if os.geteuid() == 0 else f"uid {os.geteuid()}",
              "retained": [str(p) for p in plan["protected"]], "candidates": [],
              "removed_allocated_bytes": 0}
    try:
        if journal.exists():
            previous, _ = read_json(journal)
            if previous.get("status") not in {"complete", "deferred", "aborted"}:
                raise CleanupError("unfinished cleanup journal; inspect original operation before retrying")
        existing = [p for p in plan["candidates"] if p.exists()]
        guarded = protections(plan, existing)
        for path in plan["candidates"]:
            row = {"path": str(path), "state": "absent" if not path.exists() else "planned",
                   "reasons": guarded.get(path, [])}
            if row["reasons"]:
                row["state"] = "deferred"
            elif path.exists():
                row["identity"] = list(identity(path))
                row["allocated_bytes"] = sum(info.st_blocks * 512 for _, info in artifact_entries(path))
            report["candidates"].append(row)
        if not apply:
            return report
        if not shutil.rmtree.avoids_symlink_attacks:
            raise CleanupError("fd-safe tree removal is unavailable")
        run_checks(plan)
        report["status"] = "quarantining"
        write_journal(journal, report)
        for row in report["candidates"]:
            if row["state"] != "planned":
                continue
            path = absolute_path(row["path"])
            guarded = protections(plan, [path])[path]
            if guarded:
                row.update(state="deferred", reasons=guarded)
                continue
            if list(identity(path)) != row["identity"]:
                raise CleanupError("candidate replaced since preview")
            quarantine = path.with_name(f".cc-remote-retired-{uuid.uuid4().hex}")
            row.update(state="rename_pending", quarantine=str(quarantine))
            write_journal(journal, report)
            path.rename(quarantine)
            moved.append((path, quarantine, row))
            row["state"] = "quarantined"
            plan.setdefault("quarantined_paths", []).append(quarantine)
            sync_directory(path.parent)
            write_journal(journal, report)
        # Keep bytes recoverable until the now-retired paths pass fresh checks.
        run_checks(plan)
        report["status"] = "removing"
        write_journal(journal, report)
        for path, quarantine, row in moved:
            # Complete the potentially long tree walk before the final live
            # process/service dependency check, not between that check and rm.
            for _ in artifact_entries(quarantine):
                pass
            guarded = protections(plan, [path, quarantine])
            reasons = guarded[path] + guarded[quarantine]
            if reasons:
                if path.exists() or path.is_symlink():
                    raise CleanupError("original path reappeared; inspect before restoring")
                quarantine.rename(path)
                row.update(state="deferred", reasons=reasons)
            else:
                if list(identity(quarantine)) != row["identity"]:
                    raise CleanupError("quarantined artifact changed identity")
                parent = os.open(absolute_path(quarantine.parent.as_posix()),
                                 os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    row["state"] = "remove_pending"
                    write_journal(journal, report)
                    if quarantine.is_dir():
                        shutil.rmtree(quarantine.name, dir_fd=parent)
                    else:
                        os.unlink(quarantine.name, dir_fd=parent)
                finally:
                    os.close(parent)
                row["state"] = "removed"
                report["removed_allocated_bytes"] += row["allocated_bytes"]
            sync_directory(path.parent)
            write_journal(journal, report)
        run_checks(plan)
        check_bindings(plan)
        report["status"] = ("deferred" if any(r["state"] == "deferred" for r in report["candidates"])
                            else "complete")
        report["verified_at"] = time.time()
        write_journal(journal, report)
        return report
    except BaseException:
        # Restore quarantined *whole* artifacts. A partially deleted artifact is
        # never relabeled as restored; its journal remains an unresolved outcome.
        if apply and report["status"] != "preview":
            restored = report["status"] == "quarantining"
            for path, quarantine, row in reversed(moved):
                if (row["state"] == "quarantined" and quarantine.exists()
                        and not path.exists() and not path.is_symlink()):
                    try:
                        quarantine.rename(path)
                        sync_directory(path.parent)
                        row["state"] = "restored"
                    except OSError:
                        restored = False
            if any(row["state"] not in {"restored", "planned", "deferred", "absent"}
                   for row in report["candidates"]):
                restored = False
            report["status"] = "aborted" if restored else "requires_inspection"
            write_journal(journal, report)
        raise
    finally:
        os.close(lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path, help="private, operator-reviewed JSON inventory")
    parser.add_argument("--apply", action="store_true", help="quarantine, validate, then remove eligible artifacts")
    args = parser.parse_args(argv)
    try:
        report = cleanup(args.inventory, apply=args.apply)
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 2 if report["status"] == "deferred" else 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Cleanup stopped ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
