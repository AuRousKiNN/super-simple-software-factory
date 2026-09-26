"""Fault injection at durable boundaries must resume only unfinished work."""
import json
import pytest

from test_tickets import repo, git
from test_delivery import adw_build, ticket_plan
from test_review_routing import review, setup_flow
from test_recovery import fail_at, next_run, retained, checkpoint
from adw_modules import quality, git_helper, tickets, spec_artifacts
from adw_modules.data_types import BuildInput


@pytest.mark.parametrize('owner,expected', [('reviewer',['reviewer','documenter']), ('documenter',['documenter'])])
def test_resume_starts_at_unfinished_agent(monkeypatch, repo, owner, expected):
    _,ticket,_=ticket_plan(repo)
    source=setup_flow(monkeypatch,repo,[review()],adw_build)
    fail_at(source,owner,RuntimeError('offline'))
    with pytest.raises(RuntimeError): adw_build.main(BuildInput(ticket=ticket))
    before=retained(source)
    run=next_run(monkeypatch,source)
    monkeypatch.setattr(quality,'run_selected',lambda *a:pytest.fail('successful checks replayed'))
    assert adw_build.main(BuildInput(resume=source.adw_id))==0
    assert [p.owner for p,c in run.calls]==expected
    assert retained(source)==before
    assert (run.session_dir/'ticket-acceptance.json').exists()


def test_completed_check_survives_interruption_of_next_check(monkeypatch, repo):
    source=setup_flow(monkeypatch,repo,[review()],adw_build)
    spec=quality.check_specs()['test']
    monkeypatch.setattr(quality,'check_specs',lambda:{'test':spec,'typecheck':spec.model_copy(update={'name':'typecheck'})})
    original=quality.run_selected
    def fail(run,names):
        if 'typecheck' in names: raise RuntimeError('interrupted check')
        return original(run,names)
    monkeypatch.setattr(quality,'run_selected',fail)
    with pytest.raises(RuntimeError): adw_build.main('Implement collection')
    seen=[]
    def record(run,names):
        seen.extend(names)
        return original(run,names)
    monkeypatch.setattr(quality,'run_selected',record)
    run=next_run(monkeypatch,source)
    assert adw_build.main(BuildInput(resume=source.adw_id))==0
    assert seen==['typecheck']


def test_commit_success_before_exception_is_not_repeated(monkeypatch, repo):
    _,ticket,_=ticket_plan(repo)
    source=setup_flow(monkeypatch,repo,[review()],adw_build)
    original=git_helper.commit_paths
    def fail(message,paths):
        original(message,paths)
        raise RuntimeError('lost commit response')
    monkeypatch.setattr(git_helper,'commit_paths',fail)
    with pytest.raises(RuntimeError): adw_build.main(BuildInput(ticket=ticket))
    delivered=git(repo,'rev-parse','HEAD')
    before=retained(source)
    messages=[]
    def record(message,paths):
        messages.append(message)
        return original(message,paths)
    monkeypatch.setattr(git_helper,'commit_paths',record)
    run=next_run(monkeypatch,source)
    assert adw_build.main(BuildInput(resume=source.adw_id))==0
    assert run.calls==[]
    assert all('实现集合返回值' not in m for m in messages)
    assert git(repo,'merge-base',delivered,'HEAD')==delivered
    assert retained(source)==before


def test_acceptance_failure_resumes_without_agents_or_checks(monkeypatch, repo):
    _,ticket,_=ticket_plan(repo)
    source=setup_flow(monkeypatch,repo,[review()],adw_build)
    original=tickets.record_acceptance
    monkeypatch.setattr(tickets,'record_acceptance',lambda *a: (_ for _ in ()).throw(OSError('publish unavailable')))
    assert adw_build.main(BuildInput(ticket=ticket))==1
    assert source.accepted is False
    monkeypatch.setattr(tickets,'record_acceptance',original)
    before=retained(source)
    run=next_run(monkeypatch,source)
    monkeypatch.setattr(quality,'run_selected',lambda *a:pytest.fail('checks replayed'))
    assert adw_build.main(BuildInput(resume=source.adw_id))==0
    assert run.calls==[]
    assert retained(source)==before


def test_check_log_change_invalidates_check_and_downstream(monkeypatch, repo):
    source=setup_flow(monkeypatch,repo,[review()],adw_build)
    fail_at(source,'documenter',RuntimeError('offline'))
    with pytest.raises(RuntimeError): adw_build.main('Implement collection')
    log=next(source.context_handoff_dir.glob('quality/**/command.log'))
    log.write_text('altered log')
    run=next_run(monkeypatch,source)
    assert adw_build.main(BuildInput(resume=source.adw_id))==0
    assert [p.owner for p,c in run.calls]==['reviewer','documenter']
    assert any(p.params.name.startswith('checks_') for p in run.phases)


def test_draft_survives_publication_failure(monkeypatch, repo):
    source=setup_flow(monkeypatch,repo,[review()],adw_build)
    original=spec_artifacts.publish
    monkeypatch.setattr(spec_artifacts,'publish',lambda *a: (_ for _ in ()).throw(OSError('publication failed')))
    with pytest.raises(OSError): adw_build.main('Implement collection')
    monkeypatch.setattr(spec_artifacts,'publish',original)
    run=next_run(monkeypatch,source)
    assert adw_build.main(BuildInput(resume=source.adw_id))==0
    assert run.calls==[]


@pytest.mark.parametrize("failed", [False, True])
def test_real_finish_reports_success_only_after_acceptance(monkeypatch, repo, failed):
    from types import MethodType, SimpleNamespace
    from adw_modules.runner import Run
    _, ticket, _ = ticket_plan(repo)
    run = setup_flow(monkeypatch, repo, [review()], adw_build)
    run.finish = MethodType(Run.finish, run)
    run.close = lambda: None
    run.tokens, run.cost = 0, None
    run.cfg.observability = SimpleNamespace(db="unused")
    events = []
    run.tracer.session_finish = lambda *a, **kw: events.append(("finish", kw["ok"]))
    run.console.session_finished = lambda *a: None
    original = tickets.record_acceptance
    def publish(*args):
        events.append(("publish", None))
        if failed:
            raise OSError("acceptance failed")
        return original(*args)
    monkeypatch.setattr(tickets, "record_acceptance", publish)
    assert adw_build.main(BuildInput(ticket=ticket)) == int(failed)
    assert events[0] == ("publish", None)
    assert events[1] == ("finish", not failed)
    assert run.accepted is (not failed)
    if failed:
        assert ("finish", True) not in events


def test_failed_check_is_retried_while_passed_check_is_reused(monkeypatch, repo):
    source = setup_flow(monkeypatch, repo, [], adw_build)
    spec = quality.check_specs()["test"]
    monkeypatch.setattr(quality, "check_specs", lambda: {
        "test": spec, "typecheck": spec.model_copy(update={"name": "typecheck"})})
    original = quality.run_selected
    def failed(run, names):
        result = original(run, names)
        if names == ["typecheck"]:
            result.checks[0].passed = False
            result.checks[0].returncode = 124
        return result
    monkeypatch.setattr(quality, "run_selected", failed)
    fail_at(source, "reviewer", RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        adw_build.main("Implement collection")
    seen = []
    def record(run, names):
        seen.extend(names)
        return original(run, names)
    monkeypatch.setattr(quality, "run_selected", record)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert seen == ["typecheck"]


@pytest.mark.parametrize("after_write", [False, True])
def test_partial_acceptance_pair_is_rolled_back_before_recovery(monkeypatch, repo, after_write):
    from adw_modules import acceptance_history
    _, ticket, _ = ticket_plan(repo)
    source = setup_flow(monkeypatch, repo, [review()], adw_build)
    original = acceptance_history.publish
    def fail(*args, **kwargs):
        if after_write:
            original(*args, **kwargs)
        raise OSError("ordering publication interrupted")
    monkeypatch.setattr(acceptance_history, "publish", fail)
    assert adw_build.main(BuildInput(ticket=ticket)) == 1
    assert not (source.session_dir / "ticket-acceptance.json").exists()
    assert not (source.session_dir / "ticket-acceptance-order.json").exists()
    before = retained(source)
    monkeypatch.setattr(acceptance_history, "publish", original)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert run.calls == []
    assert retained(source) == before
    assert len(acceptance_history.orders(run)) == 1


@pytest.mark.parametrize("after_commit", [False, True])
def test_partial_finish_projection_resumes_without_agents(monkeypatch, repo, after_commit):
    _, ticket, _ = ticket_plan(repo)
    source = setup_flow(monkeypatch, repo, [review()], adw_build)
    original = git_helper.commit_paths
    def interrupt(message, paths):
        if message == "同步规格验收事实与索引":
            if after_commit:
                original(message, paths)
            raise OSError("finish projection interrupted")
        return original(message, paths)
    monkeypatch.setattr(git_helper, "commit_paths", interrupt)
    assert adw_build.main(BuildInput(ticket=ticket)) == 1
    before = retained(source)
    monkeypatch.setattr(git_helper, "commit_paths", original)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(resume=source.adw_id)) == 0
    assert run.calls == []
    assert retained(source) == before
    assert git(repo, "status", "--porcelain") == ""
