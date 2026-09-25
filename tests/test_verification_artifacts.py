"""Ticket proof is repaired in the reviewer turn, before host publication."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_review_routing import review
from test_codex_m1 import _config, _QueuedRuntime, _Run
from adw_modules import agents, gates, tickets
from adw_modules.data_types import AgentCall, Phase, PhaseParams, ReviewObligation, ReviewOutput


def verdict(path):
    output = review()
    output.required_verification = [ReviewObligation(
        id="V-1", description="Inspect SDK contract", satisfied=True, evidence=[path])]
    return output


@pytest.mark.parametrize("kind", ["symlink", "absolute_symlink", "node_modules", "missing", "directory", "outside", "line_reference"])
def test_invalid_proof_is_a_gate_failure(tmp_path, kind):
    proof = tmp_path / "proof.md"
    proof.write_text("Retained inspection and result")
    alias = tmp_path / "alias.md"
    alias.symlink_to(proof)
    vendor = tmp_path / "node_modules/sdk/docs.md"
    vendor.parent.mkdir(parents=True)
    vendor.write_text("Installed dependency reference")
    paths = dict(symlink="alias.md", absolute_symlink=str(alias),
                 node_modules="node_modules/sdk/docs.md", missing="missing.md",
                 directory="node_modules", outside="../proof.md", line_reference="proof.md:1")
    report = gates.verification_artifacts(verdict(paths[kind]), SimpleNamespace(repo_root=tmp_path))
    assert not report.passed
    assert "V-1" in str(report.violations)


@pytest.mark.parametrize("absolute", [False, True])
def test_stable_proof_is_accepted_by_gate_and_publication(tmp_path, absolute):
    proof = tmp_path / "proof"
    proof.write_text("Retained inspection and result")
    value = str(proof) if absolute else "proof"
    assert gates.verification_artifacts(verdict(value), SimpleNamespace(repo_root=tmp_path)).passed
    assert tickets.verification_artifact(tmp_path, value).path == "proof"


def test_invalid_proof_is_corrected_on_same_runtime_thread(tmp_path, monkeypatch):
    system, user = tmp_path / "system.md", tmp_path / "user.md"
    system.write_text("reviewer role")
    user.write_text("{{prompt}}")
    proof = tmp_path / "proof.md"
    proof.write_text("Applicable retained evidence")
    (tmp_path / "alias.md").symlink_to(proof)
    runtime = _QueuedRuntime([verdict("alias.md").model_dump_json(), verdict("proof.md").model_dump_json()])
    run = _Run(tmp_path, _config(system, user), runtime)
    run.repo_root = tmp_path
    phase = Phase(phase_id="review-1", adw_id=run.adw_id, seq=1,
                  params=PhaseParams(name="review", kind="agent", owner="builder",
                                     description="Validate and repair review proof", retries=1))
    monkeypatch.setattr(agents.permissions, "snapshot", lambda _: {})
    monkeypatch.setattr(agents.permissions, "enforce", lambda *a, **kw: [])
    result = agents.execute(run, phase, AgentCall(output_type=ReviewOutput, prompt="review",
                           gates=[gates.verdict_consistent, gates.verification_artifacts]))
    assert result.required_verification[0].evidence == ["proof.md"]
    assert [r.thread_id for r in runtime.requests] == [None, "thread-shared"]
    assert "V-1" in runtime.requests[1].prompt
    assert "alias.md" in runtime.requests[1].prompt


def test_original_gap_and_corrected_ticket_publication(monkeypatch, repo):
    from test_delivery import adw_build, ticket_plan
    from test_review_routing import setup_flow
    from adw_modules.data_types import BuildInput
    import json

    _, ticket, _ = ticket_plan(repo)
    run = setup_flow(monkeypatch, repo, [], adw_build)
    proof = run.context_handoff_dir / "inspection.md"
    proof.write_text("Inspected SDK contract and current implementation; applicable result")
    alias = run.context_handoff_dir / "sdk.md"
    alias.symlink_to(proof)
    bad = verdict(alias.relative_to(repo).as_posix())
    # The original structural gate admits exactly the claim that failed at publication.
    assert gates.verdict_consistent(bad, run).passed
    with pytest.raises(tickets.TicketError, match="symlink"):
        tickets.artifact(repo, bad.required_verification[0].evidence[0])
    good = verdict(proof.relative_to(repo).as_posix())
    run.reviews = iter([good])
    original = run.call
    def call(params, request):
        if params.owner == "reviewer":
            assert gates.verification_artifacts in request.gates
            assert not gates.verification_artifacts(bad, run).passed
        return original(params, request)
    run.call = call
    assert adw_build.main(BuildInput(ticket=ticket)) == 0
    receipt = json.loads((run.session_dir / "ticket-acceptance.json").read_text())
    assert receipt["manual_validation"] == [tickets.verification_artifact(repo, str(proof)).model_dump()]


from test_tickets import repo
