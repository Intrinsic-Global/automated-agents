#!/usr/bin/env python3
"""Deterministic Symphony eligibility, dependency and workspace freshness checks.

This is the single preflight used by:
  * the coding lane before Todo dispatch and before In Progress/Rework resume,
  * the reviewer lane before accepting a review handoff,
  * the staging gate before integration.

State is file-backed and lock-protected.  It is intentionally colocated with the
existing Symphony workspace tree so it survives process restarts without a
database.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from local_env import load_local_env

load_local_env()

CODE_ROOT = Path(os.environ.get('SYMPHONY_WORKSPACES_ROOT', str(Path.home() / '.local/share/symphony/workspaces'))).expanduser()
DEFAULT_STATE_DIR = Path(os.environ.get('SYMPHONY_STATE_DIR', str(CODE_ROOT / '.symphony-state')))
STAGE_ROOT = CODE_ROOT / 'staging-integrations'
RE_SHA = re.compile(r'^[0-9a-f]{40}$')
RE_ISSUE = re.compile(r'^[A-Z][A-Z0-9]*-[0-9]+$')
SCHEMA = 'symphony-issue-eligibility.v1'
TERMINAL_DEPENDENCY_STATE = 'done'
DEFAULT_TERMINAL_STATES = {'done', 'canceled', 'cancelled', 'duplicate'}
PHASES = ('dispatch', 'resume', 'review', 'stage')


def now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fail(message):
    raise RuntimeError(message)


def run(argv, cwd=None, check=True):
    argv = [str(a) for a in argv]
    p = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and p.returncode:
        raise RuntimeError(f"{argv[0]} failed ({p.returncode}): {p.stdout[-1200:]}")
    return p.stdout.strip()


def jj(repo, *args):
    # Eligibility is read-only; never snapshot another owner's working copy.
    return run(['jj', '--ignore-working-copy', '-R', str(repo), *args])


def jj_commit(repo, rev):
    out = jj(repo, 'log', '--no-graph', '-r', rev, '-T', 'commit_id')
    if not RE_SHA.fullmatch(out):
        fail(f'ambiguous or missing JJ revision {rev!r}: {out[:200]}')
    return out


def jj_is_ancestor(repo, ancestor, descendant):
    out = jj(repo, 'log', '--no-graph', '-r', f'({ancestor}) & ::({descendant})', '-T', 'commit_id')
    return out == ancestor


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def config_path(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get('SYMPHONY_REPO_CONFIG')
    if env:
        return Path(env).expanduser()
    default = Path(__file__).resolve().parents[1] / 'symphony-repos.json'
    return default if default.is_file() else None


def load_repo_config(explicit=None):
    path = config_path(explicit)
    if path is None or not path.is_file():
        return None
    document = json.loads(os.path.expandvars(path.read_text(encoding='utf-8')))
    if document.get('schema') != 'symphony-repo-scopes.v1':
        fail('unsupported repository config schema: ' + str(document.get('schema')))
    if not isinstance(document.get('repos'), dict) or not isinstance(document.get('issue_repos'), dict):
        fail('repository config must define repos and issue_repos maps')
    return document


def legacy_repo():
    return {
        'name': 'prf',
        'jj_repo': os.environ.get('SYMPHONY_JJ_REPO', str(Path.home() / '.local/share/symphony/staging')),
        'producer_root': os.environ.get('SYMPHONY_PRODUCER_ROOT', str(CODE_ROOT / 'producers')),
        'reviewer_root': os.environ.get('SYMPHONY_REVIEWER_ROOT', str(CODE_ROOT / 'reviewers')),
        'staging_bookmark': 'agent-c-staging',
        'bookmark_prefix': 'symphony/',
        'scopes': {},
    }


def resolve_repo(issue, explicit_config=None):
    document = load_repo_config(explicit_config)
    if document is None:
        return legacy_repo()
    repo_name = document['issue_repos'].get(issue)
    if not repo_name:
        # Legacy PRF issues are not all listed in the newer scoped config.
        return legacy_repo()
    if repo_name not in document['repos']:
        fail('unknown repository for ' + issue + ': ' + str(repo_name))
    cfg = dict(document['repos'][repo_name])
    cfg['name'] = repo_name
    cfg['scopes'] = cfg.get('issues', {})
    return cfg


def state_dir(explicit=None):
    return Path(explicit or os.environ.get('SYMPHONY_STATE_DIR') or DEFAULT_STATE_DIR).expanduser()


def issue_dir(explicit=None):
    path = state_dir(explicit) / 'issues'
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_dir(explicit=None):
    path = state_dir(explicit) / 'locks'
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(issue, explicit=None):
    if not RE_ISSUE.fullmatch(issue):
        fail('invalid issue identifier: ' + str(issue))
    return issue_dir(explicit) / f'{issue}.json'


def lock_path(issue, explicit=None):
    if not RE_ISSUE.fullmatch(issue):
        fail('invalid issue identifier: ' + str(issue))
    return lock_dir(explicit) / f'{issue}.lock'


def empty_state(issue, cfg):
    return {
        'schema': SCHEMA,
        'issue': issue,
        'repo': cfg.get('name'),
        'staging_bookmark': cfg.get('staging_bookmark'),
        'workspace': None,
        'workspace_created_at': None,
        'staging_base': None,
        'workspace_revision': None,
        'candidate': {'sha': None, 'base': None, 'bookmark': None, 'recorded_at': None},
        'dependencies': [],
        'review': {
            'candidate_sha': None,
            'decision_path': None,
            'decision_sha256': None,
            'reviewer': None,
            'approved_at': None,
        },
        'staging': None,
        'last_check': None,
        'events': [],
    }


def load_state(issue, cfg=None, explicit=None):
    path = state_path(issue, explicit)
    if not path.is_file():
        return empty_state(issue, cfg or legacy_repo())
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('schema') != SCHEMA or state.get('issue') != issue:
        fail(f'invalid eligibility state for {issue}: {path}')
    return state


def save_state(issue, state, explicit=None):
    path = state_path(issue, explicit)
    atomic_write_json(path, state)


def atomic_write_json(path, document):
    """Write private JSON state atomically without following a predictable temp path."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(document, indent=2, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def update_state(issue, mutator, cfg=None, explicit=None):
    """Mutate state under an exclusive per-issue lock."""
    path = state_path(issue, explicit)
    with open(lock_path(issue, explicit), 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(issue, cfg, explicit)
        result = mutator(state)
        if isinstance(result, dict):
            state = result
        save_state(issue, state, explicit)
        return state


def append_event(state, phase, status, reason, staging_tip):
    event = {
        'at': now(),
        'phase': phase,
        'status': status,
        'reason': reason,
        'staging_tip': staging_tip,
    }
    state['events'] = (state.get('events') or [])[-49:] + [event]
    state['last_check'] = event
    return state


def graphql(query, variables):
    token = os.environ.get('LINEAR_API_KEY')
    if not token:
        fail('LINEAR_API_KEY is not set')
    import urllib.request

    request = urllib.request.Request(
        'https://api.linear.app/graphql',
        data=json.dumps({'query': query, 'variables': variables}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': token},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if payload.get('errors'):
        fail('Linear GraphQL errors: ' + str(payload['errors'])[:600])
    return payload['data']


def linear_issue(issue):
    data = graphql(
        '''query($id:String!){issue(id:$id){
             id identifier state{name} labels{nodes{id name}}
             inverseRelations{nodes{type issue{id identifier state{name}}}}
             team{states{nodes{id name}}}
           }}''',
        {'id': issue},
    )
    value = data.get('issue')
    if not value or value.get('identifier') != issue:
        fail(f'Linear issue lookup does not match {issue}')
    return value


def normalize_state(value):
    return str(value or '').strip().lower()


def dependencies_from_linear(issue_data):
    nodes = ((issue_data.get('inverseRelations') or {}).get('nodes') or [])
    deps = []
    for node in nodes:
        if str(node.get('type') or '').strip().lower() != 'blocks':
            continue
        related = node.get('issue') or {}
        deps.append({
            'issue': related.get('identifier'),
            'state': ((related.get('state') or {}).get('name')),
            'relation': 'blocks',
        })
    return [d for d in deps if d['issue']]


def legacy_receipt(cfg, issue):
    candidate_paths = [
        Path(cfg.get('reviewer_root', '')).expanduser() / issue / 'integration-result.json',
        Path(os.environ.get('SYMPHONY_REVIEWER_ROOT', str(CODE_ROOT / 'reviewers'))) / issue / 'integration-result.json',
    ]
    for path in candidate_paths:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            if value.get('issue') == issue and value.get('new_staging_sha'):
                return value, path
    return None, None


def dependency_receipt(issue, cfg, explicit_state_dir=None):
    state = load_state(issue, cfg, explicit_state_dir)
    staging = state.get('staging') or {}
    if staging.get('new_staging_sha'):
        return staging, state_path(issue, explicit_state_dir)
    return legacy_receipt(cfg, issue)


def check_dependencies(issue, issue_data, cfg, staging_tip, explicit_state_dir=None):
    deps = dependencies_from_linear(issue_data)
    checked = []
    errors = []
    for dep in deps:
        blocker = dep['issue']
        state_name = normalize_state(dep.get('state'))
        item = {
            'issue': blocker,
            'state': dep.get('state'),
            'accepted': state_name == TERMINAL_DEPENDENCY_STATE,
            'staging_inclusion': None,
            'receipt': None,
            'verified_at': now(),
        }
        if not item['accepted']:
            errors.append(f'dependency_not_accepted:{blocker}:{dep.get("state") or "unknown"}')
            checked.append(item)
            continue
        receipt, receipt_path = dependency_receipt(blocker, cfg, explicit_state_dir)
        if not receipt:
            errors.append(f'dependency_receipt_missing:{blocker}')
            checked.append(item)
            continue
        inclusion = receipt.get('new_staging_sha')
        candidate = receipt.get('candidate_sha')
        if not inclusion or not RE_SHA.fullmatch(str(inclusion)):
            errors.append(f'dependency_receipt_invalid:{blocker}')
            checked.append(item)
            continue
        item['staging_inclusion'] = inclusion
        item['receipt'] = str(receipt_path)
        repo = cfg['jj_repo']
        if not (inclusion == staging_tip or jj_is_ancestor(repo, inclusion, staging_tip)):
            errors.append(f'dependency_not_in_staging:{blocker}:{inclusion}')
            checked.append(item)
            continue
        if candidate and RE_SHA.fullmatch(str(candidate)):
            if not (candidate == inclusion or jj_is_ancestor(repo, candidate, inclusion)):
                errors.append(f'dependency_candidate_not_in_integration:{blocker}:{candidate}')
                checked.append(item)
                continue
        checked.append(item)
    return checked, errors


def repo_pointer(workspace):
    jjdir = Path(workspace) / '.jj'
    repo_file = jjdir / 'repo'
    if repo_file.is_file():
        pointer = repo_file.read_text(encoding='utf-8').strip()
        if not pointer:
            return None
        return (jjdir / pointer).resolve()
    if repo_file.is_dir():
        return repo_file.resolve()
    return None


def classify_workspace(issue, state, cfg, staging_tip, explicit_state_dir=None):
    workspace = state.get('workspace')
    if not workspace:
        return 'absent', 'no workspace recorded', None, None
    path = Path(workspace)
    if not path.is_dir():
        return 'absent', f'workspace path missing: {workspace}', None, None
    if not (path / '.jj').exists():
        return 'unsafe', 'workspace has no .jj directory', None, None
    try:
        source_pointer = repo_pointer(cfg['jj_repo'])
        workspace_pointer = repo_pointer(path)
        if source_pointer and workspace_pointer and source_pointer != workspace_pointer:
            return 'unsafe', 'workspace points at a different JJ repository', None, None
        jj(path, 'root')
        conflicts = jj(path, 'log', '--no-graph', '-r', 'conflicts() & @', '-T', 'commit_id')
        if conflicts:
            return 'unsafe', 'workspace working copy contains conflicts', None, None
        workspace_tip = jj_commit(path, '@')
    except Exception as exc:
        return 'unsafe', f'workspace is not inspectable: {exc}', None, None

    base = state.get('staging_base')
    if not base:
        try:
            base = jj_commit(path, '@-')
        except Exception as exc:
            return 'unsafe', f'workspace base is unreadable: {exc}', workspace_tip, None
    if base == staging_tip:
        return 'fresh', 'workspace base equals current staging tip', workspace_tip, base
    if jj_is_ancestor(cfg['jj_repo'], base, staging_tip):
        return 'stale_ancestor', 'workspace base is an ancestor of current staging tip', workspace_tip, base
    return 'diverged', 'workspace base and current staging tip have diverged', workspace_tip, base


def review_state(issue, state, cfg, explicit_state_dir=None):
    candidate = ((state.get('candidate') or {}).get('sha'))
    review = state.get('review') or {}
    if not candidate or not RE_SHA.fullmatch(str(candidate)):
        return {'ok': False, 'reason': 'candidate_missing', 'candidate': candidate}
    if review.get('candidate_sha') != candidate:
        return {'ok': False, 'reason': 'review_not_bound_to_candidate', 'candidate': candidate}
    decision_path = review.get('decision_path')
    if not decision_path or not Path(decision_path).is_file():
        return {'ok': False, 'reason': 'review_decision_missing', 'candidate': candidate}
    if sha256_file(Path(decision_path)) != review.get('decision_sha256'):
        return {'ok': False, 'reason': 'review_decision_changed', 'candidate': candidate}
    return {'ok': True, 'reason': 'review_bound', 'candidate': candidate}


def check_phase(issue, phase, explicit_config=None, explicit_state_dir=None, staging_tip=None, linear_data=None):
    if phase not in PHASES:
        fail('phase must be one of: ' + ', '.join(PHASES))
    if not RE_ISSUE.fullmatch(issue):
        fail('invalid issue identifier: ' + str(issue))
    cfg = resolve_repo(issue, explicit_config)
    repo = Path(cfg['jj_repo']).expanduser()
    if not (repo / '.jj').exists():
        fail(f'JJ repository is missing: {repo}')
    if staging_tip is None:
        staging_tip = jj_commit(repo, cfg['staging_bookmark'])
    issue_data = linear_data or linear_issue(issue)
    state_file_exists = state_path(issue, explicit_state_dir).is_file()
    state = load_state(issue, cfg, explicit_state_dir)
    if phase in ('review', 'stage') and not state_file_exists:
        if os.environ.get('SYMPHONY_ELIGIBILITY_LEGACY', '1').lower() not in ('0', 'false', 'off', 'no'):
            return {
                'schema': 'symphony-eligibility-result.v1',
                'issue': issue,
                'phase': phase,
                'status': 'eligible',
                'reasons': ['legacy_no_state'],
                'legacy': True,
                'staging_bookmark': cfg.get('staging_bookmark'),
                'staging_tip': staging_tip,
                'repo': cfg.get('name'),
                'dependencies': [],
                'workspace': {'path': None, 'state': 'absent', 'tip': None, 'base': None},
                'candidate': None,
                'review': None,
                'checked_at': now(),
            }
    if state.get('repo') != cfg.get('name'):
        state['repo'] = cfg.get('name')
    state['staging_bookmark'] = cfg.get('staging_bookmark')

    deps, dep_errors = check_dependencies(issue, issue_data, cfg, staging_tip, explicit_state_dir)
    state['dependencies'] = deps

    reasons = []
    status = 'eligible'
    if dep_errors:
        status = 'blocked'
        reasons.extend(dep_errors)

    workspace_state = None
    workspace_tip = None
    workspace_base = None
    if phase in ('dispatch', 'resume'):
        workspace_state, workspace_reason, workspace_tip, workspace_base = classify_workspace(
            issue, state, cfg, staging_tip, explicit_state_dir
        )
        state['workspace_revision'] = workspace_tip
        if workspace_base:
            state['staging_base'] = state.get('staging_base') or workspace_base
        if workspace_state == 'stale_ancestor':
            status = 'stale'
            reasons.append('workspace_base_is_stale_ancestor')
        elif workspace_state == 'diverged':
            status = 'diverged'
            reasons.append('workspace_base_diverged_from_staging')
        elif workspace_state == 'unsafe':
            status = 'unsafe'
            reasons.append('workspace_unsafe:' + str(workspace_reason))
        elif phase == 'resume' and workspace_state == 'absent':
            status = 'blocked'
            reasons.append('workspace_missing_for_resume')

    candidate = ((state.get('candidate') or {}).get('sha'))
    review_check = None
    if phase in ('review', 'stage'):
        if not candidate or not RE_SHA.fullmatch(str(candidate)):
            status = 'blocked'
            reasons.append('candidate_missing')
        base = state.get('staging_base') or (state.get('candidate') or {}).get('base')
        if not base:
            status = 'blocked'
            reasons.append('staging_base_missing')
        elif base != staging_tip:
            if jj_is_ancestor(repo, base, staging_tip):
                status = 'stale'
                reasons.append('candidate_base_is_stale_ancestor')
            else:
                status = 'diverged'
                reasons.append('candidate_base_diverged_from_staging')
        if phase == 'stage':
            review_check = review_state(issue, state, cfg, explicit_state_dir)
            if not review_check['ok']:
                status = 'blocked'
                reasons.append('review_invalid:' + str(review_check['reason']))

    result = {
        'schema': 'symphony-eligibility-result.v1',
        'issue': issue,
        'phase': phase,
        'status': status,
        'reasons': reasons,
        'staging_bookmark': cfg.get('staging_bookmark'),
        'staging_tip': staging_tip,
        'repo': cfg.get('name'),
        'dependencies': deps,
        'workspace': {
            'path': state.get('workspace'),
            'state': workspace_state,
            'tip': workspace_tip,
            'base': state.get('staging_base'),
        },
        'candidate': candidate,
        'review': review_check,
        'checked_at': now(),
    }

    def mutate(current):
        current['repo'] = cfg.get('name')
        current['staging_bookmark'] = cfg.get('staging_bookmark')
        current['dependencies'] = deps
        current['workspace_revision'] = workspace_tip or current.get('workspace_revision')
        if state.get('staging_base'):
            current['staging_base'] = state.get('staging_base')
        append_event(current, phase, status, ';'.join(reasons) if reasons else 'eligible', staging_tip)
        return current

    update_state(issue, mutate, cfg, explicit_state_dir)
    return result


def record_workspace(issue, workspace, staging_base=None, fresh=False, explicit_config=None, explicit_state_dir=None):
    cfg = resolve_repo(issue, explicit_config)
    workspace = str(Path(workspace).resolve())
    staging_base = staging_base or jj_commit(cfg['jj_repo'], cfg['staging_bookmark'])

    def mutate(state):
        state['repo'] = cfg.get('name')
        state['staging_bookmark'] = cfg.get('staging_bookmark')
        state['workspace'] = workspace
        state['workspace_created_at'] = state.get('workspace_created_at') or now()
        if fresh or not state.get('staging_base'):
            state['staging_base'] = staging_base
            state['review'] = {
                'candidate_sha': None,
                'decision_path': None,
                'decision_sha256': None,
                'reviewer': None,
                'approved_at': None,
            }
        try:
            state['workspace_revision'] = jj_commit(workspace, '@')
        except Exception:
            state['workspace_revision'] = None
        append_event(state, 'workspace', 'recorded', f'base={staging_base}', staging_base)
        return state

    state = update_state(issue, mutate, cfg, explicit_state_dir)
    return {'issue': issue, 'workspace': workspace, 'staging_base': state['staging_base']}


def record_candidate(issue, candidate, base=None, explicit_config=None, explicit_state_dir=None):
    if not RE_SHA.fullmatch(str(candidate)):
        fail('candidate must be a 40-character lowercase Git SHA')
    cfg = resolve_repo(issue, explicit_config)
    if base is None:
        state = load_state(issue, cfg, explicit_state_dir)
        base = state.get('staging_base')
    if not base or not RE_SHA.fullmatch(str(base)):
        fail('candidate base is unknown; record the workspace first')

    def mutate(state):
        prior = (state.get('candidate') or {}).get('sha')
        state['candidate'] = {
            'sha': candidate,
            'base': base,
            'bookmark': cfg.get('bookmark_prefix', 'symphony/') + issue,
            'recorded_at': now(),
        }
        if prior and prior != candidate:
            state['review'] = {
                'candidate_sha': None,
                'decision_path': None,
                'decision_sha256': None,
                'reviewer': None,
                'approved_at': None,
            }
        append_event(state, 'candidate', 'recorded', f'candidate={candidate}', base)
        return state

    state = update_state(issue, mutate, cfg, explicit_state_dir)
    return {'issue': issue, 'candidate': state['candidate']}


def record_review(issue, decision_path, reviewer=None, explicit_config=None, explicit_state_dir=None):
    cfg = resolve_repo(issue, explicit_config)
    decision_path = Path(decision_path).resolve()
    if not decision_path.is_file():
        fail('review decision is missing: ' + str(decision_path))
    document = json.loads(decision_path.read_text(encoding='utf-8'))
    if document.get('schema') != 'symphony-pilot-review-decision.v1' or document.get('issue') != issue:
        fail('invalid review decision schema or issue')
    if document.get('verdict') != 'approved':
        fail('review decision is not an approval')
    candidate = document.get('candidate_sha')
    base = document.get('base_sha')
    if not RE_SHA.fullmatch(str(candidate)) or not RE_SHA.fullmatch(str(base)):
        fail('invalid candidate or base in review decision')
    digest = sha256_file(decision_path)

    def mutate(state):
        current = (state.get('candidate') or {}).get('sha')
        if current != candidate:
            fail(f'review candidate {candidate} does not match recorded candidate {current}')
        if state.get('staging_base') != base:
            fail(f'review base {base} does not match recorded base {state.get("staging_base")}')
        state['review'] = {
            'candidate_sha': candidate,
            'decision_path': str(decision_path),
            'decision_sha256': digest,
            'reviewer': reviewer or document.get('reviewer') or 'independent-reviewer',
            'approved_at': now(),
        }
        append_event(state, 'review', 'recorded', f'candidate={candidate}', state.get('staging_base'))
        return state

    state = update_state(issue, mutate, cfg, explicit_state_dir)
    return {'issue': issue, 'review': state['review']}


def record_staging(issue, receipt, explicit_config=None, explicit_state_dir=None):
    cfg = resolve_repo(issue, explicit_config)
    candidate = receipt.get('candidate_sha')
    if not RE_SHA.fullmatch(str(candidate)):
        fail('staging receipt has no valid candidate_sha')

    def mutate(state):
        current = (state.get('candidate') or {}).get('sha')
        if current and current != candidate:
            fail(f'staging receipt candidate {candidate} does not match recorded candidate {current}')
        state['staging'] = receipt
        append_event(state, 'staging', 'recorded', f'new={receipt.get("new_staging_sha")}', receipt.get('new_staging_sha'))
        return state

    state = update_state(issue, mutate, cfg, explicit_state_dir)
    return {'issue': issue, 'staging': state['staging']}


def record_legacy_receipt(issue, candidate, staging_inclusion, evidence, note=None,
                          explicit_config=None, explicit_state_dir=None):
    if not RE_SHA.fullmatch(str(candidate)) or not RE_SHA.fullmatch(str(staging_inclusion)):
        fail('legacy receipt requires 40-character candidate and staging-inclusion SHAs')
    receipt = {
        'schema': 'symphony-staging-receipt.v1',
        'issue': issue,
        'candidate_sha': candidate,
        'base_sha': None,
        'new_staging_sha': staging_inclusion,
        'integration_mode': 'legacy-reconstructed',
        'validated_tree': None,
        'tests': [],
        'reviewed_tests': [],
        'source_roots': {},
        'audit': {'required': False, 'verdict': 'not-run', 'path': None},
        'legacy_reconstructed': True,
        'evidence': evidence,
        'note': note or 'Reconstructed from retained staging history during eligibility rollout; not an original gate receipt.',
        'recorded_at': now(),
    }
    path = write_receipt(issue, receipt, explicit_state_dir)
    receipt['receipt_path'] = str(path)
    record_staging(issue, receipt, explicit_config, explicit_state_dir)
    return receipt


def write_receipt(issue, receipt, explicit_state_dir=None):
    directory = state_dir(explicit_state_dir) / 'receipts'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f'{issue}-{receipt.get("candidate_sha", "unknown")[:12]}.json'
    path = directory / name
    atomic_write_json(path, receipt)
    return path


def cmd_check(args):
    result = check_phase(
        args.issue,
        args.phase,
        explicit_config=args.config,
        explicit_state_dir=args.state_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'eligible' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog='symphony_eligibility.py')
    parser.add_argument('--state-dir', default=None)
    parser.add_argument('--config', default=None)
    sub = parser.add_subparsers(dest='command', required=True)

    check = sub.add_parser('check')
    check.add_argument('--issue', required=True)
    check.add_argument('--phase', required=True, choices=PHASES)
    check.set_defaults(func=cmd_check)

    workspace = sub.add_parser('record-workspace')
    workspace.add_argument('--issue', required=True)
    workspace.add_argument('--workspace', required=True)
    workspace.add_argument('--staging-base', default=None)
    workspace.add_argument('--fresh', action='store_true')
    workspace.set_defaults(func=lambda a: print(json.dumps(record_workspace(
        a.issue, a.workspace, a.staging_base, a.fresh, a.config, a.state_dir), indent=2)))

    candidate = sub.add_parser('record-candidate')
    candidate.add_argument('--issue', required=True)
    candidate.add_argument('--candidate', required=True)
    candidate.add_argument('--base', default=None)
    candidate.set_defaults(func=lambda a: print(json.dumps(record_candidate(
        a.issue, a.candidate, a.base, a.config, a.state_dir), indent=2)))

    review = sub.add_parser('record-review')
    review.add_argument('--issue', required=True)
    review.add_argument('--decision', required=True)
    review.add_argument('--reviewer', default=None)
    review.set_defaults(func=lambda a: print(json.dumps(record_review(
        a.issue, a.decision, a.reviewer, a.config, a.state_dir), indent=2)))

    staging = sub.add_parser('record-staging')
    staging.add_argument('--issue', required=True)
    staging.add_argument('--receipt', required=True)
    staging.set_defaults(func=lambda a: print(json.dumps(record_staging(
        a.issue, json.loads(Path(a.receipt).read_text(encoding='utf-8')), a.config, a.state_dir), indent=2)))

    legacy = sub.add_parser('record-legacy-receipt')
    legacy.add_argument('--issue', required=True)
    legacy.add_argument('--candidate', required=True)
    legacy.add_argument('--staging-inclusion', required=True)
    legacy.add_argument('--evidence', required=True)
    legacy.add_argument('--note', default=None)
    legacy.set_defaults(func=lambda a: print(json.dumps(record_legacy_receipt(
        a.issue, a.candidate, a.staging_inclusion, a.evidence, a.note, a.config, a.state_dir), indent=2)))

    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except Exception as exc:
        print(json.dumps({'schema': 'symphony-eligibility-result.v1', 'status': 'error', 'error': str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
