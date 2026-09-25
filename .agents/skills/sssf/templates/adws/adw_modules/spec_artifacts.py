"""Stable spec identities and recoverable host publication. No acceptance policy.

The workspace lock serializes host writes; README compare-and-swap also protects
against outside editors. A durable journal precedes every multi-file publication.
Reports are immutable. A journal is recovery state, not an atomic transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import yaml

from . import tickets
from .data_types import (AgentCall, ArtifactRef, DocumentContext, DocumentDraftOutput,
                         DocumentOutput, DocumentRequest, EventRecord, FinishOptions, GateReport,
                         PhaseParams, PlanningTarget)
from .utils import new_id, now_iso

BODY = "<!-- sssf:body -->\n"
HOST_PATHS = ["specs/README.md", "specs/*/README.md", "specs/*/executions/**",
              "specs/*/spec.tickets/index.json"]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def relative(run, path: Path) -> str:
    return path.absolute().relative_to(run.repo_root.resolve()).as_posix()


def _lock(run):
    run.workspace_lock.assert_held()


def _path(run, value: str) -> Path:
    return tickets.repo_path(run.repo_root, value)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".sssf-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def managed_path(value: str) -> bool:
    """Only documentation owned by this module, never definitions/tests/config."""
    parts = Path(value).parts
    return value == "specs/README.md" or (len(parts) >= 3 and parts[0] == "specs"
        and (parts[2] == "executions" or (len(parts) == 3 and parts[2] == "README.md")))


def snapshot(run) -> str:
    from .review_routing import tree_digest, tree_files
    return tree_digest(tree_files(run))


def _layout(run, spec: str) -> Path:
    path = _path(run, spec)
    parts = Path(spec).parts
    if len(parts) != 3 or parts[0] != "specs" or parts[2] != "spec.md" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", parts[1]):
        raise ValueError("spec must use specs/<spec_key>/spec.md")
    return path.parent


def bind_plan(run, target: PlanningTarget) -> str:
    _lock(run)
    spec = target.spec or f"{target.spec_dir}/spec.md"
    directory = _layout(run, spec)
    binding = run.session_dir / "spec-binding.json"
    if binding.exists():
        saved = json.loads(binding.read_text())
        if saved["spec_path"] != spec or saved["target"] != target.model_dump():
            raise ValueError("conflicting planning target for this session")
        if saved.get("published"):
            tickets.verify_ref(run.repo_root, ArtifactRef.model_validate(saved["published"]))
        return spec
    if (run.session_dir / "work_item.json").exists():
        raise ValueError("revise definitions in a new planning session")
    if target.spec_dir:
        if directory.exists():
            raise ValueError(f"spec directory already occupied: {target.spec_dir}")
    else:
        tickets.artifact(run.repo_root, spec)
    saved = {"spec_path": spec, "target": target.model_dump(),
             "original": _path(run, spec).read_text() if target.spec else None}
    tickets.atomic_json(binding, saved)
    directory.mkdir(parents=True, exist_ok=True)
    return spec


def plan_gate(output, run) -> GateReport:
    result = GateReport()
    try:
        saved = json.loads((run.session_dir / "spec-binding.json").read_text())
        if output.spec_path != saved["spec_path"]:
            raise ValueError("planner changed bound spec_path")
        ref = tickets.artifact(run.repo_root, output.spec_path)
        text = _path(run, ref.path).read_text()
        revision = _revision(_path(run, ref.path))
        old = saved["original"]
        if old is None and revision != 1:
            raise ValueError("new spec requires revision: 1")
        if old is not None:
            old_revision = yaml.safe_load(old.split("---", 2)[1])["revision"]
            if revision != old_revision + (text != old):
                raise ValueError("changed spec requires revision increment")
        if (run.context_handoff_dir / "plan.md").read_text() != text:
            raise ValueError("plan handoff differs from bound spec")
    except (ValueError, OSError, KeyError, IndexError, TypeError) as error:
        return result.check("planning binding", False, str(error))
    return result.check("planning binding", True)


def _revision(path: Path) -> int:
    value = tickets._metadata(path).get("revision")
    if type(value) is not int or value < 1:
        raise ValueError("spec requires positive integer revision")
    return value


def _read(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    if not text.startswith("---\n") or BODY not in text:
        raise ValueError(f"unmanaged overview: {path}")
    return yaml.safe_load(text.split("---", 2)[1]), text.split(BODY, 1)[1]


def _page(meta: dict, body: str) -> str:
    facts = [f"[规格定义](spec.md)：`{meta['spec_path']}` · revision {meta['revision']}",
             f"观测时间：{meta.get('observed_at') or '尚未形成执行观察'}",
             f"代码基线：{meta.get('baseline') or '尚未观测'} · 快照：{meta.get('snapshot') or '未知'}",
             f"观察范围：{meta.get('scope', '尚未观测')}",
             f"验收：{meta.get('acceptance', {}).get('result', '尚未确认')}；范围：{meta.get('acceptance', {}).get('scope', '尚未确认')}"]
    if meta.get("current_revision") != meta["revision"]:
        facts.append(f"当前规格 revision：{meta.get('current_revision', meta['revision'])}；以上观察对应旧定义")
    if meta.get("run_result"):
        result = meta["run_result"]
        state = "待完成" if result["accepted"] is None else "接受" if result["accepted"] else "未接受"
        facts.append(f"最近记录所属运行：{result['adw_id']} · {state} · {result.get('finished_at') or '尚未结束'}")
    acceptance = meta.get('acceptance', {})
    if acceptance.get('source'):
        facts.append(f"验收来源：`{acceptance['source']}`；适用基线：`{acceptance.get('baseline', '')}`")
    if meta.get("stale_reasons"):
        facts.append("**待复核 / 现状尚未同步：" + "；".join(meta["stale_reasons"]) + "**")
    if meta.get("latest_execution"):
        facts.append(f"[最新执行报告]({meta['latest_execution']}) · [完整历史](executions/)")
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=True) + "---\n\n" + "\n\n".join(facts) + "\n\n" + BODY + body.strip() + "\n"


def publish_plan(run, output) -> None:
    _lock(run)
    binding = run.session_dir / "spec-binding.json"
    saved = json.loads(binding.read_text())
    ref = tickets.artifact(run.repo_root, output.spec_path)
    saved["published"] = ref.model_dump()
    tickets.atomic_json(binding, saved)
    initialize(run, ref)
    rebuild_index(run)


def initialize(run, ref: ArtifactRef) -> None:
    directory = _layout(run, ref.path)
    overview = _path(run, relative(run, directory / "README.md"))
    revision = _revision(_path(run, ref.path))
    if overview.exists():
        meta, body = _read(overview)
        if meta["spec_sha256"] != ref.sha256:
            meta["stale_reasons"] = list(dict.fromkeys([*meta.get("stale_reasons", []), "规格定义已改变，原观察待复核"]))
    else:
        meta = {"observed_at": None, "acceptance": {}, "stale_reasons": []}
        body = "尚未形成执行观察。请在实施或复核工作流中整理已有能力和验证证据。"
    title = next((line[2:].strip() for line in _path(run, ref.path).read_text().splitlines() if line.startswith("# ")), directory.name)
    meta.update(spec_path=ref.path, current_spec_sha256=ref.sha256, current_revision=revision, title=title)
    if not meta.get("observed_at"):
        meta.update(spec_sha256=ref.sha256, revision=revision)
    _write(overview, _page(meta, body))


def rebuild_index(run) -> None:
    _lock(run)
    rows = ["# 规格索引", "", "| 规格 | 最近观测 | 验收范围与结果 | 适用限制 |", "|---|---|---|---|"]
    current = snapshot(run)
    for path in sorted((run.repo_root / "specs").glob("*/README.md")):
        _path(run, relative(run, path))
        meta, body = _read(path)
        ref = tickets.artifact(run.repo_root, meta["spec_path"])
        dynamic_reasons = {"规格定义已改变", "实现快照已改变，证据适用性待复核",
                           "验收回执对应旧实现快照，待复核", "绑定定义已改变", "存在未完成的执行记录发布"}
        reasons = [r for r in meta.get("stale_reasons", []) if r not in dynamic_reasons]
        if ref.sha256 != meta["spec_sha256"]:
            reasons.append("规格定义已改变")
        if meta.get("snapshot") and current != meta["snapshot"]:
            reasons.append("实现快照已改变，证据适用性待复核")
        acceptance = meta.get("acceptance", {})
        if acceptance.get("snapshot") and acceptance["snapshot"] != current:
            reasons.append("验收回执对应旧实现快照，待复核")
        acceptance_item = acceptance.get("work_item", {})
        definitions = meta.get("definitions", []) + [acceptance_item[key] for key in ("spec", "ticket", "ticket_set", "index") if key in acceptance_item]
        for source in definitions:
            try:
                tickets.verify_ref(run.repo_root, ArtifactRef.model_validate(source))
            except ValueError:
                reasons.append("绑定定义已改变")
        # Interrupted attempts are discoverable without an agent or database migration.
        for journal in (Path(run.cfg.defaults.data_dir) / "sessions").glob("*/spec-artifacts/*.json"):
            data = json.loads(journal.read_text())
            context = data.get("context", {})
            if context.get("overview_path") == relative(run, path) and data.get("state") != "published":
                reasons.append("存在未完成的执行记录发布")
        meta["stale_reasons"] = list(dict.fromkeys(reasons))
        rendered = _page(meta, body)
        if rendered != path.read_text():
            _write(path, rendered)
        acceptance = meta.get("acceptance", {})
        entry = f"[{meta['title']}]({path.parent.name}/README.md)"
        if (path.parent / "executions").is_dir():
            entry += f" · [历史]({path.parent.name}/executions/)"
        delivery_reasons = []
        for pending in (Path(run.cfg.defaults.data_dir) / "sessions").glob("*/spec-finish.json"):
            if pending == getattr(run, "projecting_finish", None):
                continue
            value = json.loads(pending.read_text())
            if value.get("overview", {}).get("path") == relative(run, path) and value.get("state") != "synced":
                delivery_reasons.append("最终运行事实尚未同步；使用 sync 核对恢复")
        values = [entry, meta.get("observed_at") or "尚未观测",
                  f"{acceptance.get('scope', '尚未确认')} / {acceptance.get('result', '尚未确认')}",
                  "；".join(dict.fromkeys([*meta["stale_reasons"], *delivery_reasons])) or "—"]
        rows.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |")
    _write(_path(run, "specs/README.md"), "\n".join(rows) + "\n")


def _refs(context: DocumentContext) -> list[ArtifactRef]:
    item = context.work_item
    refs = [item.spec, *context.evidence, *context.history]
    if item.kind == "ticket":
        refs += [item.ticket_set, item.ticket, item.index, *item.dependency_evidence]
    return refs


def prepare(run, request: DocumentRequest, phase: str = "document") -> DocumentContext | None:
    _lock(run)
    tickets.validate_work_item(run, request.work_item, check_tree=False)
    if not request.record_requested and not (request.checks or request.review or request.review_receipt or request.evidence or (request.changes and request.changes.changed_files)):
        return None
    directory = _layout(run, request.work_item.spec.path)
    if request.session_only:
        # Rechecking several prerequisite tickets must not advance HEAD between them.
        publication_dir = run.session_dir / "documentation"
        original = directory / "README.md"
        _write(publication_dir / "README.md", original.read_text() if original.exists()
               else _page({"acceptance": {}, "stale_reasons": []}, "本次会话的票据重验记录。"))
    else:
        initialize(run, request.work_item.spec)
        publication_dir = directory
    overview_path = relative(run, publication_dir / "README.md")
    execution_id = re.sub(r"[^A-Za-z0-9_-]", "-", phase) + "-" + new_id(12)
    invocation = run.session_dir / "documenter" / "invocations" / ("inv_" + new_id(12))
    reports = invocation / "reports"
    reports.mkdir(parents=True)
    evidence = list(request.evidence)
    if request.review_receipt:
        evidence.append(request.review_receipt)
    def normalize(value):
        return relative(run, Path(value)) if Path(value).is_absolute() else value
    checks = [c.model_copy(update={"output_artifact": normalize(c.output_artifact)}) for c in request.checks]
    changes = request.changes
    if changes:
        changes = changes.model_copy(update={"diff_path": normalize(changes.diff_path) if changes.diff_path else "",
            "artifacts": [normalize(p) for p in changes.artifacts]})
    request = request.model_copy(update={"checks": checks, "changes": changes})
    paths = [c.output_artifact for c in checks]
    if changes and changes.diff_path:
        paths.append(changes.diff_path)
    if request.review:
        paths.extend(normalize(p) for p in request.review.artifacts)
    for path in paths:
        evidence.append(tickets.artifact(run.repo_root, path, Path(path).suffix))
    from .review_routing import baseline
    observed_baseline = baseline(run)
    review_applicable = None
    if request.review_receipt:
        from .data_types import ReviewReceipt
        tickets.verify_ref(run.repo_root, request.review_receipt)
        receipt = ReviewReceipt.model_validate_json(_path(run, request.review_receipt.path).read_text())
        if receipt.work_item != request.work_item or (request.review and receipt.review != request.review):
            raise ValueError("review receipt differs from the declared target or review")
        review_applicable = receipt.tree_sha256 == snapshot(run)
        if review_applicable:
            observed_baseline = receipt.baseline
    context = DocumentContext(**request.model_dump(exclude={"evidence"}), evidence=evidence,
        spec_key=directory.name, revision=_revision(directory / "spec.md"), review_applicable=review_applicable,
        overview=tickets.artifact(run.repo_root, overview_path),
        history=[tickets.artifact(run.repo_root, relative(run, p)) for p in sorted((directory / "executions").glob("*/*.md"))],
        baseline=observed_baseline, snapshot=snapshot(run), observed_at=now_iso(),
        execution_id=execution_id, invocation_dir=relative(run, invocation),
        execution_draft_path=relative(run, reports / "execution.md"),
        overview_draft_path=relative(run, reports / "overview.md"),
        document_path=relative(run, publication_dir / "executions" / run.adw_id / f"{execution_id}.md"),
        overview_path=overview_path)
    for ref in _refs(context):
        tickets.verify_ref(run.repo_root, ref)
    tickets.atomic_json(_journal(run, execution_id), {"state": "drafting", "context": context.model_dump()})
    return context


def _journal(run, execution_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", execution_id):
        raise ValueError("invalid execution id")
    return run.session_dir / "spec-artifacts" / f"{execution_id}.json"


def validate_context(run, context: DocumentContext) -> None:
    saved = json.loads(_journal(run, context.execution_id).read_text())
    if saved["context"] != context.model_dump():
        raise ValueError("document context differs from host binding")
    for ref in _refs(context):
        tickets.verify_ref(run.repo_root, ref)


def draft_gate(output, run) -> GateReport:
    result = GateReport()
    try:
        context = run.active_document_context
        validate_context(run, context)
        expected = [context.execution_draft_path, context.overview_draft_path]
        if [output.execution_draft_path, output.overview_draft_path] != expected or set(output.artifacts) != set(expected):
            raise ValueError("draft paths must match the current invocation")
        if set(output.documented_files) - set(context.changes.changed_files if context.changes else []):
            raise ValueError("documented_files must come from actual captured changes")
        for value in expected:
            text = _path(run, value).read_text()
            if not text.strip() or text.startswith("---") or BODY.strip() in text:
                raise ValueError("draft must contain a nonempty body without managed metadata")
    except (ValueError, OSError) as error:
        return result.check("document drafts", False, str(error))
    return result.check("document drafts", True)


def _history(context: DocumentContext) -> str:
    groups = {}
    for ref in [*context.history, ArtifactRef(path=context.document_path, sha256="0" * 64)]:
        path = Path(ref.path)
        groups.setdefault(path.parent.name, []).append(path)
    lines = ["", "## 执行报告导航", ""]
    for session, paths in groups.items():
        lines.append(f"- {session}: " + ", ".join(f"[{p.stem}](executions/{session}/{p.name})" for p in paths[-5:]))
    return "\n".join(lines) + "\n"


def publish(run, context: DocumentContext, draft: DocumentDraftOutput) -> DocumentOutput:
    _lock(run)
    journal = _journal(run, context.execution_id)
    saved = json.loads(journal.read_text())
    validate_context(run, context)
    if saved.get("output"):
        if saved["draft"] != draft.model_dump() or any(
                digest(_path(run, p).read_text()) != h for p, h in saved["draft_hashes"].items()):
            raise ValueError("publication replay has different content")
        return recover(run, context.execution_id)
    run.active_document_context = context
    try:
        report = draft_gate(draft, run)
        if not report.passed:
            raise ValueError("; ".join(report.violations))
    finally:
        run.active_document_context = None
    tickets.verify_ref(run.repo_root, context.overview)
    if snapshot(run) != context.snapshot:
        raise ValueError("implementation changed since document input")
    meta, _ = _read(_path(run, context.overview_path))
    scope = context.work_item.ticket_id if context.work_item.kind == "ticket" else "整个规格"
    meta.update(observed_at=context.observed_at, baseline=context.baseline, snapshot=context.snapshot,
                spec_sha256=context.work_item.spec.sha256, revision=context.revision,
                run_result={"adw_id": run.adw_id, "accepted": None}, scope=scope, definitions=[r.model_dump() for r in _refs(context) if r in [context.work_item.spec,
                    getattr(context.work_item, 'ticket', None), getattr(context.work_item, 'ticket_set', None), getattr(context.work_item, 'index', None)]],
                latest_execution=str(Path(context.document_path).relative_to(Path(context.overview_path).parent)),
                stale_reasons=[])
    prior = meta.get("acceptance", {})
    if prior and (prior.get("snapshot") != context.snapshot or prior.get("definition") != context.work_item.spec.sha256):
        meta["stale_reasons"].append("历史验收适用性待复核")
    execution_meta = {"adw_id": run.adw_id, "execution_id": context.execution_id,
        "spec_path": context.work_item.spec.path, "spec_sha256": context.work_item.spec.sha256,
        "revision": context.revision, "baseline": context.baseline, "snapshot": context.snapshot,
        "observed_at": context.observed_at, "scope": scope, "finish": "待完成",
        "sources": [r.model_dump() for r in _refs(context)],
        "body_sha256": digest(_path(run, draft.execution_draft_path).read_text())}
    report_dir = _path(run, context.document_path).parent
    spec_link = os.path.relpath(_path(run, context.work_item.spec.path), report_dir)
    overview_link = os.path.relpath(_path(run, context.overview_path), report_dir)
    execution = ("---\n" + yaml.safe_dump(execution_meta, allow_unicode=True, sort_keys=True)
        + f"---\n\n[规格]({spec_link}) · [现状]({overview_link})\n\n"
        + _path(run, draft.execution_draft_path).read_text().strip() + "\n")
    overview = _page(meta, _path(run, draft.overview_draft_path).read_text() + _history(context))
    output = DocumentOutput(status="success", summary=draft.summary, spec_path=context.work_item.spec.path,
        document_path=context.document_path, overview_path=context.overview_path,
        artifacts=[context.document_path, context.overview_path] + ([] if context.session_only else ["specs/README.md"]),
        documented_files=draft.documented_files, commit_message=draft.commit_message,
        notes_for_next_agent=draft.notes_for_next_agent)
    saved.update(state="prepared", draft=draft.model_dump(),
                 draft_hashes={p: digest(_path(run, p).read_text()) for p in draft.artifacts},
                 execution=execution, overview_text=overview,
                 output=output.model_dump())
    tickets.atomic_json(journal, saved)
    return recover(run, context.execution_id)


def recover(run, execution_id: str) -> DocumentOutput:
    _lock(run)
    journal = _journal(run, execution_id)
    saved = json.loads(journal.read_text())
    if "output" not in saved:
        raise ValueError("unpublished drafts: documenter did not complete publication validation")
    context = DocumentContext.model_validate(saved["context"])
    target = _path(run, context.document_path)
    overview = _path(run, context.overview_path)
    if saved["state"] == "published":
        if not target.is_file() or target.read_text() != saved["execution"]:
            raise ValueError("immutable execution report conflict")
        return DocumentOutput.model_validate(saved["output"])
    validate_context(run, context)
    current = digest(overview.read_text())
    if current not in {context.overview.sha256, digest(saved["overview_text"])}:
        raise ValueError("README publication conflict; preserve pending journal and reconcile inputs")
    if snapshot(run) != context.snapshot:
        raise ValueError("implementation changed before publication recovery")
    if target.exists() and target.read_text() != saved["execution"]:
        raise ValueError("immutable execution report conflict")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".sssf-report-", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(saved["execution"])
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, target)  # atomic, exclusive, never overwrites history
        finally:
            os.unlink(temporary)
    saved["state"] = "report_published"
    tickets.atomic_json(journal, saved)
    _write(overview, saved["overview_text"])
    saved["state"] = "overview_published"
    tickets.atomic_json(journal, saved)
    # Mark complete before index rebuild so this attempt is not shown as pending.
    saved["state"] = "published"
    tickets.atomic_json(journal, saved)
    try:
        if not context.session_only:
            rebuild_index(run)
        _write(run.context_handoff_dir / "document.md", saved["execution"])
        _write(run.context_handoff_dir / "overview.md", saved["overview_text"])
    except BaseException:
        saved["state"] = "overview_published"
        tickets.atomic_json(journal, saved)
        raise
    run.tracer.event(EventRecord(adw_id=run.adw_id, type="log", name="spec_publication",
                                 payload={"execution_id": execution_id, "document_path": context.document_path}))
    return DocumentOutput.model_validate(saved["output"])


def document(run, request: DocumentRequest, name: str = "document") -> DocumentOutput | None:
    with run.phase(PhaseParams(name=name + "_input", kind="code", owner="spec_artifacts",
                               description="Bind execution facts, source hashes and publication destinations")) as ph:
        context = prepare(run, request, name)
        ph.log(execution_id=context.execution_id if context else None)
    if context is None:
        return None
    with run.phase(PhaseParams(name=name, kind="agent", owner="documenter", retries=1,
                               description="Explain current evidence and preserve cumulative specification progress")) as ph:
        draft = ph.call(AgentCall(output_type=DocumentDraftOutput, prompt=request.purpose,
                                  work_item=request.work_item, document_context=context))
    with run.phase(PhaseParams(name=name + "_publish", kind="code", owner="spec_artifacts",
                               description="Publish immutable history and refresh the cumulative overview")) as ph:
        output = publish(run, context, draft)
        ph.log(artifacts=output.artifacts)
    return output


def prepare_finish(run, document: DocumentOutput, options: FinishOptions | None = None) -> None:
    """The owning ADW explicitly declares whether it accepts this complete scope."""
    _lock(run)
    options = options or FinishOptions()
    receipt = options.receipt
    overview = tickets.artifact(run.repo_root, document.overview_path)
    meta, _ = _read(_path(run, overview.path))
    if receipt:
        tickets.verify_ref(run.repo_root, receipt)
    tickets.atomic_json(run.session_dir / "spec-finish.json", {
        "state": "pending_finish", "execution_id": Path(document.document_path).stem,
        "overview": overview.model_dump(),
        "spec": tickets.artifact(run.repo_root, document.spec_path).model_dump(),
        "meta": meta, "receipt": receipt.model_dump() if receipt else None,
        "accepts_scope": options.accepts_scope, "commit": options.commit, "session_only": options.session_only,
        "sources": yaml.safe_load(_path(run, document.document_path).read_text().split("---", 2)[1])["sources"]})


def record_finish(run, accepted: bool) -> None:
    """Persist actual finish result under the original lock, before closing runtime."""
    path = run.session_dir / "spec-finish.json"
    if not path.exists():
        return
    saved = json.loads(path.read_text())
    if saved["state"] != "pending_finish":
        raise ValueError("finish projection already finalized; prepare a new record first")
    saved.update(state="pending_projection", accepted=accepted, finished_at=now_iso())
    tickets.atomic_json(path, saved)


def sync_finished(run) -> None:
    """Reacquire the workspace lock; never overwrite another observation."""
    from . import permissions, git_helper
    from .data_types import ReviewReceipt
    path = run.session_dir / "spec-finish.json"
    if not path.exists():
        return
    saved = json.loads(path.read_text())
    if saved["state"] == "synced":
        return
    if saved["state"] not in {"pending_projection", "projected"}:
        raise ValueError("finish result is unknown; cannot project acceptance")
    lock = permissions.acquire_workspace_lock(run.repo_root)
    previous_lock = run.workspace_lock
    run.workspace_lock = lock
    try:
        tickets.verify_ref(run.repo_root, ArtifactRef.model_validate(saved["spec"]))
        overview = _path(run, saved["overview"]["path"])
        current = digest(overview.read_text())
        if current not in {saved["overview"]["sha256"], digest(saved.get("projection", ""))}:
            raise ValueError("finish README conflict; explicit reconciliation required")
        meta, body = _read(overview)
        if "projection" not in saved:
            meta["run_result"] = {"adw_id": run.adw_id, "accepted": saved["accepted"], "finished_at": saved["finished_at"]}
            if saved["accepts_scope"]:
                if not saved["receipt"]:
                    raise ValueError("scope acceptance requires an explicit host review receipt")
                ref = ArtifactRef.model_validate(saved["receipt"])
                tickets.verify_ref(run.repo_root, ref)
                receipt = ReviewReceipt.model_validate_json(_path(run, ref.path).read_text())
                if receipt.work_item is None or receipt.work_item.spec.model_dump() != saved["spec"]:
                    raise ValueError("acceptance receipt target mismatch")
                scope = receipt.work_item.ticket_id if receipt.work_item.kind == "ticket" else "整个规格"
                # The ADW owns acceptance; this checks provenance, not a second acceptance algorithm.
                if scope != meta["scope"] or receipt.tree_sha256 != meta["snapshot"] or snapshot(run) != meta["snapshot"]:
                    raise ValueError("acceptance receipt baseline/scope no longer applies")
                if saved["accepted"] and receipt.decision.action != "approve":
                    raise ValueError("accepted scope has no approved host receipt")
                acceptance = {"scope": scope, "result": "通过" if saved["accepted"] else "未验收",
                    "source": ref.path, "source_sha256": ref.sha256, "definition": saved["spec"]["sha256"],
                    "baseline": receipt.baseline, "snapshot": receipt.tree_sha256,
                    "finished_at": saved["finished_at"], "accepted": saved["accepted"],
                    "sources": saved["sources"], "mandatory_checks": receipt.mandatory_checks,
                    "review": ref.model_dump(), "work_item": receipt.work_item.model_dump()}
                acceptance_path = run.session_dir / "spec-acceptance" / f"{saved['execution_id']}.json"
                if acceptance_path.exists() and json.loads(acceptance_path.read_text()) != acceptance:
                    raise ValueError("immutable scope acceptance receipt conflict")
                tickets.atomic_json(acceptance_path, acceptance)
                final_ref = tickets.artifact(run.repo_root, relative(run, acceptance_path), ".json")
                meta["acceptance"] = {**acceptance, "source": final_ref.path, "source_sha256": final_ref.sha256}
                meta["stale_reasons"] = [r for r in meta.get("stale_reasons", []) if r not in {"历史验收适用性待复核", "验收回执对应旧实现快照，待复核"}]
            saved["projection"] = _page(meta, body)
            tickets.atomic_json(path, saved)
        _write(overview, saved["projection"])
        saved["state"] = "projected"
        tickets.atomic_json(path, saved)
        run.projecting_finish = path
        if not saved.get("session_only"):
            rebuild_index(run)
        saved["projection"] = overview.read_text()
        tickets.atomic_json(path, saved)
        if saved["commit"]:
            git_helper.commit_paths("同步规格验收事实与索引", [saved["overview"]["path"], "specs/README.md"])
        saved["state"] = "synced"
        saved.pop("error", None)
        tickets.atomic_json(path, saved)
    except BaseException as error:
        saved["error"] = str(error)
        tickets.atomic_json(path, saved)
        raise
    finally:
        run.projecting_finish = None
        lock.release()
        run.workspace_lock = previous_lock


def mark_unsynced(run, item) -> None:
    """Conservative reminder for workflows that change facts without documenter."""
    directory = _layout(run, item.spec.path)
    path = directory / "README.md"
    initialize(run, item.spec)
    meta, body = _read(path)
    reason = f"会话 {run.adw_id} 正在执行；执行现状尚未同步，结果需核对"
    meta["stale_reasons"] = list(dict.fromkeys([*meta.get("stale_reasons", []), reason]))
    _write(path, _page(meta, body))
    rebuild_index(run)


def ticket_accepted(run, item, receipt: ArtifactRef) -> None:
    """Project a host-issued ticket receipt without promoting the entire spec."""
    _lock(run)
    finish = run.session_dir / "spec-finish.json"
    if finish.exists():
        saved = json.loads(finish.read_text())
        if saved.get("state") == "synced" and saved.get("accepted") and saved.get("accepts_scope"):
            # The owning delivery already published and committed this scope's facts.
            # Keep the dependency receipt in the session; another docs commit would
            # immediately invalidate its exact-HEAD baseline.
            tickets.atomic_json(run.session_dir / "ticket-facts.json", {
                "state": "synced", "receipt": receipt.model_dump(), "spec": item.spec.model_dump()})
            return
    initialize(run, item.spec)
    overview = _layout(run, item.spec.path) / "README.md"
    meta, body = _read(overview)
    record = json.loads(_path(run, receipt.path).read_text())
    meta["acceptance"] = {"scope": item.ticket_id, "result": "通过", "accepted": True,
        "source": receipt.path, "source_sha256": receipt.sha256, "baseline": record["baseline"],
        "snapshot": snapshot(run), "definition": item.spec.sha256,
        "ticket_definition": item.definition_sha256, "work_item": item.model_dump()}
    meta["stale_reasons"] = list(dict.fromkeys([*meta.get("stale_reasons", []),
        "新增 ticket 验收回执；累计正文尚未同步"]))
    tickets.atomic_json(run.session_dir / "ticket-facts.json", {
        "state": "pending", "receipt": receipt.model_dump(), "spec": item.spec.model_dump(),
        "overview": tickets.artifact(run.repo_root, relative(run, overview)).model_dump(),
        "projection": _page(meta, body)})
    sync_ticket(run)


def sync_ticket(run) -> None:
    _lock(run)
    path = run.session_dir / "ticket-facts.json"
    if not path.exists():
        return
    saved = json.loads(path.read_text())
    if saved["state"] == "synced":
        return
    tickets.verify_ref(run.repo_root, ArtifactRef.model_validate(saved["receipt"]))
    tickets.verify_ref(run.repo_root, ArtifactRef.model_validate(saved["spec"]))
    overview = _path(run, saved["overview"]["path"])
    if digest(overview.read_text()) not in {saved["overview"]["sha256"], digest(saved["projection"])}:
        raise ValueError("ticket facts README conflict; preserve pending projection")
    _write(overview, saved["projection"])
    rebuild_index(run)
    saved["state"] = "synced"
    tickets.atomic_json(path, saved)
