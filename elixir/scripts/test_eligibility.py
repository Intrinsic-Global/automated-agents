#!/usr/bin/env python3
"""Adversarial tests for Symphony eligibility and workspace freshness."""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import symphony_eligibility as elig


def run(argv, cwd=None):
    p = subprocess.run([str(a) for a in argv], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode:
        raise RuntimeError(f"{' '.join(str(a) for a in argv)} failed ({p.returncode}):\n{p.stdout}")
    return p.stdout.strip()


def jj(repo, *args):
    return run(['jj', '--ignore-working-copy', '-R', str(repo), *args], cwd=repo)

def jj_mut(repo, *args):
    return run(['jj', '-R', str(repo), *args], cwd=repo)


def commit(repo, rev='@-'):
    return jj(repo, 'log', '--no-graph', '-r', rev, '-T', 'commit_id')


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='symphony-eligibility-'))
        self.previous_xdg_config_home = os.environ.get('XDG_CONFIG_HOME')
        os.environ['XDG_CONFIG_HOME'] = str(self.root / 'xdg-config')
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
        if self.previous_xdg_config_home is None:
            os.environ.pop('XDG_CONFIG_HOME', None)
        else:
            os.environ['XDG_CONFIG_HOME'] = self.previous_xdg_config_home
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
            'description': 'test task contract',
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

    def publish_candidate(self, candidate):
        producer_issue = self.producer / 'PRO-999'
        producer_issue.mkdir(parents=True, exist_ok=True)
        if not (producer_issue / '.git').is_dir():
            run(['git', 'init', '-q', str(producer_issue)])
        run([
            'git', '-C', str(producer_issue), 'fetch', '--no-tags', str(self.repo),
            f'+{candidate}:refs/heads/symphony/PRO-999',
        ])

    def test_dispatch_without_dependencies_is_eligible(self):
        result = self.check('dispatch')
        self.assertEqual(result['status'], 'eligible', result)

    def test_workspace_only_repository_uses_pinned_base_and_rejects_review(self):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['repos']['test']['workspace_only'] = True
        config['repos']['test']['staging_bookmark'] = None
        config['repos']['test']['base_revision'] = self.base
        self.config.write_text(json.dumps(config), encoding='utf-8')

        result = self.check('dispatch')
        self.assertEqual(result['status'], 'eligible', result)
        self.assertEqual(result['staging_tip'], self.base)
        with self.assertRaisesRegex(RuntimeError, 'no configured staging/review handoff'):
            self.check('review')

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

    def test_workspace_fresh_then_pinned_ancestor_remains_resumable(self):
        self.record_workspace()
        fresh = self.check('resume', self.issue([], 'In Progress'))
        self.assertEqual(fresh['status'], 'eligible', fresh)
        (self.repo / 'next.txt').write_text('next\n')
        run(['jj', 'commit', '-m', 'next'], cwd=self.repo)
        next_commit = commit(self.repo)
        jj(self.repo, 'bookmark', 'set', 'staging', '-r', next_commit)
        stale = self.check('resume', self.issue([], 'In Progress'))
        self.assertEqual(stale['status'], 'eligible', stale)
        self.assertIn('workspace_base_behind_staging; preserving the pinned workspace base', stale['warnings'])
        self.assertEqual(self.base, stale['workspace']['base'])

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
        self.publish_candidate(candidate)
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
        run(['jj', 'edit', changed], cwd=self.workspace)
        elig.record_candidate('PRO-999', changed, self.base, explicit_config=str(self.config), explicit_state_dir=str(self.state_dir))
        self.publish_candidate(changed)
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

    def test_review_without_a_pinned_candidate_is_blocked(self):
        self.record_workspace()
        result = self.check('review', self.issue([], 'In Review'))
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('candidate_missing', result['reasons'])

    def prepare_review_candidate(self, candidate=None):
        candidate = candidate or self.base
        self.record_workspace()
        elig.record_candidate(
            'PRO-999', candidate, self.base,
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )
        self.publish_candidate(candidate)
        return candidate

    def test_same_review_identity_is_claimed_once_and_stays_blocked_after_restart(self):
        candidate = self.prepare_review_candidate()
        issue = self.issue([], 'In Review')
        first = elig.claim_review_attempt(
            'PRO-999', explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            issue_data=issue,
        )
        self.assertTrue(first['claimed'], first)

        for _ in range(100):
            result = self.check('review', issue)
            self.assertEqual(result['status'], 'review_attempt_seen', result)
            self.assertEqual(result['review_identity'], first['identity'])

        # A fresh process reads the same durable claim and cannot acquire it.
        second = elig.claim_review_attempt(
            'PRO-999', explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            issue_data=issue,
        )
        self.assertFalse(second['claimed'], second)
        self.assertEqual(second['identity'], first['identity'])
        saved = elig.load_state('PRO-999', explicit=str(self.state_dir))
        self.assertEqual(candidate, saved['review_attempts'][first['identity']]['inputs']['candidate_commit'])

    def test_concurrent_claims_for_same_candidate_have_one_winner(self):
        self.prepare_review_candidate()
        issue = self.issue([], 'In Review')

        def claim():
            return elig.claim_review_attempt(
                'PRO-999', explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
                issue_data=issue,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: claim(), range(2)))
        self.assertEqual(sum(1 for result in results if result['claimed']), 1, results)

    def test_terminal_findings_block_same_identity_but_changed_candidate_gets_new_identity(self):
        self.prepare_review_candidate()
        issue = self.issue([], 'In Review')
        claimed = elig.claim_review_attempt(
            'PRO-999', explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            issue_data=issue,
        )
        finished = elig.finish_review_attempt(
            'PRO-999', 'inconclusive', 'review requested changes',
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )
        self.assertEqual(finished['status'], 'quarantined')
        self.assertEqual(self.check('review', issue)['status'], 'review_attempt_seen')

        (self.repo / 'new-candidate.txt').write_text('repaired candidate\n')
        run(['jj', 'commit', '-m', 'repaired candidate'], cwd=self.repo)
        changed = commit(self.repo)
        elig.record_candidate(
            'PRO-999', changed, self.base,
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )
        self.publish_candidate(changed)
        self.assertEqual(self.check('review', issue)['status'], 'eligible')
        next_claim = elig.claim_review_attempt(
            'PRO-999', explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            issue_data=issue,
        )
        self.assertTrue(next_claim['claimed'], next_claim)
        self.assertNotEqual(claimed['identity'], next_claim['identity'])

    def test_operator_retry_requires_reason_and_reuses_identity_explicitly(self):
        self.prepare_review_candidate()
        issue = self.issue([], 'In Review')
        first = elig.claim_review_attempt(
            'PRO-999', explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            issue_data=issue,
        )
        elig.finish_review_attempt(
            'PRO-999', 'crashed', 'Codex exited',
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )
        with self.assertRaisesRegex(RuntimeError, 'requires a reason'):
            elig.authorize_review_retry(
                'PRO-999', first['identity'], '',
                explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            )
        authorized = elig.authorize_review_retry(
            'PRO-999', first['identity'], 'operator reconciled the failed run',
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )
        self.assertEqual(authorized['status'], 'retry_authorized')
        second = elig.claim_review_attempt(
            'PRO-999', explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            issue_data=issue,
        )
        self.assertTrue(second['claimed'], second)
        self.assertEqual(second['identity'], first['identity'])

    def test_review_identity_ignores_comments_and_updated_at_but_binds_task_description_and_policy(self):
        self.prepare_review_candidate()
        issue = self.issue([], 'In Review')
        cfg = elig.resolve_repo('PRO-999', str(self.config))
        state = elig.load_state('PRO-999', cfg, str(self.state_dir))
        identity, inputs = elig.review_identity('PRO-999', issue, state, cfg)
        metadata_only = dict(issue, updatedAt='later', comments=['reviewer comment'])
        same, _ = elig.review_identity('PRO-999', metadata_only, state, cfg)
        changed_description, _ = elig.review_identity(
            'PRO-999', dict(issue, description='changed acceptance criteria'), state, cfg
        )
        changed_title, _ = elig.review_identity(
            'PRO-999', dict(issue, title='changed review task'), state, cfg
        )
        changed_policy, _ = elig.review_identity(
            'PRO-999', issue, state, cfg, policy_version='review-v2'
        )
        self.assertEqual(identity, same)
        self.assertEqual(inputs['candidate_commit'], self.base)
        self.assertNotEqual(identity, changed_description)
        self.assertNotEqual(identity, changed_title)
        self.assertNotEqual(identity, changed_policy)

    def test_quarantine_clears_only_after_published_candidate_and_review_preflight(self):
        self.record_workspace()
        (self.workspace / '.symphony-quarantined').write_text('held\n', encoding='utf-8')
        elig.record_candidate(
            'PRO-999', self.base, self.base,
            explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
        )
        self.publish_candidate(self.base)
        with patch.object(elig, 'linear_issue', return_value=self.issue([], 'In Review')):
            result = elig.clear_quarantine(
                'PRO-999', self.base,
                explicit_config=str(self.config), explicit_state_dir=str(self.state_dir),
            )
        self.assertTrue(result['quarantine_cleared'])
        self.assertFalse((self.workspace / '.symphony-quarantined').exists())

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
