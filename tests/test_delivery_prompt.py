"""Supplemental instructions survive target binding, repair and recovery."""
import json
import pytest

from test_tickets import repo, git
from test_delivery import ticket_plan, adw_build
from test_review_routing import review, blocker, setup_flow
from test_recovery import fail_at, next_run, retained, checkpoint
from adw_modules import delivery, tickets
from adw_modules.data_types import BuildInput


@pytest.mark.parametrize('mode', ['spec', 'ticket', 'resume', 'retry'])
def test_prompt_is_available_with_every_target(mode):
    assert BuildInput(prompt='额外指令', **{mode: 'target'}).prompt == '额外指令'


@pytest.mark.parametrize('mode', ['spec', 'ticket'])
def test_instructions_reach_build_repair_review_and_documenter(monkeypatch, repo, mode):
    _, ticket, _ = ticket_plan(repo)
    prompt = '保留现有接口\n补充中文说明'
    target = {'ticket': ticket} if mode == 'ticket' else {'spec': 'specs/demo/spec.md'}
    run = setup_flow(monkeypatch, repo, [review(blocker()), review()], adw_build)
    assert adw_build.main(BuildInput(prompt=prompt, **target)) == 0
    for phase, call in run.calls:
        if phase.owner in {'builder', 'reviewer'}:
            assert call.prompt == prompt
        elif phase.owner == 'documenter':
            assert prompt in call.document_context.purpose
    assert checkpoint(run)['prompt'] == prompt
    assert json.loads((run.session_dir / 'delivery-input.json').read_text())['prompt'] == prompt


@pytest.mark.parametrize('mode', ['resume', 'retry'])
def test_recovery_appends_instructions_and_revisits_completed_build(monkeypatch, repo, mode):
    _, ticket, _ = ticket_plan(repo)
    source = setup_flow(monkeypatch, repo, [], adw_build)
    fail_at(source, 'reviewer', RuntimeError('offline'))
    with pytest.raises(RuntimeError):
        adw_build.main(BuildInput(ticket=ticket, prompt='原始指令'))
    assert checkpoint(source)['build'] is not None
    before = retained(source)
    run = next_run(monkeypatch, source)
    assert adw_build.main(BuildInput(prompt='新增指令', **{mode: source.adw_id})) == 0
    assert [p.owner for p, c in run.calls] == ['builder', 'reviewer', 'documenter']
    for phase, call in run.calls:
        value = call.document_context.purpose if phase.owner == 'documenter' else call.prompt
        assert '原始指令' in value and '新增指令' in value
    assert checkpoint(run)['work_item'] == checkpoint(source)['work_item']
    assert retained(source) == before


def test_bound_prompt_cannot_change_inside_an_attempt(monkeypatch, repo):
    ticket_plan(repo)
    run = setup_flow(monkeypatch, repo, [], adw_build)
    request = delivery.DeliveryRequest('原始指令', tickets.spec_work_item(repo, 'specs/demo/spec.md'),
                                       git(repo, 'rev-parse', 'HEAD'))
    delivery.freeze_input(run, request)
    request.prompt = 'changed'
    with pytest.raises(ValueError, match='frozen delivery inputs'):
        delivery.verify_frozen_input(run, request)
