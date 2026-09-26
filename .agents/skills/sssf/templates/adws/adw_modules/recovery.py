"""Explicit delivery recovery in a new session, preserving the source attempt.

Checkpoints retain completed stages and their proof. Resume reuses only results
whose inputs and retained artifacts still match; retry invalidates downstream work.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import permissions, quality, review_routing, tickets
from .data_types import (ArtifactRef, BuildOutput, DocumentDraftOutput, DocumentOutput,
                         QualityCheckResult, ReviewOutput, ReviewReceipt, SpecWorkItem, TicketWorkItem)


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
    checks: dict[str, QualityCheckResult] = Field(default_factory=dict)
    check_proofs: dict[str, ArtifactRef] = Field(default_factory=dict)
    approval: ArtifactRef | None = None
    approval_proofs: list[ArtifactRef] = Field(default_factory=list)
    document_draft: DocumentDraftOutput | None = None
    draft_proofs: list[ArtifactRef] = Field(default_factory=list)
    document: DocumentOutput | None = None
    document_proof: ArtifactRef | None = None
    commit_parent: str = ""
    commit_tree: str = ""
    commit_sha: str = ""
    finish_started: bool = False
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
        invalidate(result, checks=True)
        result.repairs = result.verifications = 0
    return result


def invalidate(state: DeliveryCheckpoint, *, checks: bool = False) -> None:
    """Invalidate descendants without discarding previous review obligations."""
    if checks:
        state.checks = {}
        state.check_proofs = {}
    state.approval = None
    state.approval_proofs = []
    state.document_draft = None
    state.draft_proofs = []
    state.document = None
    state.document_proof = None
    state.commit_parent = state.commit_tree = state.commit_sha = ""
    state.finish_started = False


def file_ref(run, value) -> ArtifactRef:
    path = Path(value)
    relative = path.relative_to(run.repo_root).as_posix() if path.is_absolute() else str(value)
    return tickets.artifact(run.repo_root, relative, path.suffix)


def remember_check(run, state, result) -> None:
    state.checks[result.name] = result
    state.check_proofs[result.name] = file_ref(run, result.output_artifact)
    save(run, state)


def reusable_checks(run, state) -> dict[str, QualityCheckResult]:
    valid = {}
    for name, result in review_routing.refresh_results(run, state.checks).items():
        try:
            ref = state.check_proofs[name]
            if ref.path != file_ref(run, result.output_artifact).path:
                continue
            tickets.verify_ref(run.repo_root, ref)
        except (KeyError, ValueError, OSError):
            continue
        if result.passed and result.applicable:
            valid[name] = result
    if set(valid) != set(state.checks):
        invalidate(state)
    state.checks = valid
    state.check_proofs = {name: state.check_proofs[name] for name in valid}
    return valid


def remember_approval(run, state, path) -> None:
    receipt = ReviewReceipt.model_validate_json(Path(path).read_text())
    state.approval = file_ref(run, path)
    state.approval_proofs = [file_ref(run, p) for p in receipt.review.artifacts]
    if isinstance(state.work_item, TicketWorkItem):
        state.approval_proofs.extend(tickets.verification_artifact(run.repo_root, p)
            for obligation in receipt.review.required_verification for p in obligation.evidence)
    save(run, state)


def reusable_approval(run, state):
    if not state.approval:
        return None
    try:
        for ref in [state.approval, *state.approval_proofs]:
            tickets.verify_ref(run.repo_root, ref)
        receipt = ReviewReceipt.model_validate_json((run.repo_root / state.approval.path).read_text())
        from . import gates
        if not gates.verdict_consistent(receipt.review, run).passed:
            raise ValueError("cached review no longer passes the current verdict gate")
        if isinstance(state.work_item, TicketWorkItem) and not gates.verification_artifacts(receipt.review, run).passed:
            raise ValueError("cached verification evidence no longer passes the current gate")
        if (receipt.work_item != state.work_item or receipt.prompt != state.prompt
                or receipt.tree_sha256 != review_routing.verification_fingerprint(run)
                or receipt.decision.action != "approve"):
            raise ValueError("approval inputs changed")
    except (ValueError, OSError):
        invalidate(state)
        return None
    return receipt


def remember_draft(run, draft) -> None:
    state = getattr(run, "active_delivery_checkpoint", None)
    if state is None:
        return
    state.document_draft = draft
    state.draft_proofs = [file_ref(run, p) for p in
                         [draft.execution_draft_path, draft.overview_draft_path]]
    save(run, state)


def reusable_document(run, state):
    if state.document is None or state.document_proof is None:
        return None
    tickets.verify_ref(run.repo_root, state.document_proof)
    return state.document


def commit_once(run, state, message: str, paths: list[str]) -> str:
    """Persist commit intent before execution; recognize success after a lost response."""
    from . import git_helper
    head = git_helper.rev("HEAD")
    if state.commit_sha:
        if (git_helper.merge_base(state.commit_sha, head) != state.commit_sha
                or git_helper.rev(state.commit_sha + "^{tree}") != state.commit_tree):
            raise ValueError("recorded delivery commit differs from the saved intent/history")
        return state.commit_sha
    if not state.commit_tree:
        state.commit_parent = head
        state.commit_tree = git_helper.planned_tree(paths)
        save(run, state)
    if head != state.commit_parent:
        if (git_helper.rev("HEAD^{tree}") != state.commit_tree
                or git_helper.rev("HEAD^") != state.commit_parent):
            raise ValueError("commit outcome differs from the saved intent")
        state.commit_sha = head
    else:
        if git_helper.planned_tree(paths) != state.commit_tree:
            raise ValueError("commit inputs changed since saved intent")
        committed = git_helper.commit_paths(message, paths)
        if git_helper.rev(committed + "^{tree}") != state.commit_tree:
            raise ValueError("commit hook changed the intended tree")
        state.commit_sha = committed
    save(run, state)
    return state.commit_sha
