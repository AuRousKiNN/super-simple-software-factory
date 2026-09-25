"""Delivery entrypoints must share checks, review and ticket acceptance policy."""
from pathlib import Path
import importlib
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / '.agents/skills/sssf/templates/adws'
sys.path.insert(0, str(ADWS))


def test_distribution_has_only_nine_hyphenated_workflows():
    assert {p.stem for p in ADWS.glob('adw[-_]*.py')} == {
        'adw-prompt', 'adw-scout', 'adw-plan', 'adw-decompose',
        'adw-plan-decompose', 'adw-build', 'adw-quality', 'adw-recheck',
        'adw-simple-sdlc',
    }


def test_delivery_preflight_rejects_placeholder_before_any_agent():
    from adw_modules import quality
    with pytest.raises(ValueError, match='configured'):
        quality.required_checks()


def test_build_and_sdlc_use_the_same_delivery_module():
    build = importlib.import_module('adw-build')
    sdlc = importlib.import_module('adw-simple-sdlc')
    assert build.delivery is sdlc.delivery

import json
from test_tickets import repo, planning, git
from test_review_routing import Flow, review, blocker, setup_flow
from adw_modules import delivery, quality, tickets, review_routing, spec_artifacts
from adw_modules.data_types import BuildInput, RecheckRequest, QualityCheckSpec, ReviewObligation

adw_build = importlib.import_module('adw-build')
adw_recheck = importlib.import_module('adw-recheck')


def ticket_plan(root):
    set_path, members = planning(root, {'TICKET-A': [], 'TICKET-B': ['TICKET-A']})
    directory = root / 'specs/demo'
    directory.mkdir()
    (root / 'specs/root.md').rename(directory / 'spec.md')
    (root / 'specs/root.tickets').rename(directory / 'spec.tickets')
    for path in directory.rglob('*.md'):
        path.write_text(path.read_text().replace('specs/root.md', 'specs/demo/spec.md')
                        .replace('specs/root.tickets', 'specs/demo/spec.tickets'))
    set_path = 'specs/demo/spec.tickets/ticket-set.md'
    tickets.write_index(root, set_path)
    git(root, 'add', 'specs')
    git(root, 'commit', '-qm', '记录票据定义')
    return set_path, 'specs/demo/spec.tickets/tickets/TICKET-A.md', 'specs/demo/spec.tickets/tickets/TICKET-B.md'


def build_ticket(monkeypatch, root, verdict=None):
    set_path, first, second = ticket_plan(root)
    run = setup_flow(monkeypatch, root, [verdict or review()], adw_build)
    result = adw_build.main(BuildInput(ticket=first, ticket_set=set_path))
    return run, result, set_path, first, second


def test_ticket_delivery_produces_consumable_current_head_acceptance(monkeypatch, repo):
    run, result, set_path, first, second = build_ticket(monkeypatch, repo)
    assert result == 0
    record = json.loads((run.session_dir / 'ticket-acceptance.json').read_text())
    assert record['baseline'] == git(repo, 'rev-parse', 'HEAD')
    assert record['checks'] and record['reviews']
    assert git(repo, 'status', '--porcelain') == ''
    evidence = run.session_dir / 'dependencies.json'
    evidence.write_text(json.dumps([tickets.artifact(repo, (run.session_dir / 'ticket-acceptance.json').relative_to(repo).as_posix(), '.json').model_dump()]))
    downstream = Flow(repo, name='downstream')
    item = tickets.ticket_work_item(downstream, set_path, second, evidence.relative_to(repo).as_posix())
    tickets.validate_work_item(downstream, item)
    assert item.ticket_id == 'TICKET-B'
    assert [p.owner for p, c in run.calls] == ['builder', 'reviewer', 'documenter']


def test_approved_ticket_rechecks_on_new_head_without_builder(monkeypatch, repo):
    source, result, set_path, first, second = build_ticket(monkeypatch, repo)
    assert result == 0
    (repo / 'code.py').write_text('value = 20\n')
    git(repo, 'add', 'code.py')
    git(repo, 'commit', '-qm', '更新实现基线')
    receipt = next((source.session_dir / 'review-routing').glob('*.json'))
    request = RecheckRequest(original_review=tickets.artifact(repo, receipt.relative_to(repo).as_posix(), '.json'),
                             baseline=git(repo, 'rev-parse', 'HEAD'))
    request_path = source.session_dir / 'recheck.json'
    request_path.write_text(request.model_dump_json())
    run = Flow(repo, [review()], name='revalidate')
    monkeypatch.setattr(adw_recheck.session, 'ensure', lambda *_: run)
    assert adw_recheck.main(str(request_path)) == 0
    assert [p.owner for p, c in run.calls] == ['scout', 'reviewer', 'documenter']
    record = json.loads((run.session_dir / 'ticket-acceptance.json').read_text())
    assert record['baseline'] == git(repo, 'rev-parse', 'HEAD')
    assert record['baseline'] != json.loads((source.session_dir / 'ticket-acceptance.json').read_text())['baseline']
    assert git(repo, 'status', '--porcelain') == ''


def test_manual_blocker_cannot_issue_ticket_acceptance(monkeypatch, repo):
    run, result, *_ = build_ticket(monkeypatch, repo, review(blocker('manual_validation')))
    assert result == 1
    assert not (run.session_dir / 'ticket-acceptance.json').exists()


def test_unconfigured_checks_stop_before_builder(monkeypatch, repo):
    run = setup_flow(monkeypatch, repo, [], adw_build)
    monkeypatch.setattr(quality, 'check_specs', lambda: {})
    assert adw_build.main('Implement this') == 2
    assert 'preflight_rejected' in run.reason
    assert 'not configured' in run.reason
    assert run.calls == []


def test_failed_checks_cannot_be_overridden_by_review(monkeypatch, repo):
    run = setup_flow(monkeypatch, repo, [review()], adw_build)
    monkeypatch.setattr(quality, 'check_specs', lambda: {'test': QualityCheckSpec(
        name='test', area='backend', operation='build', argv=[sys.executable, '-c', 'exit(1)'])})
    assert adw_build.main('Implement this') == 1
    assert 'failed' in run.reason
    assert not (run.session_dir / 'ticket-acceptance.json').exists()


def test_commit_hook_mutation_blocks_acceptance(monkeypatch, repo):
    from adw_modules import git_helper
    original = git_helper.commit_paths
    def mutate(message, paths):
        sha = original(message, paths)
        (repo / 'code.py').write_text('unchecked mutation\n')
        return sha
    monkeypatch.setattr(git_helper, 'commit_paths', mutate)
    with pytest.raises(ValueError, match='changed after verification'):
        build_ticket(monkeypatch, repo)
    assert not list((repo / 'sessions').glob('*/ticket-acceptance.json'))


def test_two_prerequisites_can_revalidate_across_documentation_commits(monkeypatch, repo):
    # Prior acceptance remains usable across commits that only update specification records.
    from test_tickets import write_ticket
    set_path, first, second = ticket_plan(repo)
    write_ticket(repo / second, 'TICKET-B', [])
    third = 'specs/demo/spec.tickets/tickets/TICKET-C.md'
    write_ticket(repo / third, 'TICKET-C', ['TICKET-A', 'TICKET-B'])
    import yaml
    path = repo / set_path
    meta = yaml.safe_load(path.read_text().split('---')[1])
    meta['tickets'].append(third)
    path.write_text('---\n' + yaml.safe_dump(meta) + '---\nTicket set\n')
    tickets.write_index(repo, set_path)
    git(repo, 'add', 'specs')
    git(repo, 'commit', '-qm', '定义两个前置票据')
    run = setup_flow(monkeypatch, repo, [review()], adw_build)
    assert adw_build.main(BuildInput(ticket=first, ticket_set=set_path)) == 0
    sources = [run]
    run = Flow(repo, [review()], name='build-b')
    monkeypatch.setattr(adw_build.session, 'ensure', lambda *_: run)
    assert adw_build.main(BuildInput(ticket=second, ticket_set=set_path)) == 0
    sources.append(run)
    original_records = [json.loads((s.session_dir / 'ticket-acceptance.json').read_text()) for s in sources]
    assert len({r['baseline'] for r in original_records}) == 2
    manifest = sources[-1].session_dir / 'original-dependencies.json'
    manifest.write_text(json.dumps([tickets.artifact(repo,
        (s.session_dir / 'ticket-acceptance.json').relative_to(repo).as_posix(), '.json').model_dump()
        for s in sources]))
    downstream = Flow(repo, name='bind-original-c')
    item = tickets.ticket_work_item(downstream, set_path, third, manifest.relative_to(repo).as_posix())
    tickets.validate_work_item(downstream, item)
    assert item.ticket_id == 'TICKET-C'
    head = git(repo, 'rev-parse', 'HEAD')
    refs = []
    for index, source in enumerate(sources):
        receipt = sorted((source.session_dir / 'review-routing').glob('*.json'))[-1]
        request = RecheckRequest(original_review=tickets.artifact(repo, receipt.relative_to(repo).as_posix(), '.json'), baseline=head)
        request_path = source.session_dir / 'recheck.json'
        request_path.write_text(request.model_dump_json())
        run = Flow(repo, [review()], name=f'recheck-{index}')
        monkeypatch.setattr(adw_recheck.session, 'ensure', lambda *_: run)
        assert adw_recheck.main(str(request_path)) == 0
        new_head = git(repo, 'rev-parse', 'HEAD')
        assert new_head != head
        assert all(spec_artifacts.managed_path(p) for p in git(repo, 'diff', '--name-only', head, new_head).splitlines())
        head = new_head
        assert git(repo, 'status', '--porcelain') == ''
        refs.append(tickets.artifact(repo, (run.session_dir / 'ticket-acceptance.json').relative_to(repo).as_posix(), '.json'))
    manifest = run.session_dir / 'dependencies.json'
    manifest.write_text(json.dumps([r.model_dump() for r in refs]))
    downstream = Flow(repo, name='build-c')
    item = tickets.ticket_work_item(downstream, set_path, third, manifest.relative_to(repo).as_posix())
    tickets.validate_work_item(downstream, item)
    assert item.ticket_id == 'TICKET-C'


def test_entrypoint_cli_help_and_generator_names(tmp_path):
    import subprocess
    for path in ADWS.glob('adw-*.py'):
        result = subprocess.run([sys.executable, str(path), '--help'], capture_output=True, text=True)
        assert result.returncode == 0, (path, result.stderr)
    script = ROOT / '.agents/skills/sssf/scripts/make_adw.py'
    result = subprocess.run([sys.executable, str(script), '--name', 'read_only', '--agents', 'scout'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert (tmp_path / 'adws/adw-read-only.py').is_file()


@pytest.mark.parametrize('customized', [False, True])
def test_installer_retires_only_untouched_legacy_entries(tmp_path, customized):
    import subprocess
    installer = ROOT / '.agents/skills/sssf/scripts/install.py'
    def invoke(*args):
        return subprocess.run([sys.executable, str(installer), '--root', str(tmp_path), *args], capture_output=True, text=True)
    assert invoke().returncode == 0
    new = tmp_path / 'adws/adw-build.py'
    old = tmp_path / 'adws/adw_build.py'
    new.rename(old)
    manifest_path = tmp_path / '.sssf/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files']['adws/adw_build.py'] = manifest['files'].pop('adws/adw-build.py')
    manifest_path.write_text(json.dumps(manifest))
    original = old.read_bytes()
    if customized:
        old.write_bytes(original + b'\n# custom delivery\n')
    result = invoke('--force-managed')
    if customized:
        assert result.returncode == 2
        assert old.read_bytes().endswith(b'# custom delivery\n')
        assert not new.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert new.is_file() and not old.exists()
        assert invoke('--rollback', 'latest').returncode == 0
        assert old.read_bytes() == original and not new.exists()


@pytest.mark.parametrize("ticket_mode", [False, True])
@pytest.mark.parametrize("approved", [False, True])
def test_recheck_replaces_failed_canonical_acceptance(monkeypatch, repo, ticket_mode, approved):
    from adw_modules.data_types import RecheckEvidence
    if ticket_mode:
        source, result, *_ = build_ticket(monkeypatch, repo, review(blocker("environment")))
        overview = repo / "specs/demo/README.md"
    else:
        source = setup_flow(monkeypatch, repo, [review(blocker("environment"))], adw_build)
        result = adw_build.main("Implement collection")
        overview = repo / "specs/request-flow/README.md"
    assert result == 1
    assert spec_artifacts._read(overview)[0]["acceptance"]["accepted"] is False
    git(repo, "add", "code.py", "specs")
    git(repo, "commit", "-qm", "保留失败交付供复查")
    original_report = next(overview.parent.glob("executions/*/*.md"))
    original_bytes = original_report.read_bytes()
    receipt = next((source.session_dir / "review-routing").glob("*.json"))
    proof = source.session_dir / "environment.md"
    proof.write_text("Environment restored and verified")
    request = RecheckRequest(
        original_review=tickets.artifact(repo, receipt.relative_to(repo).as_posix(), ".json"),
        baseline=git(repo, "rev-parse", "HEAD"),
        evidence=[RecheckEvidence(artifact=tickets.artifact(repo, proof.relative_to(repo).as_posix()),
                  resolves=["B-1"], applicability="Restored environment on current implementation")])
    request_path = source.session_dir / "recheck.json"
    request_path.write_text(request.model_dump_json())
    run = Flow(repo, [review() if approved else review(blocker("environment"))], name="recheck-result")
    monkeypatch.setattr(adw_recheck.session, "ensure", lambda *_: run)
    assert adw_recheck.main(str(request_path)) == (0 if approved else 1)
    meta, _ = spec_artifacts._read(overview)
    assert meta["run_result"]["adw_id"] == run.adw_id
    assert meta["acceptance"]["accepted"] is approved
    assert meta["acceptance"]["scope"] == ("TICKET-A" if ticket_mode else "整个规格")
    assert run.adw_id in meta["acceptance"]["source"]
    assert (overview.parent / meta["latest_execution"]).is_file()
    assert original_report.read_bytes() == original_bytes
    assert "builder" not in [p.owner for p, _ in run.calls]
    assert ("通过" if approved else "未验收") in (repo / "specs/README.md").read_text()
