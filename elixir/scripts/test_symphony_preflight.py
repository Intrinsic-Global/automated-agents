"""Focused tests for the host-side lane startup preflight."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import symphony_preflight as preflight


def workflow(role='coding', labels=None, states=None, projects=None):
    reviewer = role.startswith('reviewer')
    return {
        'tracker': {
            'kind': 'linear',
            'provider': {
                'project_slugs': projects or ['automation'],
                'api_key': '$LINEAR_API_KEY',
            },
            'required_labels': labels or (
                ['symphony-pilot', 'symphony-pilot-review'] if reviewer else ['symphony-pilot']
            ),
            'active_states': states or (['In Review'] if reviewer else ['Todo', 'In Progress']),
        },
        'codex': {'command': '$CODEX_BIN app-server'},
    }


class SymphonyPreflightTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'LINEAR_API_KEY': 'test-token', 'CODEX_BIN': 'codex'})
        self.env.start()
        self.tools = patch.object(preflight.shutil, 'which', return_value='/usr/bin/tool')
        self.tools.start()

    def tearDown(self):
        self.tools.stop()
        self.env.stop()

    def test_accepts_coding_workflow_with_matching_scope(self):
        preflight.validate_config('coding', workflow())

    def test_reviewer_requires_both_routing_labels_and_review_state(self):
        preflight.validate_config('reviewer', workflow('reviewer'))
        with self.assertRaisesRegex(ValueError, 'symphony-pilot-review'):
            preflight.validate_config('reviewer', workflow('reviewer', labels=['symphony-pilot']))

    def test_coding_lane_rejects_review_only_state(self):
        with self.assertRaisesRegex(ValueError, 'active states'):
            preflight.validate_config('coding', workflow(states=['In Review']))

    def test_project_scope_must_be_a_nonempty_list(self):
        config = workflow()
        config['tracker']['provider']['project_slugs'] = 'automation'
        with self.assertRaisesRegex(ValueError, 'must be a list'):
            preflight.validate_config('coding', config)

    def test_workspace_root_must_be_writable_and_inside_code_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code_root = root / 'code'
            worktree = code_root / 'symphony'
            worktree.mkdir(parents=True)
            preflight.validate_paths(code_root, code_root / 'workspaces' / 'lane', worktree)
            with self.assertRaisesRegex(ValueError, 'inside the writable sandbox mount'):
                preflight.validate_paths(code_root, root / 'outside', worktree)


if __name__ == '__main__':
    unittest.main(verbosity=2)
