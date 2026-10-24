#!/usr/bin/env python3
"""Minimal read-only health check for Symphony lanes and their host dependencies."""
import argparse
import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_env import load_local_env

load_local_env()

CODE_ROOT = Path(os.environ.get('SYMPHONY_WORKSPACES_ROOT', str(Path.home() / '.local/share/symphony/workspaces'))).expanduser()
STATE_DIR = Path(os.environ.get('SYMPHONY_STATE_DIR', str(CODE_ROOT / '.symphony-state')))


def which(name):
    return shutil.which(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue', default=None)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    checks = []
    errors = []

    def check(name, ok, detail=''):
        checks.append({'check': name, 'ok': bool(ok), 'detail': str(detail)})
        if not ok:
            errors.append(name)

    for binary in ['jj', 'git', 'bb', 'clojure', 'python3', 'codex', 'systemctl', 'bwrap', 'mise']:
        path = which(binary)
        check('binary:' + binary, path is not None, path or 'missing')

    for path in [
        Path(os.environ.get('SYMPHONY_REPO_CONFIG', str(Path(__file__).resolve().parents[1] / 'symphony-repos.json'))),
        Path(os.environ.get('SYMPHONY_WORKFLOW', str(Path(__file__).resolve().parents[1] / 'WORKFLOW.md'))),
        Path(__file__).resolve().parent / 'symphony_eligibility.py',
    ]:
        check('file:' + str(path), path.exists(), 'present' if path.exists() else 'missing')

    check('env:LINEAR_API_KEY', bool(os.environ.get('LINEAR_API_KEY')), 'set' if os.environ.get('LINEAR_API_KEY') else 'missing')

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        probe = STATE_DIR / '.health-probe'
        probe.write_text('ok\n', encoding='utf-8')
        probe.unlink()
        check('state_dir:writable', True, str(STATE_DIR))
    except Exception as exc:
        check('state_dir:writable', False, str(exc))

    for repo in filter(None, [os.environ.get('SYMPHONY_JJ_REPO')]):
        check('jj_repo:' + repo, (Path(repo) / '.jj').is_dir(), 'present' if (Path(repo) / '.jj').is_dir() else 'missing')

    try:
        output = subprocess.run(['systemctl', '--user', 'list-units', '--type=scope', '--all', '--no-legend'],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        scopes = [line.split()[0] for line in output.stdout.splitlines() if line.strip().startswith('symphony-')]
        check('systemd_scopes', len(scopes) > 0, ','.join(scopes) or 'none')
        for scope in scopes:
            if not scope.endswith('.scope'):
                continue
            active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', scope]).returncode == 0
            check('scope_active:' + scope, active, 'active' if active else 'inactive')
    except Exception as exc:
        check('systemd_scopes', False, str(exc))

    try:
        ps = subprocess.run(['ps', '-eo', 'args'], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        counts = {}
        for line in ps.stdout.splitlines():
            match = re.search(r'WORKFLOW[^ ]*\.md', line)
            if line.strip().startswith('./bin/symphony') and match:
                counts[match.group(0)] = counts.get(match.group(0), 0) + 1
        duplicates = {name: count for name, count in counts.items() if count > 1}
        check('duplicate_workers', not duplicates, json.dumps(duplicates) if duplicates else 'none')
    except Exception as exc:
        check('duplicate_workers', False, str(exc))

    if args.issue:
        state = STATE_DIR / 'issues' / f'{args.issue}.json'
        check('issue_state:' + args.issue, state.is_file(), str(state))
        if state.is_file():
            try:
                document = json.loads(state.read_text(encoding='utf-8'))
                check('issue_state_schema:' + args.issue, document.get('schema') == 'symphony-issue-eligibility.v1', document.get('schema'))
            except Exception as exc:
                check('issue_state_schema:' + args.issue, False, str(exc))

    result = {
        'schema': 'symphony-health.v1',
        'verdict': 'ok' if not errors else 'failed',
        'checked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'errors': errors,
        'checks': checks,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for item in checks:
            print(('OK  ' if item['ok'] else 'FAIL') + ' ' + item['check'] + (' - ' + item['detail'] if item['detail'] else ''))
        print('verdict:', result['verdict'])
    return 0 if not errors else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('health check failed: ' + str(exc), file=sys.stderr)
        sys.exit(2)
