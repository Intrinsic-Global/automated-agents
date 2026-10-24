#!/usr/bin/env python3
"""Read the versioned Symphony repository scope for an issue."""
import argparse
import json
import os
from pathlib import Path

from local_env import load_local_env

load_local_env()

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'symphony-repos.json'
FIELDS = (
    'name', 'remote', 'jj_repo', 'producer_root', 'reviewer_root',
    'staging_bookmark', 'bookmark_prefix', 'workspace_prefix', 'validation',
)


def load(path=None):
    path = Path(path or os.environ.get('SYMPHONY_REPO_CONFIG') or DEFAULT_CONFIG).expanduser()
    document = json.loads(os.path.expandvars(path.read_text(encoding='utf-8')))
    if document.get('schema') != 'symphony-repo-scopes.v1':
        raise SystemExit('unsupported repo config schema: ' + str(document.get('schema')))
    return document


def resolve(document, issue):
    repo_name = document.get('issue_repos', {}).get(issue)
    if not repo_name:
        raise SystemExit('no repository scope configured for ' + issue)
    repo = document.get('repos', {}).get(repo_name)
    if not repo:
        raise SystemExit('unknown repository for ' + issue + ': ' + repo_name)
    result = dict(repo)
    result['name'] = repo_name
    result['scopes'] = repo.get('issues', {})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue', required=True)
    parser.add_argument('--field', required=True, choices=FIELDS + ('scopes', 'repo'))
    parser.add_argument('--config', default=None)
    args = parser.parse_args()
    result = resolve(load(args.config), args.issue)
    value = result[args.field]
    if isinstance(value, (dict, list)):
        print(json.dumps(value, separators=(',', ':')))
    else:
        print(value)


if __name__ == '__main__':
    main()
