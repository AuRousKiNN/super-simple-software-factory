"""Prior evidence is decided once by scout; rejection must stop downstream work."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/skills/sssf/templates/adws"))

import pytest

from adw_modules import agents, evidence_freshness, permissions, tickets
from adw_modules.data_types import AgentCall, BuildInput, EvidenceAssessment, EvidenceScoutOutput, ScoutOutput
from test_codex_m1 import _QueuedRuntime
from test_delivery import adw_build, adw_recheck, build_ticket
from test_review_routing import Flow, review, source_receipt
from test_tickets import git, phase, repo, run_for


@pytest.mark.parametrize("verdict", ["stale", "uncertain", "applicable"])
def test_ticket_dependency_scout_decides_before_builder(monkeypatch, repo, verdict):
    source, result, set_path, first, second = build_ticket(monkeypatch, repo)
    assert result == 0
    (repo / "unrelated.py").write_text("unrelated = True\n")
    git(repo, "add", "unrelated.py")
    git(repo, "commit", "-qm", "添加无关功能")
    ref = tickets.artifact(repo, (source.session_dir / "ticket-acceptance.json").relative_to(repo).as_posix(), ".json")
    manifest = source.session_dir / "dependencies.json"
    manifest.write_text(json.dumps([ref.model_dump()]))
    run = Flow(repo, [review()], name="downstream")
    run.freshness_verdict = verdict
    monkeypatch.setattr(adw_build.session, "ensure", lambda *_: run)
    original = (repo / "code.py").read_bytes()
    head = git(repo, "rev-parse", "HEAD")
    result = adw_build.main(BuildInput(ticket=second, ticket_set=set_path,
                                      dependency_evidence=manifest.relative_to(repo).as_posix()))
    assert not (run.session_dir / "evidence-freshness.json").exists()
    assert not (run.context_handoff_dir / "scout_findings.md").exists()
    assert not any(p.params.name == "evidence_freshness" for p in run.phases)
    assert run.closed
    if verdict == "applicable":
        assert result == 0
        assert [p.owner for p, c in run.calls] == ["scout", "builder", "reviewer", "documenter"]
        for params, call in run.calls:
            if params.owner in {"builder", "reviewer"}:
                assert "decided by scout" in call.previous.notes_for_next_agent
    else:
        assert result == 1 and not run.accepted
        assert [p.owner for p, c in run.calls] == ["scout"]
        assert not any(p.params.owner == "quality" for p in run.phases)
        assert ref.path in run.reason and verdict in run.reason
        assert (repo / "code.py").read_bytes() == original
        assert git(repo, "rev-parse", "HEAD") == head
        assert not (run.session_dir / "ticket-acceptance.json").exists()


@pytest.mark.parametrize("verdict", ["stale", "uncertain", "applicable"])
def test_recheck_scout_owns_changed_content_decision(monkeypatch, repo, verdict):
    monkeypatch.chdir(repo)
    source, request = source_receipt(repo)
    # Content difference alone must reach scout, not a whole-tree rejection.
    (repo / "unrelated.py").write_text("unrelated = True\n")
    git(repo, "add", "unrelated.py")
    git(repo, "commit", "-qm", "添加独立模块")
    request.baseline = git(repo, "rev-parse", "HEAD")
    request.checks = ["test"] if verdict != "applicable" else []
    request_path = source.session_dir / "recheck.json"
    request_path.write_text(request.model_dump_json())
    run = Flow(repo, [review()], name="recheck")
    run.freshness_verdict = verdict
    monkeypatch.setattr(adw_recheck.agents, "load_config", lambda _: run.cfg)
    monkeypatch.setattr(adw_recheck.agents, "validate", lambda *_: None)
    monkeypatch.setattr(adw_recheck.session, "ensure", lambda *_: run)
    monkeypatch.setattr(adw_recheck.quality, "configured_checks", lambda: {"test"})
    if verdict == "applicable":
        # The pending manual obligation still needs acceptance evidence; freshness
        # does not itself approve or waive it.
        from adw_modules.data_types import ReviewReceipt
        receipt = ReviewReceipt.model_validate_json((repo / request.original_review.path).read_text())
        run.reviews = iter([receipt.review])
    assert adw_recheck.main(str(request_path)) == 1
    assert run.logs[0]["changed"] is True
    assert run.closed and not run.accepted
    assert not (run.session_dir / "evidence-freshness.json").exists()
    assert not (run.context_handoff_dir / "scout_findings.md").exists()
    if verdict != "applicable":
        assert [p.owner for p, c in run.calls] == ["scout"]
        assert not any(p.params.owner == "quality" for p in run.phases)
        assert verdict in run.reason
    else:
        assert [p.owner for p, c in run.calls] == ["scout", "reviewer", "documenter"]


def test_scout_must_assess_each_evidence_exactly_once(repo):
    refs = [tickets.artifact(repo, "code.py", ".py"), tickets.artifact(repo, ".gitignore", "")]
    entries = [EvidenceAssessment(**r.model_dump(), verdict="applicable", reason="Inputs unchanged") for r in refs]
    output = EvidenceScoutOutput(status="success", assessments=entries)
    gate = evidence_freshness.coverage(refs)
    assert gate(output, None).passed
    assert not gate(output.model_copy(update={"artifacts": ["scout_findings.md"]}), None).passed
    for invalid in ([entries[0]], [entries[0], entries[0]],
                    [entries[0], entries[1].model_copy(update={"reason": " "})]):
        assert not gate(output.model_copy(update={"assessments": invalid}), None).passed


def test_freshness_scout_is_single_and_readonly_without_changing_general_scout(repo):
    ref = tickets.artifact(repo, "code.py", ".py")
    output = EvidenceScoutOutput(status="success", assessments=[EvidenceAssessment(
        **ref.model_dump(), verdict="applicable", reason="Relevant code unchanged")])
    runtime = _QueuedRuntime([ScoutOutput(status="success").model_dump_json(), output.model_dump_json()])
    run = run_for(repo, "scout", runtime)
    run.cfg.agents[0].subagents.enabled = True
    run.cfg.agents[0].writes = None
    agents.execute(run, phase(run, "scout"), AgentCall(output_type=ScoutOutput, prompt="General exploration"))
    result = agents.execute(run, phase(run, "scout"), AgentCall(
        output_type=EvidenceScoutOutput, prompt="Evidence freshness investigation", gates=[evidence_freshness.coverage([ref])]))
    assert result == output
    assert runtime.requests[0].subagents.enabled
    assert not runtime.requests[1].subagents.enabled
    assert runtime.requests[1].thread_id is None
    assert "scout" in run.agent_map["agents"] and "evidence_scout" in run.agent_map["agents"]
    assert run.cfg.agents[0].subagents.enabled and run.cfg.agents[0].writes is None

    original_turn = runtime.run_turn
    def mutate(request, hooks):
        (repo / "code.py").write_text("forbidden edit")
        return original_turn(request, hooks)
    runtime.texts.append(output.model_dump_json())
    runtime.run_turn = mutate
    before = (repo / "code.py").read_bytes()
    with pytest.raises(permissions.PermissionBreach):
        agents.execute(run, phase(run, "scout"), AgentCall(output_type=EvidenceScoutOutput, prompt="Evidence freshness investigation"))
    assert (repo / "code.py").read_bytes() == before
