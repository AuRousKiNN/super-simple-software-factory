#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""Install the Codex-only SSSF distribution into a target repository.

The installer is repeatable and transactional. Existing user-owned config,
prompts, workflows, and recipes are preserved. Managed files update only when
their recorded digest still matches; local edits are reported as conflicts.
Every successful change set records a manifest and a restorable snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
SSSF_SKILL = Path(__file__).resolve().parent.parent / "SKILL.md"
DISTRIBUTION_VERSION = "ticket-launch-v1-codex-sdk-0.155.1"
MANIFEST_PATH = Path(".sssf/manifest.json")
BACKUP_ROOT = Path(".sssf/backups")

GITIGNORE_ENTRIES = (
    "/.agents/skills/sssf/",
    "/.codex/agents/sssf_recon.toml",
    "/.sssf/",
    "/adws/",
    "/justfile",
    "/.env",
)

# A snapshot is made before these known retired files are removed.
REMOVED_IN_M4 = (
    Path("adws/adw_document.py"),
    Path("adws/adw_modules/agent_pi.py"),
    Path("adws/adw_data/harness_engineering/subagents.ts"),
    Path("adws/adw_data/harness_engineering/themeMap.ts"),
)


@dataclass(frozen=True)
class SourceFile:
    relative: Path
    data: bytes
    mode: int
    ownership: Literal["managed", "user"]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Action:
    kind: Literal["write", "remove"]
    relative: Path
    source: SourceFile | None = None


def _digest(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _state_token(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
    digest = _digest(path)
    if digest is not None:
        return "file:" + digest
    if path.exists():
        return "other"
    return "absent"


def _is_user_owned(relative: Path) -> bool:
    value = relative.as_posix()
    return (
        value == "adws/adw_sssf_config/sssf.config.yaml"
        or value == "justfile"
        or value == "adws/adw_modules/quality.py"
        or value.startswith("adws/adw_data/prompt_engineering/")
        or (
            value.startswith("adws/")
            and relative.name.startswith("adw-")
            and relative.parent == Path("adws")
        )
    )


def _source_file(source: Path, relative: Path) -> SourceFile:
    ownership: Literal["managed", "user"] = (
        "user" if _is_user_owned(relative) else "managed"
    )
    return SourceFile(
        relative=relative,
        data=source.read_bytes(),
        mode=stat.S_IMODE(source.stat().st_mode),
        ownership=ownership,
    )


def _walk(source_root: Path, target_root: Path) -> list[SourceFile]:
    result: list[SourceFile] = []
    for source in sorted(source_root.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        relative = target_root / source.relative_to(source_root)
        result.append(_source_file(source, relative))
    return result


def _gitignore_source(root: Path) -> SourceFile:
    path = root / ".gitignore"
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines()
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in lines]
    if missing:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n# sssf installation (specs/ stays trackable)\n" + "\n".join(missing) + "\n"
    return SourceFile(Path(".gitignore"), text.encode(), 0o644, "user")


def collect_sources(root: Path) -> dict[Path, SourceFile]:
    sources = {
        item.relative: item
        for item in (
            _walk(TEMPLATES / "adws", Path("adws"))
            + _walk(
                TEMPLATES / "prompt_engineering",
                Path("adws/adw_data/prompt_engineering"),
            )
        )
    }
    for source, relative in (
        (
            TEMPLATES / "sssf.config.yaml",
            Path("adws/adw_sssf_config/sssf.config.yaml"),
        ),
        (TEMPLATES / "justfile", Path("justfile")),
    ):
        item = _source_file(source, relative)
        sources[relative] = item

    role_source = TEMPLATES / "codex_agents" / "sssf_recon.toml"
    role_data = role_source.read_text().replace(
        "{{sssf_skill_path_toml}}", json.dumps(str(SSSF_SKILL.resolve())),
    ).encode()
    role = SourceFile(
        Path(".codex/agents/sssf_recon.toml"),
        role_data,
        stat.S_IMODE(role_source.stat().st_mode),
        "managed",
    )
    sources[role.relative] = role
    gitignore = _gitignore_source(root)
    sources[gitignore.relative] = gitignore
    return sources


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


# Only untouched recorded legacy entries are retired automatically; customized
# workflows require an explicit merge and are never erased by --force-managed.
RETIRED_WORKFLOWS = tuple(Path("adws/" + name + ".py") for name in (
    "adw_prompt", "adw_scout", "adw_plan", "adw_decompose", "adw_plan_decompose",
    "adw_build", "adw_build_test", "adw_build_review", "adw_plan_build",
    "adw_plan_build_test", "adw_plan_build_test_quality", "adw_quality", "adw_recheck", "adw_simple_sdlc",
    "adw-build-test", "adw-build-review", "adw-plan-build", "adw-plan-build-test", "adw-plan-build-test-quality",
))


def _safe_relative(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError(f"unsafe installer path: {value!r}")
    return relative


def plan_install(
    root: Path,
    sources: dict[Path, SourceFile],
    *,
    force_managed: bool,
) -> tuple[list[Action], list[Path], list[Path]]:
    manifest = _read_json(root / MANIFEST_PATH)
    previous = manifest.get("files") or {}
    actions: list[Action] = []
    preserved: list[Path] = []
    conflicts: list[Path] = []

    for relative, source in sorted(sources.items(), key=lambda item: item[0].as_posix()):
        destination = root / relative
        current = _digest(destination)
        if current == source.digest:
            continue
        if relative == Path(".gitignore"):
            actions.append(Action("write", relative, source))
            continue
        if not destination.exists():
            actions.append(Action("write", relative, source))
            continue
        if source.ownership == "user":
            preserved.append(relative)
            continue
        prior = previous.get(relative.as_posix()) if isinstance(previous, dict) else None
        prior_digest = prior.get("digest") if isinstance(prior, dict) else None
        if force_managed or (prior_digest and prior_digest == current):
            actions.append(Action("write", relative, source))
        else:
            conflicts.append(relative)

    # Remove earlier managed files only when their recorded bytes are intact.
    if isinstance(previous, dict):
        for name, metadata in previous.items():
            relative = _safe_relative(name)
            if relative in sources or not isinstance(metadata, dict):
                continue
            destination = root / relative
            if not destination.exists() or metadata.get("ownership") == "user":
                continue
            current = _digest(destination)
            if force_managed or current == metadata.get("digest"):
                actions.append(Action("remove", relative))
            else:
                conflicts.append(relative)

    for relative in RETIRED_WORKFLOWS:
        if not (root / relative).exists():
            continue
        prior = previous.get(relative.as_posix(), {}) if isinstance(previous, dict) else {}
        if prior.get("digest") == _digest(root / relative):
            if not any(action.relative == relative for action in actions):
                actions.append(Action("remove", relative))
        else:
            conflicts.append(relative)

    for relative in REMOVED_IN_M4:
        if (root / relative).exists() and not any(
            action.relative == relative for action in actions
        ):
            actions.append(Action("remove", relative))

    return actions, preserved, sorted(set(conflicts), key=Path.as_posix)


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_paths(root: Path, actions: list[Action]) -> list[Path]:
    paths = {action.relative for action in actions}
    paths.add(MANIFEST_PATH)
    paths.add(Path("adws/adw_sssf_config/sssf.config.yaml"))
    for suffix in ("", "-wal", "-shm"):
        paths.add(Path(f"adws/adw_data/sssf.db{suffix}"))
    return sorted(paths, key=Path.as_posix)


def _create_snapshot(root: Path, paths: list[Path]) -> tuple[Path, dict]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    snapshot_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    directory = root / BACKUP_ROOT / snapshot_id
    directory.mkdir(parents=True, exist_ok=False)
    entries: list[dict] = []
    for index, relative in enumerate(paths):
        path = root / relative
        if path.is_symlink():
            kind = "symlink"
        elif path.is_file():
            kind = "file"
        elif path.exists():
            kind = "other"
        else:
            kind = "absent"
        entry = {
            "path": relative.as_posix(),
            "kind": kind,
            "pre_state": _state_token(path),
            "backup": None,
            "link_target": os.readlink(path) if kind == "symlink" else None,
            "post_state": None,
        }
        if kind == "file":
            backup_name = f"{index:04d}.bin"
            shutil.copy2(path, directory / backup_name)
            entry["backup"] = backup_name
        entries.append(entry)
    payload = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "distribution_version": DISTRIBUTION_VERSION,
        "rolled_back_at": None,
        "entries": entries,
    }
    _atomic_write(
        directory / "snapshot.json",
        json.dumps(payload, indent=2).encode() + b"\n",
    )
    return directory, payload


def _restore_snapshot(root: Path, directory: Path, payload: dict, *, check: bool) -> None:
    entries = payload.get("entries") or []
    if check:
        changed = []
        for entry in entries:
            expected = entry.get("post_state")
            relative = _safe_relative(str(entry["path"]))
            if expected != _state_token(root / relative):
                changed.append(entry["path"])
        if changed:
            raise RuntimeError(
                "rollback refused because files changed after installation: "
                + ", ".join(changed)
            )
    for entry in reversed(entries):
        destination = root / _safe_relative(str(entry["path"]))
        kind = entry.get("kind")
        if kind == "file":
            backup = directory / str(entry["backup"])
            _atomic_write(
                destination,
                backup.read_bytes(),
                stat.S_IMODE(backup.stat().st_mode),
            )
        elif kind == "symlink":
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(str(entry["link_target"]))
        elif kind == "absent":
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
    _prune_empty_directories(root)


def _prune_empty_directories(root: Path) -> None:
    candidates = (
        root / "adws/adw_data/harness_engineering",
        root / "adws/adw_data",
        root / "adws/adw_modules",
        root / "adws",
        root / ".codex/agents",
        root / ".codex",
    )
    for path in candidates:
        try:
            path.rmdir()
        except OSError:
            pass


def _manifest_payload(root: Path, sources: dict[Path, SourceFile]) -> dict:
    files = {}
    for relative, source in sorted(sources.items(), key=lambda item: item[0].as_posix()):
        digest = _digest(root / relative)
        if digest is None:
            continue
        files[relative.as_posix()] = {
            "digest": digest,
            "source_digest": source.digest,
            "ownership": source.ownership,
        }
    return {
        "schema_version": 1,
        "distribution_version": DISTRIBUTION_VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def apply_install(root: Path, actions: list[Action], sources: dict[Path, SourceFile]) -> str:
    snapshot_dir, snapshot = _create_snapshot(root, _snapshot_paths(root, actions))
    try:
        for action in actions:
            destination = root / action.relative
            if action.kind == "write":
                assert action.source is not None
                _atomic_write(destination, action.source.data, action.source.mode)
            elif destination.is_file() or destination.is_symlink():
                destination.unlink()
        _prune_empty_directories(root)
        manifest = _manifest_payload(root, sources)
        _atomic_write(
            root / MANIFEST_PATH,
            json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n",
        )
    except BaseException:
        _restore_snapshot(root, snapshot_dir, snapshot, check=False)
        snapshot["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        snapshot["automatic_rollback"] = True
        try:
            _atomic_write(
                snapshot_dir / "snapshot.json",
                json.dumps(snapshot, indent=2).encode() + b"\n",
            )
        except OSError:
            # The target repository is already restored. A damaged diagnostic
            # snapshot must not replace the original installation exception.
            pass
        raise

    for entry in snapshot["entries"]:
        entry["post_state"] = _state_token(root / entry["path"])
    _atomic_write(
        snapshot_dir / "snapshot.json",
        json.dumps(snapshot, indent=2).encode() + b"\n",
    )
    return snapshot["snapshot_id"]


def _resolve_snapshot(root: Path, requested: str) -> tuple[Path, dict]:
    backup_root = root / BACKUP_ROOT
    if requested == "latest":
        candidates = (
            sorted(
                (path for path in backup_root.iterdir() if path.is_dir()),
                reverse=True,
            )
            if backup_root.is_dir()
            else []
        )
    else:
        if Path(requested).name != requested or requested in {"", ".", ".."}:
            raise RuntimeError(f"unsafe snapshot id: {requested!r}")
        candidates = [backup_root / requested]
    for directory in candidates:
        payload = _read_json(directory / "snapshot.json")
        if payload and not payload.get("rolled_back_at"):
            return directory, payload
    raise RuntimeError(f"no usable installer snapshot found for {requested!r}")


def rollback(root: Path, requested: str) -> str:
    directory, payload = _resolve_snapshot(root, requested)
    _restore_snapshot(root, directory, payload, check=True)
    payload["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(
        directory / "snapshot.json",
        json.dumps(payload, indent=2).encode() + b"\n",
    )
    return str(payload["snapshot_id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="target repository root",
    )
    parser.add_argument(
        "--force-managed",
        "--force",
        action="store_true",
        dest="force_managed",
        help="replace conflicting managed runtime files; never overwrite user-owned files",
    )
    parser.add_argument(
        "--rollback", metavar="SNAPSHOT", help="restore a snapshot id, or 'latest'",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    try:
        if args.rollback:
            snapshot_id = rollback(root, args.rollback)
            print(f"sssf rollback complete: {snapshot_id}")
            return 0

        sources = collect_sources(root)
        actions, preserved, conflicts = plan_install(
            root, sources, force_managed=args.force_managed,
        )
        if conflicts:
            print("sssf installation stopped; managed files or retired workflows need an explicit merge:")
            for relative in conflicts:
                print(f"  ! {relative}")
            print(
                "merge retired workflows explicitly; for managed runtime conflicts only, rerun with --force-managed "
                "after reviewing the conflicts; a pre-write backup will be created"
            )
            return 2

        current_manifest = _read_json(root / MANIFEST_PATH)
        if (
            not actions
            and current_manifest.get("distribution_version") == DISTRIBUTION_VERSION
        ):
            print(f"sssf {DISTRIBUTION_VERSION} is already current in {root}")
            for relative in preserved:
                print(f"  = kept {relative}")
            return 0

        snapshot_id = apply_install(root, actions, sources)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"sssf installation failed: {error}", file=sys.stderr)
        return 1

    written = sum(action.kind == "write" for action in actions)
    removed = sum(action.kind == "remove" for action in actions)
    print(f"sssf {DISTRIBUTION_VERSION} installed into {root}")
    print(
        f"  written: {written}; removed legacy: {removed}; "
        f"preserved user files: {len(preserved)}"
    )
    for relative in preserved:
        print(f"  = kept {relative}")
    print(f"  rollback snapshot: {snapshot_id}")
    print("\nspec artifact upgrade: merge --spec-dir/--spec targets, DocumentDraftOutput and host publication.")
    print("  Documenter writes only invocation reports; remove standalone document workflows.")
    print("\nautomatic ticket upgrade: merge adw-build to delivery.launch; --ticket alone resolves dependencies.")
    print("  Explicitly run scripts/migrate_ticket_history.py for existing acceptance chronology.")
    print("\nticket contract upgrade: preserved roster/prompts/ADWs need an explicit merge.")
    print("  Add decomposer + recon; planner spec_path; builder/reviewer {{work_item}};")
    print("  concrete output types and unchanged work_item in repair/review calls.")
    print("  See .agents/skills/sssf/cookbooks/install.md and references/tickets.md.")
    print("\nworkflow upgrade: nine hyphenated entries; build uses shared full delivery.")
    print("  Merge preserved quality.required_checks/not_applicable_checks and update justfile commands.")
    print("\nnext steps:")
    print("  1. codex --version && codex login status")
    print("  2. just demo")
    print("  3. just sessions")
    print("  4. just obs")
    print("\nno just? run:")
    print('  uv run adws/adw-prompt.py "summarize this repo" --agent scout')
    return 0


if __name__ == "__main__":
    sys.exit(main())
