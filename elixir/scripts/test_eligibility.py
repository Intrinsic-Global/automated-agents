#!/usr/bin/env python3
"""Adversarial tests for Symphony eligibility and workspace freshness."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import symphony_eligibility as elig


def run(argv, cwd=None):
    p = subprocess.run([str(a) for a in argv], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode:
        raise RuntimeError(f"{' '.join(str(a) for a in argv)} failed ({p.returncode}):\n{p.stdout}")
    return p.stdout.strip()


def jj(repo, *args):
    return run(['jj', '--ignore-working-copy', '-R', str(repo), *args])

def jj_mut(repo, *args):
    return run(['jj', '-R', str(repo), *args])


def commit(repo, rev='@-'):
    return jj(repo, 'log', '--no-graph', '-r', rev, '-T', 'commit_id')


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='symphony-eligibility-'))
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        run(['jj', 'git', 'init', '--colocate'], cwd=self.repo)
        run(['jj', 'config', 'set', '--repo', 'user.name', 'Symphony Test'], cwd=self.repo)
        run(['jj', 'config', 'set', '--repo', 'user.email', 'symphony-test@example.invalid'], cwd=self.repo)
        (self.repo / 'base.txt').write_text('base\n')
        run(['jj', 'commit', '-m', 'base'], cwd=self.repo)
        self.base = commit(self.repo)
        jj(self.repo, 'bookmark', 'create', 'staging', '-r', self.base)
        self.reviewer = self.root / 'reviewer'
        self.reviewer.mkdir()
        self.producer = self.root / 'producer'
        self.producer.mkdir()
        self.state_dir = self.root / 'state'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({
            'schema': 'symphony-repo-scopes.v1',
            'repos': {
                'test': {
                    'remote': 'file://' + str(self.repo),
                    'jj_repo': str(self.repo),
                    'producer_root': str(self.producer),
                    'reviewer_root': str(self.reviewer),
                    'staging_bookmark': 'staging',
                    'bookmark_prefix': 'symphony/',
                    'workspace_prefix': 'symphony-stage-test',
                    'validation': [['true']],
                    'issues': {'PRO-999': ['base.txt', 'dep.txt']},
                }
            },
            'issue_repos': {'PRO-999': 'test', 'PRO-880': 'test'},
        }), encoding='utf-8')
        self.workspace = self.root / 'workspace' / 'PRO-999'
        self.workspace.parent.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_state_json_write_is_private_and_leaves_no_temp_file(self):
        target = self.state_dir / 'issues' / 'PRO-999.json'
        elig.atomic_write_json(target, {'schema': elig.SCHEMA, 'issue': 'PRO-999'})

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['issue'], 'PRO-999')
        self.assertEqual(list(target.parent.glob('.PRO-999.json.*.tmp')), [])

    def issue(self, blockers=None, state='Todo'):
        nodes = [{'type': 'blocks', 'issue': {'identifier': b, 'state': {'name': s}}} for b, s in (blockers or [])]
        return {
            'id': 'uuid-' + state,
            'identifier': 'PRO-999',
            'state': {'name': state},
            'labels': {'nodes': []},
            'inverseRelations': {'nodes': nodes},
            'team': {'states': {'nodes': [{'id': 'done', 'name': 'Done'}]}},
        }

    def check(self, phase, issue=None, staging_tip=None):
        return elig.check_phase(
            'PRO-999', phase,
            explicit_config=str(self.config),
            explicit_state_dir=str(self.state_dir),
            staging_tip=staging_tip,
            linear_data=issue or self.issue(),
        )

    def record_workspace(self, base=None):
        jj_mut(self.repo, 'workspace', 'add', str(self.workspace), '--revision', base or self.base)
        return elig.record_workspace(
            'PRO-999', str(self.workspace), base or self.base, fresh=True,
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )

    def test_dispatch_without_dependencies_is_eligible(self):
        result = self.check('dispatch')
        self.assertEqual(result['status'], 'eligible', result)

    def test_dependency_done_without_receipt_blocks(self):
        result = self.check('dispatch', self.issue([('PRO-880', 'Done')]))
        self.assertEqual(result['status'], 'blocked', result)
        self.assertTrue(any('dependency_receipt_missing' in x for x in result['reasons']), result)

    def test_dependency_not_done_blocks(self):
        result = self.check('dispatch', self.issue([('PRO-880', 'In Progress')]))
        self.assertEqual(result['status'], 'blocked', result)
        self.assertTrue(any('dependency_not_accepted' in x for x in result['reasons']), result)

    def test_dependency_receipt_in_staging_is_eligible(self):
        staging_tip = commit(self.repo, 'staging')
        elig.record_staging('PRO-880', {
            'issue': 'PRO-880',
            'candidate_sha': staging_tip,
            'new_staging_sha': staging_tip,
        }, explicit_config=str(self.config), explicit_state_dir=str(self.state_dir))
        result = self.check('dispatch', self.issue([('PRO-880', 'Done')]), staging_tip=staging_tip)
        self.assertEqual(result['status'], 'eligible', result)

    def test_dependency_receipt_not_in_staging_blocks(self):
        staging_tip = commit(self.repo, 'staging')
        (self.repo / 'other.txt').write_text('other\n')
        run(['jj', 'commit', '-m', 'other'], cwd=self.repo)
        other = commit(self.repo)
        elig.record_staging('PRO-880', {
            'issue': 'PRO-880',
            'candidate_sha': other,
            'new_staging_sha': other,
        }, explicit_config=str(self.config), explicit_state_dir=str(self.state_dir))
        result = self.check('dispatch', self.issue([('PRO-880', 'Done')]), staging_tip=staging_tip)
        self.assertEqual(result['status'], 'blocked', result)

    def test_workspace_fresh_then_stale_ancestor(self):
        self.record_workspace()
        fresh = self.check('resume', self.issue([], 'In Progress'))
        self.assertEqual(fresh['status'], 'eligible', fresh)
        (self.repo / 'next.txt').write_text('next\n')
        run(['jj', 'commit', '-m', 'next'], cwd=self.repo)
        next_commit = commit(self.repo)
        jj(self.repo, 'bookmark', 'set', 'staging', '-r', next_commit)
        stale = self.check('resume', self.issue([], 'In Progress'))
        self.assertEqual(stale['status'], 'stale', stale)
        self.assertIn('workspace_base_is_stale_ancestor', stale['reasons'])

    def test_workspace_diverged(self):
        first = self.base
        (self.repo / 'a.txt').write_text('a\n')
        run(['jj', 'commit', '-m', 'a'], cwd=self.repo)
        side = commit(self.repo)
        jj_mut(self.repo, 'new', first, '-m', 'b')
        run(['jj', 'commit', '-m', 'b'], cwd=self.repo)
        other = commit(self.repo)
        jj(self.repo, 'bookmark', 'set', 'staging', '-r', other)
        self.record_workspace(base=side)
        result = self.check('resume', self.issue([], 'In Progress'))
        self.assertEqual(result['status'], 'diverged', result)

    def test_review_candidate_change_invalidates(self):
        self.record_workspace()
        candidate = self.base
        elig.record_candidate('PRO-999', candidate, self.base, explicit_config=str(self.config), explicit_state_dir=str(self.state_dir))
        decision_dir = self.reviewer / 'PRO-999'
        decision_dir.mkdir()
        decision = decision_dir / 'review-decision.json'
        decision.write_text(json.dumps({
            'schema': 'symphony-pilot-review-decision.v1',
            'issue': 'PRO-999',
            'verdict': 'approved',
            'candidate_sha': candidate,
            'base_sha': self.base,
            'reviewed_tests': ['true'],
            'finding': 'synthetic independent review finding for eligibility test',
        }))
        elig.record_review('PRO-999', str(decision), explicit_config=str(self.config), explicit_state_dir=str(self.state_dir))
        eligible = self.check('stage')
        self.assertEqual(eligible['status'], 'eligible', eligible)
        (self.repo / 'changed.txt').write_text('changed\n')
        run(['jj', 'commit', '-m', 'changed'], cwd=self.repo)
        changed = commit(self.repo)
        elig.record_candidate('PRO-999', changed, self.base, explicit_config=str(self.config), explicit_state_dir=str(self.state_dir))
        blocked = self.check('stage')
        self.assertEqual(blocked['status'], 'blocked', blocked)
        self.assertTrue(any('review_invalid' in x for x in blocked['reasons']), blocked)

    def test_dependency_added_after_resume_blocks(self):
        self.record_workspace()
        before = self.check('resume', self.issue([], 'In Progress'))
        self.assertEqual(before['status'], 'eligible', before)
        after = self.check('resume', self.issue([('PRO-880', 'Done')], 'In Progress'))
        self.assertEqual(after['status'], 'blocked', after)

    def test_staging_requires_bound_review(self):
        self.record_workspace()
        elig.record_candidate(
            'PRO-999', self.base, self.base,
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )
        result = self.check('stage')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertTrue(any('review_invalid' in x for x in result['reasons']), result)

    def test_workspace_check_is_read_only(self):
        self.record_workspace()
        dirty = self.workspace / 'agent-note.txt'
        dirty.write_text('preserve me\n')
        result = self.check('resume', self.issue([], 'In Progress'))
        self.assertEqual(result['status'], 'eligible', result)
        self.assertTrue(dirty.is_file())
        self.assertEqual(dirty.read_text(), 'preserve me\n')

    def test_state_survives_reload(self):
        self.record_workspace()
        state = elig.load_state('PRO-999', explicit=str(self.state_dir))
        self.assertEqual(state['workspace'], str(self.workspace.resolve()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
