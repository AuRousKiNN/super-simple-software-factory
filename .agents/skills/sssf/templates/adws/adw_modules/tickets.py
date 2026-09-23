"""Planning metadata, immutable input bindings and host evidence.

Markdown owns definitions. The index is a deterministic, replaceable cache.
Selection, acceptance policy and integration remain the responsibility of ADWs.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from graphlib import CycleError, TopologicalSorter
from pathlib import Path, PurePosixPath

import yaml

from .data_types import (
    AcceptanceRecord, ArtifactRef, DecomposeOutput, DecompositionInput,
    PlanOutput, SpecWorkItem, TicketWorkItem,
)


class TicketError(ValueError):
    pass



def repo_path(root: Path, value: str, suffix: str = "") -> Path:
    pure = PurePosixPath(value)
    if (not value or "\\" in value or pure.is_absolute() or ".." in pure.parts
            or pure.as_posix() != value or value == "."):
        raise TicketError(f"expected repository-relative POSIX path: {value!r}")
    path = root / value
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise TicketError(f"path escapes repository: {value}") from error
    # Reject aliases so identity and write protection refer to the same bytes.
    if path.resolve() != root.resolve() / value:
        raise TicketError(f"symlink path is not a stable artifact: {value}")
    if suffix and (path.suffix != suffix or not path.is_file()):
        raise TicketError(f"missing {suffix} file: {value}")
    return path


def artifact(root: Path, value: str, suffix: str = ".md") -> ArtifactRef:
    path = repo_path(root, value, suffix)
    data = path.read_bytes()
    return ArtifactRef(path=value, sha256=hashlib.sha256(data).hexdigest())


def verify_ref(root: Path, ref: ArtifactRef) -> None:
    actual = artifact(root, ref.path, Path(ref.path).suffix)
    if actual != ref:
        raise TicketError(f"artifact changed: {ref.path}; revalidate or use a new session")


def _metadata(path: Path) -> dict:
    """Read metadata needed by the index; do not validate Markdown formatting.

    Body text is opaque. No frontmatter schema, duplicate-key policy, section,
    identifier, revision, profile or acceptance-table gate is imposed.
    """
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines))
    return yaml.safe_load("\n".join(lines[1:end])) or {}


def _unique(items, label):
    if len(set(items)) != len(items):
        raise TicketError(f"duplicate {label}")


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def read_set(root: Path, set_path: str) -> dict:
    """Extract index inputs. Dependency identity is an execution constraint."""
    path = repo_path(root, set_path)
    meta = _metadata(path)
    spec = artifact(root, meta["source_spec"])
    values = meta.get("tickets", [])
    _unique(values, "set member")
    members = []
    for value in values:
        ticket_path = repo_path(root, value)
        if not ticket_path.is_relative_to(path.parent):
            raise TicketError(f"ticket outside current output directory: {value}")
        ticket = _metadata(ticket_path)
        blockers = [str(value) for value in ticket.get("blocked_by", [])]
        _unique(blockers, "blocker")
        members.append({
            "artifact": artifact(root, value, "").model_dump(),
            "id": str(ticket.get("id", ticket_path.stem)),
            "revision": ticket.get("revision", 1),
            "kind": ticket.get("kind", "behavior"),
            "profile": ticket.get("profile", "standard"),
            "blocked_by": blockers,
            "requirements": ticket.get("requirements", []),
        })
    _unique([member["id"] for member in members], "ticket id")
    graph = {member["id"]: member["blocked_by"] for member in members}
    unknown = {blocker for blockers in graph.values() for blocker in blockers} - set(graph)
    if unknown:
        raise TicketError(f"unknown blocker: {sorted(unknown)}")
    try:
        list(TopologicalSorter(graph).static_order())
    except CycleError as error:
        raise TicketError(f"dependency cycle or self dependency: {error.args[1]}") from error
    payload = {"schema_version": 1, "spec": spec.model_dump(),
               "ticket_set": artifact(root, set_path, "").model_dump(),
               "revision": meta.get("revision", 1), "tickets": sorted(members, key=lambda m: m["id"])}
    payload["definition_sha256"] = _digest(payload)
    return payload


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sssf-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_index(root: Path, set_path: str) -> ArtifactRef:
    payload = read_set(root, set_path)
    value = str(PurePosixPath(set_path).parent / "index.json")
    atomic_json(repo_path(root, value), payload)
    return artifact(root, value, ".json")


def load_index(root: Path, set_path: str) -> dict:
    payload = read_set(root, set_path)
    value = str(PurePosixPath(set_path).parent / "index.json")
    path = repo_path(root, value, ".json")
    if json.loads(path.read_text()) != payload:
        raise TicketError("stale index: revalidate Markdown and regenerate index.json")
    return payload


def spec_work_item(root: Path, value: str) -> SpecWorkItem:
    return SpecWorkItem(spec=artifact(root, value))


def prepare_decomposition(run, spec_path: str) -> DecompositionInput:
    source = artifact(run.repo_root, spec_path)
    saved = run.session_dir / "decomposition.json"
    if (run.session_dir / "decomposition-published.json").exists():
        raise TicketError("decomposition already published; start a new session to revise planning")
    if saved.exists():
        result = DecompositionInput.model_validate_json(saved.read_text())
        if result.spec != source:
            raise TicketError("decomposition source changed; use matching input or a new session")
        return result
    output_dir = Path(spec_path).with_suffix(".tickets").as_posix()
    result = DecompositionInput(spec=source, output_dir=output_dir)
    repo_path(run.repo_root, output_dir).mkdir(parents=True, exist_ok=True)
    atomic_json(saved, result.model_dump())
    return result


def publish_decomposition(run, target: DecompositionInput, output: DecomposeOutput) -> ArtifactRef:
    """Publish the current index and close this session's planning binding."""
    if prepare_decomposition(run, target.spec.path) != target:
        raise TicketError("decomposition input differs from the host binding")
    if output.status != "success" or output.outcome != "ready":
        raise TicketError("decomposition must be ready before publishing")
    set_file = repo_path(run.repo_root, output.ticket_set_path)
    output_dir = repo_path(run.repo_root, target.output_dir)
    if not set_file.is_relative_to(output_dir):
        raise TicketError("ticket set is outside the bound output directory")
    payload = read_set(run.repo_root, output.ticket_set_path)
    if payload["spec"] != target.spec.model_dump():
        raise TicketError("ticket set source differs from dispatched source")
    ref = write_index(run.repo_root, output.ticket_set_path)
    atomic_json(run.session_dir / "decomposition-published.json", ref.model_dump())
    return ref


def decompose(run, spec_path: str) -> DecomposeOutput:
    from .data_types import AgentCall, PhaseParams
    with run.phase(PhaseParams(name="decomposition_input", kind="code", owner="tickets",
                              description="Bind the source digest and stable planning directory")) as ph:
        target = prepare_decomposition(run, spec_path)
        ph.log(**target.model_dump())
    with run.phase(PhaseParams(name="decompose", kind="agent", owner="decomposer",
                              description="Define independently verifiable tickets and real dependencies")) as ph:
        output = ph.call(AgentCall(output_type=DecomposeOutput, prompt="Decompose the bound root spec.",
                                   decomposition=target))
    with run.phase(PhaseParams(name="ticket_index", kind="code", owner="tickets",
                              description="Read planning metadata and publish its machine index")) as ph:
        ref = publish_decomposition(run, target, output)
        ph.log(derived_artifact=ref.model_dump())
    return output


def _git(root: Path, *args) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise TicketError(result.stderr.strip() or "cannot verify implementation baseline")
    return result.stdout.strip()


def _verify_evidence(run, item: TicketWorkItem, payload: dict, check_tree: bool = True) -> None:
    selected = next(t for t in payload["tickets"] if t["id"] == item.ticket_id)
    records = {}
    sessions = Path(run.cfg.defaults.data_dir) / "sessions"
    if not sessions.is_absolute():
        sessions = run.repo_root / sessions
    for ref in item.dependency_evidence:
        verify_ref(run.repo_root, ref)
        path = repo_path(run.repo_root, ref.path, ".json")
        record = AcceptanceRecord.model_validate_json(path.read_text())
        expected = sessions / record.adw_id / "ticket-acceptance.json"
        if path.resolve() != expected.resolve():
            raise TicketError("dependency evidence must reference a host session acceptance record")
        if record.ticket_id in records:
            raise TicketError("duplicate dependency evidence")
        if record.definition_sha256 != payload["definition_sha256"]:
            raise TicketError(f"stale dependency definition: {record.ticket_id}")
        # Core deliberately accepts only an exact baseline. An ADW can revalidate
        # older evidence and issue a new record for the current baseline.
        if record.baseline != _git(run.repo_root, "rev-parse", "HEAD"):
            raise TicketError(f"dependency baseline needs revalidation: {record.ticket_id}")
        for evidence in record.checks + record.reviews + record.manual_validation:
            verify_ref(run.repo_root, evidence)
        records[record.ticket_id] = record
    if set(records) != set(selected["blocked_by"]):
        raise TicketError(f"dependency evidence required for {selected['blocked_by']}")
    if records and check_tree:
        _assert_clean_baseline(run)


def _assert_clean_baseline(run) -> None:
    sessions = Path(run.cfg.defaults.data_dir) / "sessions"
    if not sessions.is_absolute():
        sessions = run.repo_root / sessions
    if _git(run.repo_root, "diff", "HEAD", "--name-only"):
        raise TicketError("uncommitted tracked changes require dependency revalidation")
    for value in _git(run.repo_root, "ls-files", "--others", "--exclude-standard").splitlines():
        path = run.repo_root / value
        if path.is_relative_to(sessions) or value.startswith("specs/"):
            continue
        raise TicketError(f"untracked implementation input needs dependency revalidation: {value}")


def ticket_work_item(run, set_path: str, ticket_path: str, evidence_path: str | None = None) -> TicketWorkItem:
    payload = load_index(run.repo_root, set_path)
    member = next((t for t in payload["tickets"] if t["artifact"]["path"] == ticket_path), None)
    if member is None:
        raise TicketError("selected ticket is not a member of the bound set")
    evidence = []
    if evidence_path:
        evidence = [ArtifactRef.model_validate(e) for e in json.loads(
            repo_path(run.repo_root, evidence_path, ".json").read_text())]
    item = TicketWorkItem(spec=payload["spec"], ticket_set=payload["ticket_set"],
                          ticket=member["artifact"], ticket_id=member["id"],
                          index=artifact(run.repo_root, str(Path(set_path).parent / "index.json"), ".json"),
                          definition_sha256=payload["definition_sha256"], dependency_evidence=evidence)
    saved = run.session_dir / "work_item.json"
    resumed = saved.exists() and json.loads(saved.read_text()) == item.model_dump()
    _verify_evidence(run, item, payload, check_tree=not resumed)
    return item


def validate_work_item(run, item, check_tree: bool = True) -> list[str]:
    verify_ref(run.repo_root, item.spec)
    protected = [item.spec.path]
    if isinstance(item, TicketWorkItem):
        for ref in (item.ticket_set, item.ticket, item.index):
            verify_ref(run.repo_root, ref)
        payload = load_index(run.repo_root, item.ticket_set.path)
        if (payload["definition_sha256"] != item.definition_sha256
                or payload["spec"] != item.spec.model_dump()
                or item.index.path != str(Path(item.ticket_set.path).parent / "index.json")
                or not any(t["id"] == item.ticket_id and t["artifact"] == item.ticket.model_dump()
                           for t in payload["tickets"])):
            raise TicketError("work_item identity or definition does not match the set")
        _verify_evidence(run, item, payload, check_tree)
        for ref in item.dependency_evidence:
            record = AcceptanceRecord.model_validate_json(repo_path(run.repo_root, ref.path, ".json").read_text())
            protected.extend(r.path for r in [ref, *record.checks, *record.reviews, *record.manual_validation])
        protected.extend([item.ticket_set.path, item.index.path,
                          *(t["artifact"]["path"] for t in payload["tickets"])])
    return protected


def bind_work_item(run, call, role: str = "builder"):
    """Persist one target per ADW; feedback never replaces the implementation target."""
    item = call.work_item
    if item is None and role in {"builder", "reviewer"} and isinstance(call.previous, PlanOutput):
        item = spec_work_item(run.repo_root, call.previous.spec_path)
    saved = run.session_dir / "work_item.json"
    if saved.exists():
        raw = json.loads(saved.read_text())
        previous = (SpecWorkItem if raw["kind"] == "spec" else TicketWorkItem).model_validate(raw)
        if item is None:
            item = previous
        elif item != previous:
            raise TicketError("session work_item changed; use matching input or a new adw_id")
    if item:
        protected = validate_work_item(run, item, check_tree=not saved.exists())
        if not saved.exists():
            atomic_json(saved, item.model_dump())
        return item, protected
    return None, []


def record_acceptance(run, record: AcceptanceRecord) -> ArtifactRef:
    """Call after run.finish accepted the ADW's checks/review/manual obligations."""
    if getattr(run, "accepted", False) is not True or record.adw_id != run.adw_id:
        raise TicketError("only a finished, accepted host run may publish ticket evidence")
    item = TicketWorkItem.model_validate_json((run.session_dir / "work_item.json").read_text())
    if (record.ticket_id != item.ticket_id or record.definition_sha256 != item.definition_sha256
            or record.baseline != _git(run.repo_root, "rev-parse", "HEAD")):
        raise TicketError("acceptance record differs from bound ticket or current baseline")
    _assert_clean_baseline(run)
    for ref in (item.spec, item.ticket_set, item.ticket, item.index,
                *record.checks, *record.reviews, *record.manual_validation):
        verify_ref(run.repo_root, ref)
    path = run.session_dir / "ticket-acceptance.json"
    atomic_json(path, record.model_dump())
    return artifact(run.repo_root, path.resolve().relative_to(run.repo_root.resolve()).as_posix(), ".json")


def select_ticket(run, output: DecomposeOutput, selection) -> TicketWorkItem:
    if selection is None:
        raise TicketError("decomposer -> builder requires an explicit --ticket-id")
    payload = load_index(run.repo_root, output.ticket_set_path)
    member = next((t for t in payload["tickets"] if t["id"] == selection.ticket_id), None)
    if member is None:
        raise TicketError(f"unknown selected ticket: {selection.ticket_id}")
    return ticket_work_item(run, output.ticket_set_path, member["artifact"]["path"],
                            selection.dependency_evidence)
