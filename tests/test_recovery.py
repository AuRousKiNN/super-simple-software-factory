"""Recover retained implementation without replaying acceptance or changing history."""
import json

import pytest

from test_tickets import repo, git
from test_delivery import adw_build, ticket_plan
from test_review_routing import Flow, review, blocker, setup_flow
from adw_modules import quality, recovery, tickets
from adw_modules.data_types import BuildInput


def checkpoint(run):
    return json.loads((run.session_dir / "delivery-checkpoint.json").read_text())


def retained(run):
    return {p.relative_to(run.session_dir).as_posix(): p.read_bytes()
            for p in run.session_dir.rglob("*") if p.is_file()}


def next_run(monkeypatch, source, verdicts=None, name="resumed"):
    run = Flow(source.repo_root, verdicts or [review()], name=name)
    monkeypatch.setattr(adw_build.session, "ensure", lambda *_: run)
    return run


def fail_at(run, owner, error):
    original = run.call
    def call(params, request):
        if params.owner == owner:
            if owner == "builder":
                (run.repo_root / "code.py").write_text("partial implementation\n")
            raise error
        return original(params, request)
    run.call = call


@pytest.mark.parametrize("error", [RuntimeError("builder failed"), KeyboardInterrupt(), SystemExit(143)])
def test_failed_or_interrupted_builder_keeps_changes_and_source(monkeypatch, repo, error):
    _, ticket, _ = ticket_plan(repo)
    source = setup_flow(monkeypatch, repo, [], adw_build)
    fail_at(source, "builder", error)
    with pytest.raises(type(error)):
        adw_build.main(BuildInput(ticket=ticket))
    assert checkpoint(source)["status"] == "stopped"
    assert checkpoint(source)["build"] is None
    assert (repo / "code.py").read_text() == "partial implementation\n"
    before = retained(source)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert [p.owner for p, _ in run.calls] == ["builder", "reviewer", "documenter"]
    assert retained(source) == before
    assert checkpoint(run)["source"] == source.adw_id
    receipt = json.loads((run.session_dir / "ticket-acceptance.json").read_text())
    assert receipt["adw_id"] == run.adw_id
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("owner", ["reviewer", "documenter"])
@pytest.mark.parametrize("mode", ["resume", "retry"])
def test_completed_builder_is_reused_only_for_resume(monkeypatch, repo, owner, mode):
    source = setup_flow(monkeypatch, repo, [review()], adw_build)
    fail_at(source, owner, RuntimeError("service temporarily unavailable"))
    with pytest.raises(RuntimeError):
        adw_build.main("Implement collection")
    assert checkpoint(source)["build"]["status"] == "success"
    before = retained(source)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(**{mode: source.adw_id})) == 0
    owners = [p.owner for p, _ in run.calls]
    assert ("builder" in owners) == (mode == "retry")
    assert owners[-1] == "documenter"
    assert ("reviewer" in owners) == (mode == "retry" or owner == "reviewer")
    assert any(p.params.name == "checks_1" for p in run.phases) == (mode == "retry")
    assert retained(source) == before


@pytest.mark.parametrize("change", ["code", "new_file", "index", "head", "definition", "commands"])
def test_changed_workspace_or_policy_rejected_before_agents(monkeypatch, repo, change):
    source = setup_flow(monkeypatch, repo, [], adw_build)
    fail_at(source, "reviewer", RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        adw_build.main("Implement collection")
    if change == "commands":
        spec = quality.check_specs()["test"].model_copy(update={"timeout_seconds": 99})
        monkeypatch.setattr(quality, "check_specs", lambda: {"test": spec})
    elif change == "index":
        git(repo, "add", "code.py")
    elif change == "head":
        git(repo, "commit", "--allow-empty", "-qm", "另一次提交")
    else:
        path = {"code": "code.py", "new_file": "unrelated.txt",
                "definition": "specs/request-flow/spec.md"}[change]
        (repo / path).write_text("external edit")
    before = git(repo, "status", "--porcelain")
    history = retained(source)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 2
    assert run.calls == []
    assert git(repo, "status", "--porcelain") == before
    assert retained(source) == history


def test_partial_repair_invalidates_build_and_keeps_budget_and_obligations(monkeypatch, repo):
    source = setup_flow(monkeypatch, repo, [review(blocker())], adw_build)
    original = source.call
    def repair_fails(params, request):
        if params.name.startswith("revise_"):
            (repo / "code.py").write_text("partial repair\n")
            raise RuntimeError("repair interrupted")
        return original(params, request)
    source.call = repair_fails
    with pytest.raises(RuntimeError):
        adw_build.main("Implement collection")
    saved = checkpoint(source)
    assert saved["build"] is None and saved["repairs"] == 1
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert run.calls[0][1].previous.blocking[0].id == "B-1"
    assert "Retain and reassess" in run.calls[1][1].previous.notes_for_next_agent
    assert checkpoint(run)["repairs"] == 1


def test_explicit_retry_resets_exhausted_budget(monkeypatch, repo):
    source = setup_flow(monkeypatch, repo, [review(blocker())] * 4, adw_build)
    assert adw_build.main("Implement collection") == 1
    assert checkpoint(source)["repairs"] == 3
    run = next_run(monkeypatch, source, [review(blocker()), review()])
    assert adw_build.main(BuildInput(retry=source.adw_id)) == 0
    assert checkpoint(run)["repairs"] == 1
    assert sum(p.owner == "builder" for p, _ in run.calls) == 2


def test_publication_failure_after_commit_can_be_revalidated(monkeypatch, repo):
    _, ticket, _ = ticket_plan(repo)
    source = setup_flow(monkeypatch, repo, [review()], adw_build)
    original = tickets.record_acceptance
    def fail(*_):
        raise OSError("receipt unavailable")
    monkeypatch.setattr(tickets, "record_acceptance", fail)
    assert adw_build.main(BuildInput(ticket=ticket)) == 1
    assert "receipt unavailable" in checkpoint(source)["error"]
    assert checkpoint(source)["workspace"]["head"] != checkpoint(source)["build_base"]
    before = retained(source)
    monkeypatch.setattr(tickets, "record_acceptance", original)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert "builder" not in [p.owner for p, _ in run.calls]
    assert retained(source) == before
    assert (run.session_dir / "ticket-acceptance.json").is_file()


@pytest.mark.parametrize("status", ["running", "completed", "missing", "same_id"])
def test_no_implicit_replay_of_live_successful_or_legacy_runs(monkeypatch, repo, status):
    source = setup_flow(monkeypatch, repo, [], adw_build)
    fail_at(source, "builder", RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        adw_build.main("Implement collection")
    path = source.session_dir / "delivery-checkpoint.json"
    if status == "missing":
        path.unlink()
    elif status != "same_id":
        saved = checkpoint(source)
        saved["status"] = status
        tickets.atomic_json(path, saved)
    if status != "same_id":
        run = next_run(monkeypatch, source)
    else:
        run = source
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 2
    assert run.calls == []


def test_recovery_target_is_exclusive():
    for values in ({"resume": "a", "retry": "b"}, {"resume": "a", "ticket": "ticket.md"},
                   {"retry": "a", "spec": "spec.md", "prompt": "extra"}, {"resume": "a", "ticket_set": "set.md"}):
        with pytest.raises(ValueError):
            BuildInput(**values)


def test_real_phase_saves_interruption_before_unlocking(monkeypatch, repo):
    from types import SimpleNamespace, MethodType
    from adw_modules import agents, permissions
    from adw_modules.runner import Run
    from adw_modules.tracer import Tracer

    source = setup_flow(monkeypatch, repo, [], adw_build)
    database = str(repo / "sessions/trace.db")
    source.cfg.observability = SimpleNamespace(db=database)
    source.tracer = Tracer(database, str(source.session_dir / "events.jsonl"))
    source.tracer.session_start(source.adw_id, "test")
    source._seq = source.tokens = 0
    source.cost = 0.0
    source._closed = False
    source.runtime = SimpleNamespace(close=lambda: None)
    source.workspace_lock = permissions.acquire_workspace_lock(repo)
    source.phase = MethodType(Run.phase, source)
    source.close = MethodType(Run.close, source)
    for name in ("phase_started", "phase_ended", "session_finished"):
        setattr(source.console, name, lambda *_: None)
    monkeypatch.setattr(agents, "execute", lambda run, phase, call: source.call(phase.params, call))
    stop = recovery.stop
    saved_while_locked = []
    def assert_locked(run, state, error):
        run.workspace_lock.assert_held()
        assert run._closed is False
        stop(run, state, error)
        saved_while_locked.append(True)
    monkeypatch.setattr(recovery, "stop", assert_locked)
    fail_at(source, "builder", SystemExit(143))
    with pytest.raises(SystemExit):
        adw_build.main("Implement collection")
    assert saved_while_locked == [True]
    assert source._closed
    saved = checkpoint(source)
    assert saved["failure_phase"] == "build" and saved["failure_owner"] == "builder"
    assert saved["error"] == "SystemExit: 143"
    assert source.tracer.conn.execute("SELECT status FROM sessions").fetchone()[0] == "fail"
    lock = permissions.acquire_workspace_lock(repo)
    lock.release()


def test_recovery_rejects_existing_destination_before_session_start(monkeypatch, repo):
    import sqlite3
    from types import SimpleNamespace
    from adw_modules import session
    source = setup_flow(monkeypatch, repo, [], adw_build)
    source.cfg.observability = SimpleNamespace(db=str(repo / "sessions/trace.db"))
    with sqlite3.connect(source.cfg.observability.db) as connection:
        connection.execute("CREATE TABLE sessions(adw_id TEXT, status TEXT)")
        connection.execute("INSERT INTO sessions VALUES('old-trace-only', 'fail')")
    monkeypatch.setattr(session, "ensure", lambda *_: pytest.fail("must reject before modifying the source"))
    for destination in (source.adw_id, "old-trace-only", "../escape"):
        with pytest.raises(ValueError):
            session.new_attempt(source.cfg, destination)
    with sqlite3.connect(source.cfg.observability.db) as connection:
        assert connection.execute("SELECT status FROM sessions").fetchone()[0] == "fail"


def test_changed_retained_report_rejected(monkeypatch, repo):
    source = setup_flow(monkeypatch, repo, [review()], adw_build)
    fail_at(source, "documenter", RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        adw_build.main("Implement collection")
    (source.context_handoff_dir / "review.md").write_text("tampered proof")
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 2
    assert run.calls == []


def test_sdlc_delivery_resumes_without_planning_again(monkeypatch, repo):
    from test_review_routing import adw_simple_sdlc
    from adw_modules.data_types import PlanningTarget
    source = setup_flow(monkeypatch, repo, [], adw_simple_sdlc)
    fail_at(source, "reviewer", RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        adw_simple_sdlc.main("Implement collection", target=PlanningTarget(spec_dir="specs/collection"))
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert [p.owner for p, _ in run.calls] == ["reviewer", "documenter"]


def test_recovery_protects_entire_source_session_from_builder(monkeypatch, repo):
    from types import SimpleNamespace
    from adw_modules import permissions
    from test_tickets import phase
    source = setup_flow(monkeypatch, repo, [review()], adw_build)
    fail_at(source, "documenter", RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        adw_build.main("Implement collection")
    before = retained(source)
    run = next_run(monkeypatch, source)
    run.cfg.defaults.protected_files = []
    run.delivery_recovery = recovery.load(run, source.adw_id, "resume")
    snapshot = permissions.snapshot(run)
    try:
        (source.context_handoff_dir / "review.md").write_text("overwrite old history")
        (source.session_dir / "new-proof.txt").write_text("invent old proof")
        with pytest.raises(permissions.PermissionBreach):
            permissions.enforce(run, phase(run), SimpleNamespace(name="builder", writes=None), snapshot)
        assert retained(source) == before
    finally:
        snapshot.cleanup()


def test_builder_artifacts_can_include_code_and_json(monkeypatch, repo):
    source = setup_flow(monkeypatch, repo, [], adw_build)
    original = source.call
    def call(params, request):
        if params.owner == "reviewer":
            raise RuntimeError("offline")
        output = original(params, request)
        if params.owner == "builder":
            report = source.context_handoff_dir / "implementation.json"
            report.write_text('{"completed": true}')
            output.artifacts = ["code.py", report.relative_to(repo).as_posix()]
        return output
    source.call = call
    with pytest.raises(RuntimeError, match="offline"):
        adw_build.main("Implement collection")
    assert {ref["path"] for ref in checkpoint(source)["reports"]} == {
        "code.py", "sessions/flow/context_handoff/implementation.json"}
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert "builder" not in [p.owner for p, _ in run.calls]
