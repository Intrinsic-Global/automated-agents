#!/usr/bin/env python3
"""Validate one Symphony lane before starting its sandboxed worker."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

try:
    import yaml
except ImportError:
    yaml = None


ROLE_WORKFLOWS = {
    'coding': 'WORKFLOW.md',
    'coding-pro-rata': 'WORKFLOW.coding.pro-rata.md',
    'coding-protected-ops': 'WORKFLOW.coding.protected-ops.md',
    'coding-researcher-challenges': 'WORKFLOW.coding.researcher-challenges.md',
    'coding-pro-106': 'WORKFLOW.pro-106.md',
    'reviewer': 'WORKFLOW.reviewer.md',
    'reviewer-pro-rata': 'WORKFLOW.reviewer.pro-rata.md',
    'reviewer-protected-ops': 'WORKFLOW.reviewer.protected-ops.md',
    'reviewer-researcher-challenges': 'WORKFLOW.reviewer.researcher-challenges.md',
    'forensic-coding': 'WORKFLOW.forensic.md',
    'forensic-reviewer': 'WORKFLOW.forensic-reviewer.md',
}

DEDICATED_PROJECT_SLUGS = {
    'pro-rata-production-readiness-af3912df6995',
    'protected-operations-base-sepolia-b90b18edae8f',
    'researcher-challenges-v0-dfe8a65db477',
    'deployment-bound-forensics-v0-ac0351862195',
}
DEDICATED_ROLE_PROJECTS = {
    'coding-pro-rata': 'pro-rata-production-readiness-af3912df6995',
    'reviewer-pro-rata': 'pro-rata-production-readiness-af3912df6995',
    'coding-protected-ops': 'protected-operations-base-sepolia-b90b18edae8f',
    'reviewer-protected-ops': 'protected-operations-base-sepolia-b90b18edae8f',
    'coding-researcher-challenges': 'researcher-challenges-v0-dfe8a65db477',
    'reviewer-researcher-challenges': 'researcher-challenges-v0-dfe8a65db477',
    'forensic-coding': 'deployment-bound-forensics-v0-ac0351862195',
    'forensic-reviewer': 'deployment-bound-forensics-v0-ac0351862195',
}


def fail(message):
    raise ValueError(message)


def load_workflow(path):
    if yaml is None:
        fail('PyYAML is required to validate workflow front matter')
    if not path.is_file():
        fail(f'workflow file is missing: {path}')
    content = path.read_text(encoding='utf-8')
    parts = content.split('---', 2)
    if len(parts) != 3 or parts[0].strip():
        fail(f'workflow front matter is missing or malformed: {path}')
    try:
        config = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        fail(f'workflow YAML does not parse: {exc}')
    if not isinstance(config, dict):
        fail('workflow front matter must be a YAML mapping')
    return config


def validate_config(role, config, expected_workspace_root=None):
    tracker = config.get('tracker')
    if not isinstance(tracker, dict) or tracker.get('kind') != 'linear':
        fail('tracker.kind must be linear for this lane')
    provider = tracker.get('provider')
    if not isinstance(provider, dict):
        fail('tracker.provider must be a mapping')
    slugs = provider.get('project_slugs')
    if slugs is None:
        slugs = []
    if not isinstance(slugs, list):
        fail('tracker.provider.project_slugs must be a list of strings')
    slugs = [provider.get('project_slug'), *slugs]
    if not all(slug is None or isinstance(slug, str) for slug in slugs):
        fail('tracker.provider project slugs must be strings')
    slugs = {slug.strip() for slug in slugs if isinstance(slug, str) and slug.strip()}
    excluded = provider.get('excluded_project_slugs') or []
    if not isinstance(excluded, list) or not all(isinstance(slug, str) for slug in excluded):
        fail('tracker.provider.excluded_project_slugs must be a list of strings')
    excluded = {slug.strip() for slug in excluded if slug.strip()}
    identifiers = provider.get('issue_identifiers') or []
    excluded_identifiers = provider.get('excluded_issue_identifiers') or []
    if not isinstance(identifiers, list) or not all(isinstance(value, str) for value in identifiers):
        fail('tracker.provider.issue_identifiers must be a list of strings')
    if not isinstance(excluded_identifiers, list) or not all(isinstance(value, str) for value in excluded_identifiers):
        fail('tracker.provider.excluded_issue_identifiers must be a list of strings')
    if bool(slugs) == bool(excluded) and not identifiers:
        fail('tracker.provider must define either project_slug/project_slugs or excluded_project_slugs')

    if role in ('coding', 'reviewer'):
        if slugs or excluded != DEDICATED_PROJECT_SLUGS:
            fail(f'{role} must cover all projects except the dedicated workflow projects')
    elif role in DEDICATED_ROLE_PROJECTS:
        expected_project = DEDICATED_ROLE_PROJECTS[role]
        if slugs != {expected_project} or excluded:
            fail(f'{role} must be scoped only to its dedicated project: {expected_project}')
    elif role == 'coding-pro-106':
        if identifiers != ['PRO-106'] or slugs or excluded:
            fail('coding-pro-106 must be scoped only to issue PRO-106')

    labels = tracker.get('required_labels') or []
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        fail('tracker.required_labels must be a list of strings')
    normalized_labels = {label.strip().lower() for label in labels}
    states = tracker.get('active_states') or []
    if not isinstance(states, list) or not all(isinstance(state, str) for state in states):
        fail('tracker.active_states must be a list of strings')
    normalized_states = {state.strip().lower() for state in states}

    if role.startswith('reviewer') or role == 'forensic-reviewer':
        expected_labels = {'symphony-pilot', 'symphony-pilot-review'}
        expected_states = {'in review'}
    else:
        expected_labels = {'symphony-pilot'}
        expected_states = {'todo', 'in progress'}
    if not expected_labels.issubset(normalized_labels):
        fail(f'{role} workflow is missing required routing labels: {", ".join(sorted(expected_labels - normalized_labels))}')
    if not expected_states.issubset(normalized_states):
        fail(f'{role} workflow is missing active states: {", ".join(sorted(expected_states - normalized_states))}')

    if expected_workspace_root is not None:
        workspace = config.get('workspace')
        root = workspace.get('root') if isinstance(workspace, dict) else None
        if not isinstance(root, str) or not root.strip():
            fail('workspace.root must be configured for a Symphony lane')
        expanded_root = Path(os.path.expanduser(os.path.expandvars(root))).resolve()
        if expanded_root != Path(expected_workspace_root).resolve():
            fail(f'{role} workspace.root does not match its isolated lane root: {expanded_root}')

    api_key = provider.get('api_key')
    if isinstance(api_key, str) and api_key.startswith('$'):
        name = api_key[2:-1] if api_key.startswith('${') and api_key.endswith('}') else api_key[1:]
        api_key = os.environ.get(name)
    if not isinstance(api_key, str) or not api_key.strip():
        fail('Linear API key is missing from workflow configuration and environment')

    codex = config.get('codex') or {}
    command = codex.get('command', 'codex app-server') if isinstance(codex, dict) else None
    if not isinstance(command, str) or not command.strip():
        fail('codex.command is missing')
    command = os.path.expandvars(command)
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        fail(f'codex.command is malformed: {exc}')
    codex_executable = argv[0] if argv else '<empty>'
    codex_available = bool(argv) and '$' not in codex_executable and (
        shutil.which(codex_executable)
        or (Path.home() / 'bin' / codex_executable).is_file()
        or (Path.home() / '.local' / 'bin' / codex_executable).is_file()
    )
    if not codex_available:
        fail(f'Codex executable is unavailable: {codex_executable}')

    # PRF lane hooks and validation steps use these host tools even when a
    # wrapper script hides the literal command from the YAML.
    required = {'python3', 'jj', 'git', 'bb'}
    missing = sorted(
        binary for binary in required
        if not (
            shutil.which(binary)
            or (Path.home() / 'bin' / binary).is_file()
            or (Path.home() / '.local' / 'bin' / binary).is_file()
        )
    )
    if missing:
        fail('required workflow tools are unavailable: ' + ', '.join(missing))


def validate_paths(code_root, workspace_root, worktree):
    code_root = code_root.resolve()
    workspace_root = workspace_root.resolve()
    worktree = worktree.resolve()
    if not code_root.is_dir():
        fail(f'code root is not a directory: {code_root}')
    if not worktree.is_dir():
        fail(f'Symphony worktree is not a directory: {worktree}')
    try:
        workspace_root.relative_to(code_root)
    except ValueError:
        fail(f'workspace root must be inside the writable sandbox mount {code_root}: {workspace_root}')
    workspace_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(prefix='.symphony-preflight-', dir=workspace_root):
            pass
    except OSError as exc:
        fail(f'workspace root is not writable through the configured sandbox mount: {exc}')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', required=True, choices=sorted(ROLE_WORKFLOWS))
    parser.add_argument('--workflow', required=True, type=Path)
    parser.add_argument('--code-root', required=True, type=Path)
    parser.add_argument('--workspace-root', required=True, type=Path)
    parser.add_argument('--worktree', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        expected = ROLE_WORKFLOWS[args.role]
        if args.workflow.name != expected:
            fail(f'{args.role} must load {expected}, got {args.workflow.name}')
        validate_config(args.role, load_workflow(args.workflow), args.workspace_root)
        validate_paths(args.code_root, args.workspace_root, args.worktree)
        print(f'preflight passed: role={args.role} workflow={args.workflow}')
        return 0
    except (OSError, ValueError) as exc:
        print(f'preflight failed: role={args.role}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
