#!/usr/bin/env python3
"""On-demand independent staging audit.

This is intentionally narrow: it re-reads the actual JJ repository and verifies
the proposed integration commit, not the staging writer's report.  It never
moves refs or changes the repository.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

RE_SHA = re.compile(r'^[0-9a-f]{40}$')


def run(argv):
    p = subprocess.run([str(a) for a in argv], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode:
        raise RuntimeError(f"{argv[0]} failed ({p.returncode}): {p.stdout[-1200:]}")
    return p.stdout.strip()


def jj(repo, *args):
    return run(['jj', '--ignore-working-copy', '-R', str(repo), *args])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--old', required=True)
    parser.add_argument('--new', required=True)
    parser.add_argument('--expected-tree', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    checks = []
    errors = []

    def check(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            errors.append(name)

    for name, value in [('candidate', args.candidate), ('old', args.old), ('new', args.new)]:
        if not RE_SHA.fullmatch(value):
            check(f'{name}_shape', False, value)

    try:
        candidate_ancestor = jj(args.repo, 'log', '--no-graph', '-r',
                                f'({args.candidate}) & ::({args.new})', '-T', 'commit_id')
        check('candidate_in_new', candidate_ancestor == args.candidate, candidate_ancestor)
    except Exception as exc:
        check('candidate_in_new', False, str(exc))

    try:
        old_ancestor = jj(args.repo, 'log', '--no-graph', '-r',
                          f'({args.old}) & ::({args.new})', '-T', 'commit_id')
        check('old_in_new', old_ancestor == args.old, old_ancestor)
    except Exception as exc:
        check('old_in_new', False, str(exc))

    try:
        tree = jj(args.repo, 'log', '--no-graph', '-r', args.new, '-T', 'tree')
        check('tree_matches_expected', tree == args.expected_tree, tree)
    except Exception as exc:
        check('tree_matches_expected', False, str(exc))

    try:
        conflicts = jj(args.repo, 'log', '--no-graph', '-r', f'conflicts() & ({args.new})', '-T', 'commit_id')
        check('no_conflicts', conflicts == '', conflicts)
    except Exception as exc:
        check('no_conflicts', False, str(exc))

    try:
        op_id = jj(args.repo, 'op', 'log', '--limit', '1', '-T', 'id')
    except Exception:
        op_id = None

    result = {
        'schema': 'symphony-staging-audit.v1',
        'verdict': 'pass' if not errors else 'fail',
        'repo': str(Path(args.repo).resolve()),
        'candidate_sha': args.candidate,
        'old_staging_sha': args.old,
        'new_staging_sha': args.new,
        'expected_tree': args.expected_tree,
        'jj_operation_id': op_id,
        'checks': checks,
        'errors': errors,
        'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'limitation': 'This audit is an independent process/history check, not a human reviewer or remote-chain verification.',
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['verdict'] == 'pass' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('staging audit failed: ' + str(exc), file=sys.stderr)
        sys.exit(2)
