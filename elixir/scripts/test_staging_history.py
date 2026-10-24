#!/usr/bin/env python3
"""End-to-end history-preserving staging policy test.

Uses an isolated JJ repo, producer Git checkout and repo config. It exercises:
- multi-commit reviewed stack plan/admission,
- fast-forward history preservation,
- idempotent re-admission,
- failed validation leaves staging unchanged,
- audited revert commit.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path('/tmp/symphony-history-test')
ELIXIR_ROOT = Path(__file__).resolve().parents[1]
GATE = ELIXIR_ROOT / 'symphony-stage-reviewed.py'
REVERT = ELIXIR_ROOT / 'symphony-stage-revert.py'


def run(argv, cwd=None, env=None):
    p = subprocess.run([str(a) for a in argv], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if p.returncode:
        raise RuntimeError(f"{' '.join(str(a) for a in argv)} failed ({p.returncode}):\n{p.stdout}")
    return p.stdout.strip()


def jj(repo, *args):
    return run(['jj', '--ignore-working-copy', '-R', str(repo), *args])


def commit_id(repo, rev):
    return jj(repo, 'log', '--no-graph', '-r', rev, '-T', 'commit_id')


def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    repo = ROOT / 'repo'
    producer = ROOT / 'producer' / 'PRO-999'
    reviewer = ROOT / 'reviewer'
    repo.mkdir(parents=True)
    producer.mkdir(parents=True)
    reviewer.mkdir(parents=True)

    run(['jj', 'git', 'init', '--colocate'], cwd=repo)
    run(['jj', 'config', 'set', '--repo', 'user.name', 'Symphony Test'], cwd=repo)
    run(['jj', 'config', 'set', '--repo', 'user.email', 'symphony-test@example.invalid'], cwd=repo)
    (repo / 'allowed.txt').write_text('base\n')
    run(['jj', 'commit', '-m', 'base'], cwd=repo)
    base = commit_id(repo, '@-')
    run(['jj', 'bookmark', 'create', 'staging', '-r', base], cwd=repo)

    subjects = []
    for index in range(1, 4):
        (repo / f'allowed{index}.txt').write_text(f'change {index}\n')
        message = f'meaningful change {index}'
        run(['jj', 'commit', '-m', message], cwd=repo)
        subjects.append(message)
    candidate = commit_id(repo, '@-')
    run(['jj', 'bookmark', 'create', 'symphony/PRO-999', '-r', candidate], cwd=repo)

    run(['git', 'init', '-q'], cwd=producer)
    run(['git', '-C', str(producer), 'fetch', str(repo), '+refs/heads/symphony/PRO-999:refs/heads/symphony/PRO-999'])

    config = {
        'schema': 'symphony-repo-scopes.v1',
        'repos': {
            'test': {
                'remote': 'file://' + str(repo),
                'jj_repo': str(repo),
                'producer_root': str(ROOT / 'producer'),
                'reviewer_root': str(reviewer),
                'staging_bookmark': 'staging',
                'bookmark_prefix': 'symphony/',
                'workspace_prefix': 'symphony-stage-test',
                'validation': [['sh', '-c', 'test -f allowed.txt && test -f allowed1.txt && test -f allowed2.txt && test -f allowed3.txt']],
                'issues': {'PRO-999': ['allowed.txt', 'allowed1.txt', 'allowed2.txt', 'allowed3.txt']},
            }
        },
        'issue_repos': {'PRO-999': 'test'},
    }
    config_path = ROOT / 'symphony-repos.json'
    config_path.write_text(json.dumps(config, indent=2))
    bad = json.loads(json.dumps(config))
    bad['repos']['test']['validation'] = [['sh', '-c', 'false']]
    bad_path = ROOT / 'symphony-repos-bad.json'
    bad_path.write_text(json.dumps(bad, indent=2))
    bad_format = json.loads(json.dumps(config))
    bad_format['repos']['test']['format_command'] = ['sh', '-c', 'printf "format drift\\n" >> allowed.txt']
    bad_format_path = ROOT / 'symphony-repos-bad-format.json'
    bad_format_path.write_text(json.dumps(bad_format, indent=2))

    decision_dir = reviewer / 'PRO-999'
    decision_dir.mkdir(parents=True)
    decision = {
        'schema': 'symphony-pilot-review-decision.v1',
        'issue': 'PRO-999',
        'verdict': 'approved',
        'candidate_sha': candidate,
        'base_sha': base,
        'reviewed_tests': ['sh -c "test -f allowed.txt && test -f allowed1.txt && test -f allowed2.txt && test -f allowed3.txt"'],
        'finding': 'Synthetic multi-commit stack reviewed for history-preserving staging policy test.',
    }
    decision_path = decision_dir / 'review-decision.json'
    decision_path.write_text(json.dumps(decision))

    # Failed validation leaves staging unchanged.
    bad_env = dict(os.environ, SYMPHONY_REPO_CONFIG=str(bad_path))
    before = commit_id(repo, 'staging')
    try:
        run([sys.executable, str(GATE), '--admit-local', str(decision_path)], env=bad_env)
        raise AssertionError('failed validation was accepted')
    except RuntimeError as exc:
        assert 'failed' in str(exc), exc
    assert commit_id(repo, 'staging') == before
    print('FAILED_VALIDATION_PRESERVED_STAGING')

    # A formatter that changes the reviewed candidate must fail closed before staging.
    bad_format_env = dict(os.environ, SYMPHONY_REPO_CONFIG=str(bad_format_path))
    before_format = commit_id(repo, 'staging')
    try:
        run([sys.executable, str(GATE), '--admit-local', str(decision_path)], env=bad_format_env)
        raise AssertionError('format drift was accepted')
    except RuntimeError as exc:
        assert 'Formatting changed the reviewed candidate' in str(exc), exc
    assert commit_id(repo, 'staging') == before_format
    print('FORMAT_DRIFT_PRESERVED_STAGING')

    env = dict(os.environ, SYMPHONY_REPO_CONFIG=str(config_path))
    plan = json.loads(run([sys.executable, str(GATE), '--plan', str(decision_path)], env=env))
    assert plan['mode'] == 'fast-forward', plan
    assert len(plan['candidate_commits']) == 3, plan
    assert [c['subject'] for c in plan['candidate_commits']] == subjects, plan

    admitted = json.loads(run([sys.executable, str(GATE), '--admit-local', str(decision_path)], env=env))
    assert admitted['integration_mode'] == 'fast-forward', admitted
    assert admitted['new_staging_sha'] == candidate, admitted
    assert commit_id(repo, 'staging') == candidate
    log = jj(repo, 'log', '--no-graph', '-r', 'ancestors(staging)', '-T', 'description.first_line() ++ "\\n"')
    for subject in subjects:
        assert subject in log, (subject, log)

    again = json.loads(run([sys.executable, str(GATE), '--admit-local', str(decision_path)], env=env))
    assert again['integration_mode'] == 'already-staged', again
    assert commit_id(repo, 'staging') == candidate

    # Audited revert of the actual integration.
    result_path = decision_dir / 'integration-result.json'
    revert_env = dict(os.environ, SYMPHONY_REPO_CONFIG=str(config_path))
    revert = json.loads(run([sys.executable, str(REVERT), str(result_path), '--reason', 'policy test rollback'], env=revert_env))
    assert revert['integration_tip'] == candidate, revert
    assert commit_id(repo, 'staging') == revert['revert_commit']
    old_tree = run(['git', '-C', str(repo), 'rev-parse', f'{base}^{{tree}}'])
    assert revert['revert_tree'] == old_tree, (revert['revert_tree'], old_tree)
    assert jj(repo, 'log', '--no-graph', '-r', f'({candidate}) & ancestors({revert["revert_commit"]})', '-T', 'commit_id') == candidate
    assert (decision_dir / 'revert-result.json').is_file()

    print(json.dumps({
        'plan_mode': plan['mode'],
        'stack': [c['subject'] for c in plan['candidate_commits']],
        'admitted_tip': admitted['new_staging_sha'],
        'validated_tree': admitted.get('validated_tree'),
        'idempotent_mode': again['integration_mode'],
        'revert_commit': revert['revert_commit'],
        'revert_tree': revert['revert_tree'],
    }, indent=2))
    print('HISTORY_POLICY_TEST_PASSED')


if __name__ == '__main__':
    main()
