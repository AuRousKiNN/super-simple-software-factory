"""Regression coverage for automatic ticket delivery and immutable prerequisite selection."""
import json
import os
import sqlite3

import pytest

from test_tickets import repo, git
from test_delivery import ticket_plan, adw_build, build_ticket
from test_review_routing import Flow, review, setup_flow
from adw_modules import acceptance_history as history, delivery, tickets
from adw_modules.data_types import AcceptanceRecord, BuildInput


def prerequisite(root, set_path, name, stamp):
    run = Flow(root, name=name)
    proof = run.session_dir / "proof.md"
    proof.write_text("Verified contract")
    ref = tickets.artifact(root, proof.relative_to(root).as_posix())
    record = AcceptanceRecord(adw_id=name, ticket_id="TICKET-A",
        definition_sha256=tickets.load_index(root, set_path)["definition_sha256"],
        baseline=git(root, "rev-parse", "HEAD"), checks=[ref], reviews=[ref], applicability="Verified")
    path = run.session_dir / "ticket-acceptance.json"
    tickets.atomic_json(path, record.model_dump())
    receipt = tickets.artifact(root, path.relative_to(root).as_posix(), ".json")
    history.publish(run, record, receipt, accepted_at=stamp)
    return run, record, receipt


def test_ticket_only_runs_full_chain_with_automatic_prerequisite(monkeypatch, repo):
    first, result, set_path, _, second = build_ticket(monkeypatch, repo)
    assert result == 0
    run = Flow(repo, [review()], name="downstream")
    monkeypatch.setattr(adw_build.session, "ensure", lambda *_: run)
    assert adw_build.main(BuildInput(ticket=second)) == 0
    assert [p.owner for p, _ in run.calls] == ["scout", "builder", "reviewer", "documenter"]
    snapshot = json.loads((run.session_dir / "delivery-input.json").read_text())
    assert snapshot["work_item"]["dependency_evidence"][0]["path"].startswith("sessions/flow/")
    assert git(repo, "status", "--porcelain") == ""


def test_latest_host_time_then_sequence_not_file_mtime(repo):
    set_path, _, second = ticket_plan(repo)
    older, _, old_ref = prerequisite(repo, set_path, "old", "2026-01-01T00:00:00+00:00")
    _, _, new_ref = prerequisite(repo, set_path, "new", "2026-01-02T00:00:00+00:00")
    os.utime(repo / old_ref.path, (2000000000, 2000000000))
    run = Flow(repo, name="select")
    assert tickets.ticket_work_item(run, None, second).dependency_evidence == [new_ref]
    _, _, tie_ref = prerequisite(repo, set_path, "tie", "2026-01-02T00:00:00+00:00")
    assert tickets.ticket_work_item(run, None, second).dependency_evidence == [tie_ref]


@pytest.mark.parametrize("damage", ["receipt", "proof", "missing"])
def test_corrupt_latest_never_falls_back(repo, damage):
    set_path, _, second = ticket_plan(repo)
    prerequisite(repo, set_path, "old", "2026-01-01T00:00:00+00:00")
    run, _, ref = prerequisite(repo, set_path, "new", "2026-01-02T00:00:00+00:00")
    path = run.session_dir / ("proof.md" if damage == "proof" else "ticket-acceptance.json")
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("damaged")
    with pytest.raises(ValueError):
        tickets.ticket_work_item(Flow(repo, name="select"), None, second)


def test_resume_keeps_selected_evidence_and_rejects_changed_input(monkeypatch, repo):
    set_path, _, second = ticket_plan(repo)
    _, _, old_ref = prerequisite(repo, set_path, "old", "2026-01-01T00:00:00+00:00")
    run = setup_flow(monkeypatch, repo, [], adw_build)
    item = tickets.ticket_work_item(run, None, second)
    request = delivery.DeliveryRequest("deliver", item, git(repo, "rev-parse", "HEAD"))
    delivery.freeze_input(run, request)
    prerequisite(repo, set_path, "new", "2026-01-02T00:00:00+00:00")
    assert tickets.ticket_work_item(run, None, second).dependency_evidence == [old_ref]
    (repo / "code.py").write_text("new baseline")
    git(repo, "add", "code.py")
    git(repo, "commit", "-qm", "修改基线")
    with pytest.raises(ValueError, match="baseline changed"):
        delivery.verify_frozen_input(run, request)


@pytest.mark.parametrize("problem", ["missing", "dirty", "untracked", "ignored_definition"])
def test_preflight_rejection_has_no_agent_or_business_mutation(monkeypatch, repo, problem):
    set_path, first, second = ticket_plan(repo)
    run = setup_flow(monkeypatch, repo, [], adw_build)
    target = first
    if problem == "missing":
        target = second
    elif problem == "dirty":
        (repo / "code.py").write_text("user edit")
    elif problem == "untracked":
        (repo / "specs/temporary.json").write_text("[]")
    else:
        git(repo, "rm", "--cached", first)
        with (repo / ".gitignore").open("a") as handle:
            handle.write(first + "\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "忽略票据")
    before = git(repo, "status", "--porcelain")
    assert adw_build.main(BuildInput(ticket=target)) == 2
    assert run.calls == []
    assert git(repo, "status", "--porcelain") == before
    assert json.loads((run.session_dir / "preflight-result.json").read_text())["status"] == "preflight_rejected"


def test_scout_baseline_mutation_blocks_builder(monkeypatch, repo):
    set_path, _, second = ticket_plan(repo)
    prerequisite(repo, set_path, "old", "2026-01-01T00:00:00+00:00")
    run = setup_flow(monkeypatch, repo, [], adw_build)
    original = run.call
    def mutate(params, call):
        result = original(params, call)
        if params.owner == "scout":
            (repo / "code.py").write_text("external edit")
        return result
    run.call = mutate
    with pytest.raises(ValueError, match="uncommitted tracked"):
        adw_build.main(BuildInput(ticket=second))
    assert [p.owner for p, _ in run.calls] == ["scout"]


def test_explicit_old_history_migration_is_idempotent_and_keeps_receipts(repo):
    set_path, _, second = ticket_plan(repo)
    source, record, ref = prerequisite(repo, set_path, "legacy", "2026-01-01T00:00:00+00:00")
    (source.session_dir / "ticket-acceptance-order.json").unlink()
    run = Flow(repo, name="select")
    with pytest.raises(ValueError, match="migration required"):
        tickets.ticket_work_item(run, None, second)
    database = repo / "sessions/trace.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("CREATE TABLE schema_meta(singleton INTEGER, schema_version INTEGER); INSERT INTO schema_meta VALUES(1,2); CREATE TABLE sessions(adw_id TEXT, ended_at TEXT, status TEXT);")
        connection.execute("INSERT INTO sessions VALUES(?,?,?)", (record.adw_id, "2026-01-01T00:00:00+00:00", "success"))
    assert history.migrate(run, database) == 1
    assert history.migrate(run, database) == 0
    assert tickets.artifact(repo, ref.path, ".json") == ref
    assert tickets.ticket_work_item(run, None, second).dependency_evidence == [ref]


def test_execution_rejects_invalid_structured_metadata_before_agents(monkeypatch, repo):
    _, first, _ = ticket_plan(repo)
    path = repo / first
    path.write_text(path.read_text().replace("revision: 1", "revision: invalid"))
    run = setup_flow(monkeypatch, repo, [], adw_build)
    assert adw_build.main(BuildInput(ticket=first)) == 2
    assert "execution metadata" in run.reason
    assert run.calls == []


def test_failed_later_run_does_not_replace_success(repo):
    set_path, _, second = ticket_plan(repo)
    _, _, ref = prerequisite(repo, set_path, "accepted", "2026-01-01T00:00:00+00:00")
    Flow(repo, name="failed-later")
    assert tickets.ticket_work_item(Flow(repo, name="select"), None, second).dependency_evidence == [ref]


def test_explicit_override_uses_same_full_coverage_validation(repo):
    set_path, _, second = ticket_plan(repo)
    source, _, ref = prerequisite(repo, set_path, "accepted", "2026-01-01T00:00:00+00:00")
    path = source.session_dir / "override.json"
    relative = path.relative_to(repo).as_posix()
    run = Flow(repo, name="select")
    for refs in ({"invalid": True}, [], [ref.model_dump(), ref.model_dump()]):
        tickets.atomic_json(path, refs)
        with pytest.raises(ValueError):
            tickets.ticket_work_item(run, None, second, relative)
    tickets.atomic_json(path, [ref.model_dump()])
    assert tickets.ticket_work_item(run, None, second, relative).dependency_evidence == [ref]
