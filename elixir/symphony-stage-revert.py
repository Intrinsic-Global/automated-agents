#!/usr/bin/env python3
"""Audited revert commit for a prior Symphony staging admission.

Creates a new commit on the configured staging bookmark that restores the
pre-integration tree. It never rewrites published history and never touches
Linear, deployments or external services.

Usage: symphony-stage-revert.py INTEGRATION_RESULT.json --reason "why"
Requires SYMPHONY_REPO_CONFIG when the admission used a repo-scoped config.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
from local_env import load_local_env

load_local_env()

CODE_ROOT = Path(os.environ.get('SYMPHONY_WORKSPACES_ROOT', str(Path.home() / '.local/share/symphony/workspaces'))).expanduser()
STAGE_ROOT = CODE_ROOT / 'staging-integrations'
RE_SHA = re.compile(r'^[0-9a-f]{40}$')


def run(argv, cwd=None):
    p = subprocess.run([str(a) for a in argv], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode:
        raise RuntimeError(f"{argv[0]} failed ({p.returncode}): {p.stdout[-900:]}")
    return p.stdout.strip()


def jj(repo, *args):
    return run(['jj', '--ignore-working-copy', '-R', str(repo), *args])


def commit_tree(repo, rev):
    return run(['git', '-C', str(repo), 'rev-parse', f'{rev}^{{tree}}'])


def jj_commit(repo, rev):
    out = jj(repo, 'log', '--no-graph', '-r', rev, '-T', 'commit_id')
    if not RE_SHA.fullmatch(out):
        raise RuntimeError('Ambiguous/missing revision: ' + rev)
    return out


def load_config():
    path = Path(os.environ.get('SYMPHONY_REPO_CONFIG') or (Path(__file__).resolve().parent / 'symphony-repos.json')).expanduser()
    document = json.loads(os.path.expandvars(path.read_text(encoding='utf-8')))
    if document.get('schema') != 'symphony-repo-scopes.v1':
        raise RuntimeError('Unsupported repo config schema')
    return document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('result')
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    result_path = Path(args.result).resolve()
    result = json.loads(result_path.read_text(encoding='utf-8'))
    issue = result['issue']
    config = load_config()
    repo_name = config['issue_repos'].get(issue)
    if not repo_name:
        raise RuntimeError('No repository scope for ' + issue)
    cfg = config['repos'][repo_name]
    repo = Path(cfg['jj_repo']).expanduser().resolve()
    staging = result.get('staging_bookmark') or cfg['staging_bookmark']
    old = result['old_staging_sha']
    new = result['new_staging_sha']
    if not RE_SHA.fullmatch(old) or not RE_SHA.fullmatch(new):
        raise RuntimeError('Invalid integration-result SHAs')
    current = jj_commit(repo, staging)
    if current != new:
        raise RuntimeError(f'Staging tip {current} is not the recorded integration tip {new}; refusing stale revert')
    old_tree = commit_tree(repo, old)
    target = STAGE_ROOT / f'{issue}-{new[:12]}-revert-{int(time.time())}'
    workspace = f'symphony-revert-{issue.lower()}-{new[:12]}'
    # workspace add must update the new workspace's working copy, so no ignore flag.
    run(['jj', '-R', str(repo), 'workspace', 'add', '--name', workspace, '--revision', new,
         '--message', f'Revert {issue} integration {new} ({args.reason})', str(target)])
    run(['jj', '-R', str(target), 'restore', '--from', old])
    run(['jj', '-R', str(target), 'commit', '-m', f'Revert {issue} integration {new} ({args.reason})'])
    revert = jj_commit(target, '@-')
    revert_tree = commit_tree(repo, revert)
    if revert_tree != old_tree:
        raise RuntimeError('Revert tree does not match the pre-integration tree')
    if jj(target, 'log', '--no-graph', '-r', f'({new}) & ancestors({revert})', '-T', 'commit_id') != new:
        raise RuntimeError('Revert is not a descendant of the integration tip')
    jj(target, 'bookmark', 'set', '--revision', revert, staging)
    if jj_commit(repo, staging) != revert:
        raise RuntimeError('Staging bookmark did not move to the revert commit')
    record = {
        'schema': 'symphony-stage-revert.v1',
        'issue': issue,
        'repo': repo_name,
        'staging_bookmark': staging,
        'integration_tip': new,
        'pre_integration_tip': old,
        'revert_commit': revert,
        'revert_tree': revert_tree,
        'reason': args.reason,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'workspace': str(target),
    }
    out = result_path.with_name('revert-result.json')
    out.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        sys.exit('Revert blocked: ' + str(exc))
