"""Snapshot repository content and enforce each role's write contract.

The Codex sandbox limits execution capabilities; this module independently
decides which repository modifications a phase may leave behind. It snapshots
tracked and untracked content, file kinds, modes, symlink targets, and the Git
index outside the repository. Unauthorized changes are restored to the exact
pre-turn state unless a concurrent external edit makes restoration unsafe.

Only the current invocation's report directory and the session handoff
directory are automatic runtime exceptions. The broader data directory is not
implicitly writable.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Optional

from .data_types import AgentConfig, SSSFConfig


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


class WorkspaceBusy(RuntimeError):
    """Another SSSF process already owns this repository's write lock."""


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw-*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


INDEX_PATH = "@git-index"
HOST_SESSION_FILES = (
    "spec-binding.json", "spec-artifacts/*.json", "spec-finish.json", "spec-acceptance/*.json", "ticket-facts.json",
    "documentation/**", "delivery-evidence.json", "delivery-input.json", "preflight-result.json", "ticket-acceptance-order.json", "work_item.json", "decomposition.json", "decomposition-published.json", "ticket-acceptance.json", "review-routing/*.json",
)


@dataclass(frozen=True, slots=True)
class FileState:
    kind: str
    mode: int
    digest: str
    link_target: Optional[str] = None


@dataclass(slots=True)
class WorkspaceSnapshot:
    root: Path
    files: dict[str, FileState]
    index: Optional[FileState]
    index_path: Optional[Path]
    backup_dir: Path
    backup_names: dict[str, str] = field(default_factory=dict)
    extra_patterns: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        shutil.rmtree(self.backup_dir, ignore_errors=True)


class WorkspaceLock:
    """An advisory, process-lifetime lock shared by all SSSF runs in a repo."""

    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle = handle
        self._released = False

    def assert_held(self) -> None:
        if self._released or self._handle.closed:
            raise WorkspaceBusy(f"workspace lock is no longer held: {self.path}")

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()


def _git_checked(args: list[str], cwd: str | Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def acquire_workspace_lock(repo_root: str | Path) -> WorkspaceLock:
    root = Path(repo_root).resolve()
    git_common = _git_checked(["rev-parse", "--git-common-dir"], root).strip()
    git_dir = Path(git_common)
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    lock_path = git_dir.resolve() / "sssf-workspace.lock"
    handle = lock_path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise WorkspaceBusy(
            f"another SSSF run is already modifying {root}; lock: {lock_path}"
        ) from error
    return WorkspaceLock(lock_path, handle)


def _normalize_repo_path(path: str) -> str:
    candidate = path.replace("\\", "/").strip()
    pure = PurePosixPath(candidate)
    if not candidate or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe repository path: {path!r}")
    normalized = pure.as_posix().removeprefix("./")
    if normalized in ("", "."):
        raise ValueError(f"unsafe repository path: {path!r}")
    return normalized


def _file_state(path: Path) -> Optional[FileState]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        return FileState(
            kind="symlink", mode=mode,
            digest=hashlib.sha256(target.encode()).hexdigest(),
            link_target=target,
        )
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return FileState(kind="file", mode=mode, digest=digest.hexdigest())
    return FileState(kind="other", mode=mode, digest=f"{info.st_mode}:{info.st_size}")


def _copy_to_backup(source: Path, destination: Path, state: FileState) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if state.kind == "file":
        shutil.copyfile(source, destination, follow_symlinks=False)
    elif state.kind == "symlink":
        destination.write_text(state.link_target or "")


def _listed_paths(root: Path) -> list[str]:
    raw = _git_checked(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root,
    )
    return sorted({_normalize_repo_path(value) for value in raw.split("\0") if value})


def _git_index_path(root: Path) -> Optional[Path]:
    raw = _git_checked(["rev-parse", "--git-path", "index"], root).strip()
    if not raw:
        return None
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def snapshot(run) -> WorkspaceSnapshot:
    """Back up tracked/untracked business files and the index outside the repo."""
    root = Path(run.repo_root).resolve()
    backup_dir = Path(tempfile.mkdtemp(prefix=f"sssf-{run.adw_id}-permissions-"))
    files: dict[str, FileState] = {}
    names: dict[str, str] = {}
    extra_patterns = _extra_patterns(run)
    extra_files = {p.relative_to(root).as_posix() for pattern in extra_patterns
                   for p in root.glob(pattern) if p.is_file() or p.is_symlink()}
    for number, relative in enumerate(sorted(set(_listed_paths(root)) | set(_input_paths(run)) | extra_files)):
        state = _file_state(root / relative)
        if state is None:
            state = FileState(kind="missing", mode=0, digest="")
        files[relative] = state
        if state.kind in {"file", "symlink"}:
            backup_name = f"files/{number:08d}"
            names[relative] = backup_name
            _copy_to_backup(root / relative, backup_dir / backup_name, state)

    index_path = _git_index_path(root)
    index = _file_state(index_path) if index_path else None
    if index_path and index is not None:
        names[INDEX_PATH] = "git-index"
        _copy_to_backup(index_path, backup_dir / "git-index", index)
    return WorkspaceSnapshot(root, files, index, index_path, backup_dir, names, extra_patterns)


def _current_states(before: WorkspaceSnapshot) -> dict[str, FileState]:
    paths = set(before.files) | set(_listed_paths(before.root))
    paths.update(p.relative_to(before.root).as_posix() for pattern in before.extra_patterns
                 for p in before.root.glob(pattern) if p.is_file() or p.is_symlink())
    return {
        relative: _file_state(before.root / relative)
        or FileState(kind="missing", mode=0, digest="")
        for relative in sorted(paths)
    }


def changed_paths(
    before: WorkspaceSnapshot | dict[str, object],
    after: WorkspaceSnapshot | dict[str, object],
) -> list[str]:
    """Every path whose content, kind, mode, link target, or presence changed."""
    before_files = before.files if isinstance(before, WorkspaceSnapshot) else before
    after_files = after.files if isinstance(after, WorkspaceSnapshot) else after
    return sorted(
        path for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    )


def _matches(path: str, pattern: str) -> bool:
    normalized = _normalize_repo_path(pattern.rstrip("/") or pattern)
    if pattern.endswith("/"):
        return path == normalized or path.startswith(normalized + "/")
    if "*" in normalized or "?" in normalized:
        return _glob(normalized).fullmatch(path) is not None
    return path == normalized


def always_writable(run) -> list[str]:
    """Only the current session's explicit handoff/report paths are exempt."""
    allowed: list[str] = []
    root = Path(run.repo_root).resolve()
    for attribute in ("context_handoff_dir", "active_report_dir"):
        value = getattr(run, attribute, None)
        if value is None:
            continue
        try:
            relative = Path(value).resolve().relative_to(root).as_posix()
        except ValueError:
            # Runtime files outside the repo are absent from the Git snapshot.
            continue
        allowed.append(relative.rstrip("/") + "/")
    return allowed


def _host_patterns(run) -> list[str]:
    root = Path(run.repo_root).resolve()
    sessions = Path(run.cfg.defaults.data_dir) / "sessions"
    if not sessions.is_absolute():
        sessions = root / sessions
    if not sessions.resolve().is_relative_to(root):
        return []
    prefix = sessions.resolve().relative_to(root).as_posix()
    return [f"{prefix}/*/{name}" for name in HOST_SESSION_FILES]


def _extra_patterns(run) -> list[str]:
    from .spec_artifacts import HOST_PATHS
    patterns = _host_patterns(run) + HOST_PATHS
    scope = getattr(run, "active_write_scope", None)
    if scope:
        patterns.append(scope + "**/*" if scope.endswith("/") else scope)
    return patterns


def _input_paths(run) -> list[str]:
    paths = list(getattr(run, "active_readonly_paths", []))
    root = Path(run.repo_root).resolve()
    sessions = Path(run.cfg.defaults.data_dir) / "sessions"
    if not sessions.is_absolute():
        sessions = root / sessions
    for name in HOST_SESSION_FILES:
        for path in sessions.glob(f"*/{name}"):
            if path.resolve().is_relative_to(root):
                paths.append(path.relative_to(root).as_posix())
    return paths


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig, run=None) -> bool:
    if path == INDEX_PATH:
        return False
    normalized = _normalize_repo_path(path)
    from .spec_artifacts import HOST_PATHS
    if any(_matches(normalized, pattern) for pattern in HOST_PATHS):
        return False
    if run is not None and (normalized in getattr(run, "active_readonly_paths", [])
                            or any(_matches(normalized, p) for p in _host_patterns(run))):
        return False
    if run is not None and any(_matches(normalized, item) for item in always_writable(run)):
        return True
    scope = getattr(run, "active_write_scope", None) if run is not None else None
    if scope and not _matches(normalized, scope):
        return False
    if scope and normalized == scope + "index.json":
        return False
    if scope and agent.name in {"planner", "documenter", "decomposer"}:
        return True
    if any(_matches(normalized, item) for item in (agent.writes or [])):
        return True
    if any(_matches(normalized, item) for item in cfg.defaults.protected_files):
        return False
    return agent.writes is None


def _remove_current(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _restore(
    before: WorkspaceSnapshot,
    relative: str,
    expected_current: Optional[FileState],
) -> str:
    if relative == INDEX_PATH:
        path, state = before.index_path, before.index
    else:
        path, state = before.root / relative, before.files.get(relative)
    if path is None:
        return "no original path"
    observed = _file_state(path)
    expected_missing = expected_current is not None and expected_current.kind == "missing"
    if not (expected_missing and observed is None) and observed != expected_current:
        return "not restored (path changed concurrently during permission check)"
    if state is None or state.kind == "missing":
        _remove_current(path)
        return "removed"

    _remove_current(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = before.backup_dir / before.backup_names[relative]
    if state.kind == "file":
        shutil.copyfile(backup, path, follow_symlinks=False)
        path.chmod(state.mode)
    elif state.kind == "symlink":
        path.symlink_to(backup.read_text())
    else:
        return f"cannot restore unsupported file kind {state.kind!r}"
    return "restored"


def enforce(
    run,
    phase,
    agent: AgentConfig,
    before: WorkspaceSnapshot | dict[str, object],
    *,
    cleanup: bool = False,
) -> list[str]:
    """Check every exit path and restore unauthorized changes from the backup."""
    if not isinstance(before, WorkspaceSnapshot):
        # Lightweight fake snapshots remain useful for isolated caller tests.
        after = snapshot(run)
        try:
            after_files = after.files if isinstance(after, WorkspaceSnapshot) else after
            touched = changed_paths(before, after_files)
        finally:
            cleanup_snapshot = getattr(after, "cleanup", None)
            if cleanup_snapshot:
                cleanup_snapshot()
        breaches = [path for path in touched if not permitted(path, agent, run.cfg, run)]
        if breaches:
            raise PermissionBreach(
                f"{agent.name} modified unauthorized paths: {', '.join(breaches)}"
            )
        return touched

    lock = getattr(run, "workspace_lock", None)
    if lock is not None:
        lock.assert_held()
    current = _current_states(before)
    touched = changed_paths(before.files, current)
    current_index = _file_state(before.index_path) if before.index_path else None
    if current_index != before.index:
        touched.append(INDEX_PATH)
    touched = sorted(set(touched))
    breaches = [path for path in touched if not permitted(path, agent, run.cfg, run)]
    try:
        if not breaches:
            return touched
        outcomes = {
            path: _restore(
                before,
                path,
                current_index if path == INDEX_PATH else current.get(path),
            )
            for path in breaches
        }
        scope = (
            "read-only" if agent.writes == []
            else f"limited to {agent.writes}" if agent.writes
            else f"barred from {run.cfg.defaults.protected_files}"
        )
        detail = "\n".join(
            f"  - {path} — {outcome}" for path, outcome in outcomes.items()
        )
        raise PermissionBreach(
            f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}"
        )
    finally:
        if cleanup:
            before.cleanup()
