"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these
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


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def snapshot(run) -> dict[str, str]:
    """Fingerprint every path the working tree currently differs on.

    Tracked files carry their numstat counts, so an edit to an already-dirty
    file still registers as a change. Untracked files are listed by name.
    Gitignored paths never appear, which is why the session runtime under
    `data_dir` — where handoff files legitimately land — needs no special case.
    """
    fingerprints: dict[str, str] = {}
    for line in _git(["diff", "HEAD", "--numstat"], run.repo_root).splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            path = fields[-1].strip()
            fingerprints[path] = f"{fields[0]},{fields[1]}"
    for path in _git(["ls-files", "--others", "--exclude-standard"],
                     run.repo_root).splitlines():
        if path.strip():
            fingerprints[path.strip()] = "untracked"
    return fingerprints


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
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


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(cfg: SSSFConfig) -> list[str]:
    """The session runtime, which EVERY agent must be able to write.

    `context_handoff/` is the one place agents hand work to each other, and an
    agent's own prompts, raw_output.jsonl, and envelope.json land beside it.
    Scout writes its findings there, the reviewer its review, the planner its
    plan — a read-only agent is read-only with respect to the REPO, never with
    respect to its own report.

    This is granted from `data_dir` rather than left to .gitignore. The runtime
    is normally ignored, so it never even appears in a snapshot — but an agent's
    ability to record its work must not hang on a gitignore entry that someone
    can delete or that a changed `data_dir` can outgrow.
    """
    return [cfg.defaults.data_dir.rstrip("/") + "/"]


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig) -> bool:
    """Session runtime first, then the agent's own list, then what is protected."""
    if any(_matches(path, p) for p in always_writable(cfg)):
        return True
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _roll_back(run, path: str, before: dict[str, str], after: dict[str, str]) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.
    """
    if path in before:
        # Already dirty beforehand. If it is gone from the diff now, the agent
        # reverted an engineer's uncommitted work and the content is not ours
        # to reconstruct — say so loudly rather than pretend it was handled.
        return "REVERTED-BY-AGENT (uncommitted work lost, cannot restore)" \
            if path not in after else "left as-is (was already modified)"
    if after.get(path) == "untracked":
        try:
            (Path(run.repo_root) / path).unlink()
            return "deleted"
        except OSError as error:
            return f"could not delete ({error})"
    result = subprocess.run(["git", "checkout", "--", path],
                            cwd=run.repo_root, capture_output=True, text=True)
    return "rolled back" if result.returncode == 0 else "could not roll back"


def enforce(run, phase, agent: AgentConfig, before: dict[str, str]) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.
    """
    after = snapshot(run)
    touched = changed_paths(before, after)
    breaches = [p for p in touched if not permitted(p, agent, run.cfg)]
    if not breaches:
        return touched

    outcomes = {p: _roll_back(run, p, before, after) for p in breaches}
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files}")
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")


# ── M2 content snapshot and recovery contract ──────────────────────────────
# These definitions intentionally replace the change-count implementation
# above while keeping the public function names stable for installed ADWs.

INDEX_PATH = "@git-index"


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
    for number, relative in enumerate(_listed_paths(root)):
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
    return WorkspaceSnapshot(root, files, index, index_path, backup_dir, names)


def _current_states(before: WorkspaceSnapshot) -> dict[str, FileState]:
    paths = set(before.files) | set(_listed_paths(before.root))
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


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig, run=None) -> bool:
    if path == INDEX_PATH:
        return False
    normalized = _normalize_repo_path(path)
    if run is not None and any(_matches(normalized, item) for item in always_writable(run)):
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
