"""Explicit delivery recovery in a new session, preserving the source attempt.

Checkpoints retain implementation progress, never approval. Every recovery runs
fresh checks and review; only a successful builder step can be skipped.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import permissions, quality, review_routing, tickets
from .data_types import ArtifactRef, BuildOutput, ReviewOutput, SpecWorkItem, TicketWorkItem


class DeliveryCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    adw_id: str
    repo_root: str
    status: Literal["running", "stopped", "completed"] = "running"
    source: str | None = None
    history: list[str] = Field(default_factory=list)
    mode: Literal["resume", "retry"] | None = None
    prompt: str
    work_item: SpecWorkItem | TicketWorkItem
    build_base: str
    policy: str
    workspace: dict = Field(default_factory=dict)
    build: BuildOutput | None = None
    previous_review: ReviewOutput | None = None
    repairs: int = Field(default=0, ge=0)
    verifications: int = Field(default=0, ge=0)
    required_checks: list[str] = Field(default_factory=list)
    reports: list[ArtifactRef] = Field(default_factory=list)
    unsupported_artifacts: list[str] = Field(default_factory=list)
    error: str = ""
    source_error: str = ""
    failure_phase: str = ""
    failure_owner: str = ""


def policy_digest(run) -> str:
    """Include effective commands and prompt bytes even for ignored installations."""
    config = run.cfg.model_dump(mode="json") if hasattr(run.cfg, "model_dump") else {}
    prompts = {}
    for agent in getattr(run.cfg, "agents", []):
        for value in (agent.prompt_engineering.system, agent.prompt_engineering.user):
            path = Path(value)
            prompts[str(path.resolve())] = path.read_text()
    return tickets._digest({"config": config, "prompts": prompts,
        "checks": {k: v.model_dump(mode="json") for k, v in quality.check_specs().items()},
        "not_applicable": quality.not_applicable_checks()})


def workspace(run) -> dict:
    """Compare content, modes, symlinks, HEAD and staged entries, including docs."""
    root = Path(run.repo_root).resolve()
    sessions = run.session_dir.parent.resolve()
    files = {}
    unsupported = []
    for relative in permissions._listed_paths(root):
        path = root / relative
        if path.is_relative_to(sessions):
            continue
        state = permissions._file_state(path)
        if state is not None:
            if state.kind == "other":
                unsupported.append(relative)
            files[relative] = asdict(state)
    return {"head": tickets._git(root, "rev-parse", "HEAD"),
            "index": review_routing._git(run, "ls-files", "--stage", "-z").hex(),
            "files": files, "unsupported": unsupported}


def save(run, state: DeliveryCheckpoint) -> None:
    # Failure in Run.phase closes its lock after permission verification. Reacquire
    # before observing the terminal workspace; never snapshot a competing writer.
    lock = getattr(run, "workspace_lock", None)
    acquired = None
    try:
        if lock is not None:
            try:
                lock.assert_held()
            except permissions.WorkspaceBusy:
                acquired = permissions.acquire_workspace_lock(run.repo_root)
        else:
            acquired = permissions.acquire_workspace_lock(run.repo_root)
        state.workspace = workspace(run)
        state.reports = []
        state.unsupported_artifacts = []
        for envelope in (state.build, state.previous_review):
            for value in envelope.artifacts if envelope else []:
                path = Path(value)
                relative = path.resolve().relative_to(run.repo_root.resolve()).as_posix() if path.is_absolute() else value
                if not (run.repo_root / relative).is_file():
                    state.unsupported_artifacts.append(relative)
                    continue
                state.reports.append(tickets.artifact(run.repo_root, relative, Path(relative).suffix))
        tickets.atomic_json(run.session_dir / "delivery-checkpoint.json", state.model_dump(mode="json"))
    finally:
        if acquired:
            acquired.release()


def stop(run, state: DeliveryCheckpoint, error: BaseException) -> None:
    state.status = "stopped"
    state.error = f"{type(error).__name__}: {error}"
    if run.phases:
        state.failure_phase = run.phases[-1].params.name
        state.failure_owner = run.phases[-1].params.owner
    save(run, state)
    run.delivery_stop_saved = True


def start(run, request) -> DeliveryCheckpoint:
    path = run.session_dir / "delivery-checkpoint.json"
    if path.exists():
        raise ValueError("delivery already started in this session; use --resume or --retry with a new adw_id")
    state = DeliveryCheckpoint(adw_id=run.adw_id, repo_root=str(run.repo_root.resolve()),
        prompt=request.prompt, work_item=request.work_item, build_base=request.build_base,
        policy=policy_digest(run), required_checks=quality.required_checks())
    return state


def load(run, source: str, mode: Literal["resume", "retry"]) -> DeliveryCheckpoint:
    """Read-only preflight: no input binding, source writes or business mutations."""
    if not source or Path(source).name != source or source in {".", ".."}:
        raise ValueError("recovery source must be an adw_id, not a path")
    if source == run.adw_id:
        raise ValueError("recovery requires a new adw_id; the source attempt stays immutable")
    directory = run.session_dir.parent / source
    if directory.is_symlink():
        raise ValueError("recovery source must not be a symlink")
    path = directory / "delivery-checkpoint.json"
    if not path.is_file():
        raise ValueError("source has no delivery checkpoint; legacy/pre-delivery runs cannot be resumed")
    state = DeliveryCheckpoint.model_validate_json(path.read_text())
    if state.adw_id != source or state.repo_root != str(run.repo_root.resolve()):
        raise ValueError("recovery source identity/repository mismatch")
    if any(not value or Path(value).name != value or value in {".", "..", run.adw_id}
           for value in state.history):
        raise ValueError("invalid recovery ancestry")
    if state.status != "stopped":
        raise ValueError(f"source is {state.status}, not a safely stopped delivery; do not replay unknown outcomes")
    if state.policy != policy_digest(run):
        raise ValueError("recovery configuration, prompts or quality commands changed")
    if state.workspace.get("unsupported") or state.unsupported_artifacts:
        raise ValueError("recovery cannot verify special files/submodules or non-file artifacts")
    if state.workspace != workspace(run):
        raise ValueError("workspace changed since delivery stopped; preserve/reconcile those edits before recovery")
    tickets.validate_work_item(run, state.work_item, check_tree=False)
    for ref in state.reports:
        tickets.verify_ref(run.repo_root, ref)
    if any(envelope is not None and envelope.status != "success"
           for envelope in (state.build, state.previous_review)):
        raise ValueError("recovery checkpoint contains an unsuccessful completed envelope")
    if (run.session_dir / "delivery-checkpoint.json").exists():
        raise ValueError("recovery destination already contains delivery history; choose a new adw_id")
    result = state.model_copy(deep=True)
    result.adw_id = run.adw_id
    result.source = source
    result.history = list(dict.fromkeys([*state.history, source]))
    result.mode = mode
    result.status = "running"
    result.source_error = state.error
    result.error = ""
    result.failure_phase = result.failure_owner = ""
    if mode == "retry":
        result.build = None
        result.repairs = result.verifications = 0
    return result
