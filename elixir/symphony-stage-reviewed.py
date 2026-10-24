#!/usr/bin/env python3
"""Constrained per-issue post-review admission gate, then Linear Done.

Runs only as a Symphony *host-side after_run hook*; never from Codex.
With SYMPHONY_REPO_CONFIG set, the issue's versioned repository scope selects the
JJ repo, producer/reviewer roots, staging bookmark, allowed paths and validation.
Without it, the original PRF agent-c-staging behavior is preserved.
The gate never modifies GitHub main, publishes, deploys or changes authority.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
try:
    import symphony_eligibility as eligibility
except Exception:  # pragma: no cover - the gate must fail closed if preflight is unavailable
    eligibility = None

ISSUE = None
BOOKMARK = None
LABEL = 'symphony-pilot-review'
CODE_ROOT = Path(os.environ.get('SYMPHONY_WORKSPACES_ROOT', str(Path.home() / '.local/share/symphony/workspaces'))).expanduser()
PRODUCER = None
STAGE_ROOT = CODE_ROOT / 'staging-integrations'
RE_SHA = re.compile(r'^[0-9a-f]{40}$')
BB = os.environ.get('SYMPHONY_BB','bb')
SCOPES = {
    'PRO-72': [
        'src/resolver_sim/composition/command_lineage.clj',
        'test/resolver_sim/composition/command_lineage_test.clj',
        'CHANGELOG.md',
    ],
    'PRO-48': [
        'docs/deployments/BOUNDED_CONFIGURATION_REMAINING_PLAN.md',
        'etc/deployments/baselines/README.md',
        'etc/deployments/baselines/bounded-configuration-release-sources.current.v3.json',
        'etc/deployments/bounded-configuration-local-candidate-v4/',
        'test/resolver_sim/configuration_review_candidate_v4_test.clj',
    ],
    'PRO-94': [
        'docs/specs/NETWORK_CHECKPOINT_PROTOCOL_V1.md',
        'src/resolver_sim/hash/canonical.clj',
        'src/resolver_sim/network/checkpoint.clj',
        'test/resolver_sim/network/checkpoint_fresh_process.clj',
        'test/resolver_sim/network/checkpoint_test.clj',
    ],
    'PRO-96': ['docs/allocation-proofs/pro-rata-provenance-coverage-v1.md'],
    'PRO-34': ['src/resolver_sim/', 'test/resolver_sim/', 'CHANGELOG.md'],
    'PRO-35': ['src/resolver_sim/', 'test/resolver_sim/', 'CHANGELOG.md'],
    'PRO-36': ['src/resolver_sim/', 'test/resolver_sim/', 'CHANGELOG.md'],
    'PRO-27': ['src/', 'test/', 'scripts/forensic/', 'examples/', 'CHANGELOG.md'],
    'PRO-56': ['src/', 'test/', 'scripts/forensic/', 'examples/', 'CHANGELOG.md'],
    'PRO-65': ['src/resolver_sim/composition/', 'test/resolver_sim/composition/', 'CHANGELOG.md'],
    'PRO-69': [
        'src/resolver_sim/composition/fixtures.clj',
        'src/resolver_sim/composition/execution.clj',
        'src/resolver_sim/composition/compiler.clj',
        'src/resolver_sim/composition/contract.clj',
        'test/resolver_sim/composition/execution_test.clj',
        'test/resolver_sim/composition/compiler_test.clj',
        'test/resolver_sim/composition/contract_test.clj',
        'CHANGELOG.md',
    ],
    'PRO-67': [
        'src/resolver_sim/composition/fixtures.clj',
        'src/resolver_sim/composition/compiler.clj',
        'src/resolver_sim/composition/execution.clj',
        'test/resolver_sim/composition/compiler_test.clj',
        'test/resolver_sim/composition/execution_test.clj',
        'CHANGELOG.md',
    ],
    'PRO-68': [
        'src/resolver_sim/composition/fixtures.clj',
        'src/resolver_sim/composition/compiler.clj',
        'test/resolver_sim/composition/compiler_test.clj',
        'CHANGELOG.md',
    ],
    'PRO-70': [
        'src/resolver_sim/composition/fixtures.clj',
        'src/resolver_sim/composition/contract.clj',
        'src/resolver_sim/composition/compiler.clj',
        'src/resolver_sim/composition/execution.clj',
        'test/resolver_sim/composition/contract_test.clj',
        'test/resolver_sim/composition/compiler_test.clj',
        'test/resolver_sim/composition/execution_test.clj',
        'CHANGELOG.md',
    ],
    'PRO-66': [
        'src/resolver_sim/composition/execution.clj',
        'src/resolver_sim/composition/compiler.clj',
        'src/resolver_sim/composition/contract.clj',
        'test/resolver_sim/composition/execution_test.clj',
        'test/resolver_sim/composition/compiler_test.clj',
        'test/resolver_sim/composition/contract_test.clj',
        'CHANGELOG.md',
    ],
}


REPO_CONFIG_PATH = os.environ.get('SYMPHONY_REPO_CONFIG')


def load_repo_config():
    if not REPO_CONFIG_PATH:
        return None
    path = Path(REPO_CONFIG_PATH).expanduser()
    if not path.is_file():
        raise RuntimeError('SYMPHONY_REPO_CONFIG is missing: ' + str(path))
    document = json.loads(os.path.expandvars(path.read_text(encoding='utf-8')))
    if document.get('schema') != 'symphony-repo-scopes.v1':
        raise RuntimeError('Unsupported repo config schema: ' + str(document.get('schema')))
    repos = document.get('repos')
    issue_repos = document.get('issue_repos')
    if not isinstance(repos, dict) or not isinstance(issue_repos, dict):
        raise RuntimeError('Repo config must define repos and issue_repos maps')
    return document


def resolve_repo(issue):
    document = load_repo_config()
    if document is None:
        return {
            'name': 'prf',
            'remote': 'git@github.com:protocol-robustness/protocol-robustness-framework.git',
            'jj_repo': os.environ.get('SYMPHONY_JJ_REPO', ''),
            'producer_root': str(CODE_ROOT / 'prf-coder-v2'),
            'reviewer_root': str(CODE_ROOT / 'prf-reviewer-v1'),
            'staging_bookmark': 'agent-c-staging',
            'bookmark_prefix': 'symphony/',
            'workspace_prefix': 'symphony-stage',
            'validation': [['bb', 'validate'], ['bb', 'backstop']],
            'format_command': ['bb', 'fmt'],
            'scopes': SCOPES,
        }
    repo_name = document['issue_repos'].get(issue)
    if not repo_name:
        raise RuntimeError('No repository scope configured for ' + issue)
    if repo_name not in document['repos']:
        raise RuntimeError('Unknown repository for ' + issue + ': ' + repo_name)
    cfg = dict(document['repos'][repo_name])
    cfg['name'] = repo_name
    cfg['scopes'] = cfg.get('issues', {})
    return cfg


def path_allowed(path, allowed_paths):
    return any(path == p or (p.endswith('/') and path.startswith(p)) for p in allowed_paths)


def git_stack(producer, base, candidate, allowed_paths):
    shas = run(['git', '-C', str(producer), 'rev-list', '--reverse', f'{base}..{candidate}']).splitlines()
    if not shas:
        raise RuntimeError('Reviewed candidate has no commits above its base')
    entries = []
    for sha in shas:
        if not RE_SHA.fullmatch(sha):
            raise RuntimeError('Invalid commit in candidate stack: ' + sha)
        subject = run(['git', '-C', str(producer), 'show', '-s', '--format=%s', sha])
        files = run(['git', '-C', str(producer), 'diff-tree', '--no-commit-id', '--name-only', '-r', sha]).splitlines()
        disallowed = [path for path in files if not path_allowed(path, allowed_paths)]
        if disallowed:
            raise RuntimeError(f'Candidate stack commit {sha} changed outside the approved paths: {disallowed!r}')
        entries.append({'sha': sha, 'subject': subject})
    if entries[-1]['sha'] != candidate:
        raise RuntimeError('Reviewed candidate is not the tip of the producer stack')
    return entries


def run(argv, cwd=None, log=None, check=True):
    argv = [str(a) for a in argv]
    p = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if log is not None:
        with open(log, 'a', encoding='utf-8') as f:
            f.write('$ ' + ' '.join(argv) + f'\nexit={p.returncode}\n{p.stdout}\n')
    if check and p.returncode:
        raise RuntimeError(f'{argv[0]} failed ({p.returncode}): {p.stdout[-900:]}')
    return p.stdout.strip()


def jj(repo, *args, log=None):
    # A JJ read command can snapshot an active dirty working copy. Avoid doing
    # that to the operator's existing workspace; staging test copies may snapshot.
    argv = ['jj']
    updates_working_copy = tuple(args[:2]) == ('workspace', 'add')
    if not updates_working_copy:
        argv.append('--ignore-working-copy')
    return run([*argv, '-R', str(repo), *args], log=log)


def commit_tree(repo, rev):
    return run(['git', '-C', str(repo), 'rev-parse', f'{rev}^{{tree}}'])


def jj_commit(repo, rev):
    out = jj(repo, 'log', '--no-graph', '-r', rev, '-T', 'commit_id')
    if not RE_SHA.fullmatch(out):
        raise RuntimeError(f'Ambiguous/missing JJ revision: {rev}: {out[:200]}')
    return out


def graphql(query, vars):
    token = os.environ.get('LINEAR_API_KEY')
    if not token:
        raise RuntimeError('Missing LINEAR_API_KEY; cannot update Linear')
    request = urllib.request.Request(
        'https://api.linear.app/graphql',
        data=json.dumps({'query': query, 'variables': vars}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': token},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if data.get('errors'):
        raise RuntimeError('Linear GraphQL errors: '+str(data['errors'])[:400])
    return data['data']


def linear_issue():
    data = graphql('''query($id:String!){issue(id:$id){id identifier state{name} labels{nodes{id name}} team{states{nodes{id name}}}}}''', {'id': ISSUE})
    issue = data.get('issue')
    if not issue or issue['identifier'] != ISSUE:
        raise RuntimeError(f'Linear issue lookup does not match {ISSUE}')
    return issue


def linear_comment(issue_id, body):
    graphql('''mutation($input:CommentCreateInput!){commentCreate(input:$input){success}}''',
            {'input': {'issueId':issue_id, 'body':body}})


def suspend_review(reason):
    """Best effort: avoid repeated review-staging failures; preserve other labels."""
    try:
        issue = linear_issue()
        if issue['state']['name'] != 'In Review':
            return
        labels = issue['labels']['nodes']
        ids = [x['id'] for x in labels if x['name'].lower() != LABEL]
        graphql('''mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success}}''',
                {'id':issue['id'], 'input':{'labelIds':ids}})
        linear_comment(issue['id'], '**Symphony staging blocked (no Done):** '+reason[:1500]+ '\nReview label removed to prevent repeat dispatch. Retained reviewer workspace and decision for diagnosis.')
    except Exception as e:
        print(f'Could not suspend reviewer dispatch: {e}', file=sys.stderr)


def claim_review(issue):
    """Remove the routing label before expensive integration work starts."""
    labels = issue['labels']['nodes']
    if not any(label['name'].lower() == LABEL for label in labels):
        print('Review already claimed or suspended; skipping automatic redispatch')
        return False
    label_ids = [label['id'] for label in labels if label['name'].lower() != LABEL]
    result = graphql(
        '''mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success}}''',
        {'id': issue['id'], 'input': {'labelIds': label_ids}},
    )
    if not result['issueUpdate']['success']:
        raise RuntimeError('Could not claim reviewer integration; review label remains active')
    linear_comment(issue['id'], '**Approved review claimed for host integration.** Automatic reviewer dispatch is suspended while the staging gate runs.')
    return True


def plan(decision_path):
    decision_path = decision_path.resolve()
    if not decision_path.is_file():
        raise RuntimeError('Decision file missing')
    issue = decision_path.parent.name
    if not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', issue):
        raise RuntimeError('Decision file is not in an issue workspace: '+issue)
    cfg = resolve_repo(issue)
    expected_prefix = (Path(cfg['reviewer_root']).expanduser() / issue).resolve()
    if decision_path.parent != expected_prefix:
        raise RuntimeError('Decision file outside designated reviewer workspace')
    document = json.loads(decision_path.read_text(encoding='utf-8'))
    required = {'schema','issue','verdict','candidate_sha','base_sha','reviewed_tests','finding'}
    if set(document) != required or document['schema'] != 'symphony-pilot-review-decision.v1' or document['issue'] != issue or document['verdict'] != 'approved':
        raise RuntimeError('Invalid approval schema or scope')
    candidate, base = document['candidate_sha'], document['base_sha']
    if not RE_SHA.fullmatch(candidate) or not RE_SHA.fullmatch(base) or candidate == base:
        raise RuntimeError('Missing/invalid candidate and base SHA')
    producer = Path(cfg['producer_root']).expanduser() / issue
    bookmark = cfg.get('bookmark_prefix', 'symphony/') + issue
    if not producer.is_dir() or not (producer/'.git').exists():
        raise RuntimeError('Producer Git repository missing')
    if run(['git','-C',str(producer),'rev-parse','refs/heads/'+bookmark]) != candidate:
        raise RuntimeError('Producer branch advanced or SHA mismatch; re-review exact candidate')
    run(['git','-C',str(producer),'merge-base','--is-ancestor',base,candidate])
    files = run(['git','-C',str(producer),'diff','--name-only',f'{base}..{candidate}']).splitlines()
    allowed = cfg['scopes'].get(issue) or []
    if not allowed:
        raise RuntimeError('No approved paths configured for ' + issue)
    disallowed = [path for path in files if not path_allowed(path, allowed)]
    if not files or disallowed:
        raise RuntimeError(f'Candidate changed outside the approved paths for {issue}: '+repr(disallowed or files))
    stack = git_stack(producer, base, candidate, allowed)
    repo = Path(cfg['jj_repo']).expanduser().resolve()
    if not repo.is_dir() or not (repo/'.jj').is_dir():
        raise RuntimeError('Configured JJ repository is missing: ' + str(repo))
    staging = cfg['staging_bookmark']
    old = jj_commit(repo, staging)
    candidate_in_staging = jj(repo,'log','--no-graph','-r',f'({candidate}) & ancestors({staging})','-T','commit_id')
    if candidate_in_staging == candidate:
        mode = 'already-staged'
    else:
        staging_in_candidate = jj(repo,'log','--no-graph','-r',f'({staging}) & ancestors({candidate})','-T','commit_id')
        mode = 'fast-forward' if staging_in_candidate == old else 'merge'
    return {
        'issue': issue,
        'repo': cfg['name'],
        'staging_bookmark': staging,
        'staging_tip': old,
        'mode': mode,
        'candidate_sha': candidate,
        'base_sha': base,
        'candidate_commits': stack,
        'changed_paths': files,
        'validation': [' '.join(str(x) for x in c) for c in (cfg.get('validation') or [])],
    }


def jj_op_id(repo):
    try:
        return jj(repo, 'op', 'log', '--limit', '1', '-T', 'id')
    except Exception:
        return None


def stage_eligibility(issue, staging_tip):
    if eligibility is None:
        raise RuntimeError('eligibility preflight module is unavailable; refusing to stage')
    state_file = eligibility.state_path(issue)
    enforce = os.environ.get('SYMPHONY_ELIGIBILITY_STAGE_ENFORCE', '0').lower() in ('1', 'true', 'yes')
    allow_legacy = os.environ.get('SYMPHONY_ELIGIBILITY_LEGACY', '1').lower() not in ('0', 'false', 'off', 'no')
    if not state_file.is_file():
        if enforce or not allow_legacy:
            raise RuntimeError('Eligibility state is missing; refusing the legacy staging bypass')
        return {'status': 'legacy-skipped'}
    result = eligibility.check_phase(
        issue,
        'stage',
        explicit_config=REPO_CONFIG_PATH,
        staging_tip=staging_tip,
    )
    if result.get('status') != 'eligible':
        raise RuntimeError('Eligibility blocked: ' + '; '.join(result.get('reasons') or [str(result.get('status'))]))
    return result


def maybe_audit(repo, candidate, old, new, validated_tree, target, stage_log):
    required = os.environ.get('SYMPHONY_REQUIRE_STAGING_AUDIT', '0').lower() in ('1', 'true', 'yes')
    if not required:
        return {'required': False, 'verdict': 'not-run', 'path': None}
    audit_script = Path(__file__).resolve().parent / 'scripts' / 'staging_audit.py'
    if not audit_script.is_file():
        raise RuntimeError('staging audit is required but scripts/staging_audit.py is missing')
    audit_path = Path(target) / 'staging-audit.json'
    run([sys.executable, str(audit_script), '--repo', str(repo), '--candidate', candidate,
         '--old', old, '--new', new, '--expected-tree', validated_tree,
         '--out', str(audit_path)], log=stage_log)
    data = json.loads(audit_path.read_text(encoding='utf-8'))
    if data.get('verdict') != 'pass':
        raise RuntimeError('Independent staging audit failed: ' + json.dumps(data)[:1000])
    return {'required': True, 'verdict': 'pass', 'path': str(audit_path)}


def staging_receipt(repo, repo_cfg, candidate, base, stack, old, new, mode, validated_tree,
                    stage_log, validation, reviewed_tests, audit):
    receipt = {
        'schema': 'symphony-staging-receipt.v1',
        'issue': ISSUE,
        'repo': repo_cfg['name'],
        'staging_bookmark': repo_cfg['staging_bookmark'],
        'candidate_sha': candidate,
        'base_sha': base,
        'candidate_commits': stack,
        'old_staging_sha': old,
        'new_staging_sha': new,
        'integration_mode': mode,
        'validated_tree': validated_tree,
        'source_roots': {
            'producer': str(PRODUCER),
            'jj_repo': str(repo),
        },
        'validation': [' '.join(str(x) for x in c) for c in validation],
        'tests': [' '.join(str(x) for x in c) for c in validation],
        'reviewed_tests': reviewed_tests,
        'jj_operation_id': jj_op_id(repo),
        'audit': audit,
        'log': str(stage_log),
        'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    path = eligibility.write_receipt(ISSUE, receipt)
    receipt['receipt_path'] = str(path)
    return receipt


def process(decision_path, dry_run=False, manual_apply=False, local=False):
    decision_path = decision_path.resolve()
    if not decision_path.is_file():
        return
    # Gate file is owned by the independent reviewer workspace. Its directory
    # name is the authoritative issue identifier for this decision.
    global ISSUE, BOOKMARK, PRODUCER
    ISSUE = decision_path.parent.name
    if not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', ISSUE):
        raise RuntimeError('Decision file is not in an issue workspace: '+ISSUE)
    repo_cfg = resolve_repo(ISSUE)
    BOOKMARK = repo_cfg.get('bookmark_prefix', 'symphony/') + ISSUE
    PRODUCER = Path(repo_cfg['producer_root']).expanduser() / ISSUE
    expected_prefix = (Path(repo_cfg['reviewer_root']).expanduser() / ISSUE).resolve()
    if decision_path.parent != expected_prefix:
        raise RuntimeError('Decision file outside designated reviewer workspace')
    document = json.loads(decision_path.read_text(encoding='utf-8'))
    required = {'schema','issue','verdict','candidate_sha','base_sha','reviewed_tests','finding'}
    if set(document) != required or document['schema'] != 'symphony-pilot-review-decision.v1' or document['issue'] != ISSUE or document['verdict'] != 'approved':
        raise RuntimeError('Invalid approval schema or scope')
    candidate, base = document['candidate_sha'], document['base_sha']
    if not RE_SHA.fullmatch(candidate) or not RE_SHA.fullmatch(base) or candidate == base:
        raise RuntimeError('Missing/invalid candidate and base SHA')
    if not isinstance(document['reviewed_tests'], list) or not document['reviewed_tests'] or not all(isinstance(s,str) and len(s)>5 for s in document['reviewed_tests']):
        raise RuntimeError('Missing independent test record')
    if not isinstance(document['finding'],str) or len(document['finding']) < 25:
        raise RuntimeError('Missing substantive independent review finding')
    repo = Path(repo_cfg['jj_repo']).expanduser().resolve()
    if not repo.is_dir() or not (repo/'.jj').is_dir():
        raise RuntimeError('Configured JJ repository is missing: ' + str(repo))
    if not PRODUCER.is_dir() or not (PRODUCER/'.git').exists():
        raise RuntimeError('Producer Git repository missing')
    current = run(['git','-C',str(PRODUCER),'rev-parse','refs/heads/'+BOOKMARK])
    if current != candidate:
        raise RuntimeError('Producer branch advanced or SHA mismatch; re-review exact candidate')
    run(['git','-C',str(PRODUCER),'merge-base','--is-ancestor',base,candidate])
    files = run(['git','-C',str(PRODUCER),'diff','--name-only',f'{base}..{candidate}']).splitlines()
    allowed_paths = repo_cfg['scopes'].get(ISSUE)
    if not allowed_paths and REPO_CONFIG_PATH is None:
        allowed_paths = [line.strip() for line in re.split(r'[\n,]+', os.environ.get('SYMPHONY_ALLOWED_PATHS', '')) if line.strip()]
    if not allowed_paths:
        raise RuntimeError('No approved paths configured for ' + ISSUE + '; refusing to stage an unscoped candidate')
    disallowed = [path for path in files if not path_allowed(path, allowed_paths)]
    if not files or disallowed:
        raise RuntimeError(f'Candidate changed outside the approved paths for {ISSUE}: '+repr(disallowed or files))
    stack = git_stack(PRODUCER, base, candidate, allowed_paths)
    if not local:
        issue = linear_issue()
        if issue['state']['name'] != 'In Review':
            raise RuntimeError('Linear issue is no longer In Review')
    staging = repo_cfg['staging_bookmark']
    validation = repo_cfg.get('validation') or [['bb', 'validate'], ['bb', 'backstop']]
    lock_name = 'agent-' + re.sub(r'[^a-z0-9]+', '-', (repo_cfg['name'] + '-' + staging).lower()) + '.lock'
    STAGE_ROOT.mkdir(parents=True,exist_ok=True)
    with open(STAGE_ROOT/lock_name,'a+',encoding='utf-8') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not local:
            issue = linear_issue()
            if issue['state']['name'] != 'In Review':
                raise RuntimeError('Linear issue is no longer In Review')
            if not dry_run:
                claimed = claim_review(issue)
                if not claimed and not manual_apply:
                    return
                if not claimed and manual_apply:
                    print('Explicit manual apply: proceeding with the saved approved decision')
        if not local:
            stage_eligibility(ISSUE, jj_commit(repo, staging))
        old = jj_commit(repo,staging)
        # One uniquely named local remote per immutable reviewed commit. Never touch origin.
        remote = f'symphony_{ISSUE.lower().replace("-", "")}_{candidate[:12]}'
        remotes = jj(repo,'git','remote','list')
        if not any(line.split(' ',1)[0] == remote for line in remotes.splitlines()):
            jj(repo,'git','remote','add',remote,str(PRODUCER))
        else:
            if str(PRODUCER) not in next((line for line in remotes.splitlines() if line.startswith(remote+' ')), ''):
                raise RuntimeError('Existing candidate remote points elsewhere')
        jj(repo,'git','fetch','--remote',remote,'--branch',BOOKMARK)
        rev = f'{BOOKMARK}@{remote}'
        if jj_commit(repo,rev) != candidate:
            raise RuntimeError('Imported JJ revision differs from reviewed Git commit')

        # Idempotence: the accepted candidate is already in the staging history.
        candidate_in_staging = jj(repo,'log','--no-graph','-r',f'({rev}) & ancestors({staging})','-T','commit_id')
        original_mode = None
        audit = {'required': False, 'verdict': 'not-run', 'path': None}
        if candidate_in_staging == candidate:
            mode = 'already-staged'
            new = jj_commit(repo,staging)
            validated_tree = commit_tree(repo, new)
            if dry_run:
                print(json.dumps({
                    'dry_run': True,
                    'issue': ISSUE,
                    'repo': repo_cfg['name'],
                    'mode': mode,
                    'candidate_sha': candidate,
                    'base_sha': base,
                    'candidate_commits': stack,
                    'already_staged': True,
                    'bookmark_updated': False,
                    'linear_updated': False,
                }, indent=2))
                return
            stage_log = STAGE_ROOT/f'{ISSUE}-{candidate[:12]}-already-staged.log'
            stage_log.write_text(f'already integrated\nold/new stage={new}\ncandidate={candidate}\nmode={mode}\n')
            prior_path = decision_path.with_name('integration-result.json')
            if prior_path.is_file():
                try:
                    prior = json.loads(prior_path.read_text(encoding='utf-8'))
                except Exception:
                    prior = None
                if prior and prior.get('candidate_sha') == candidate and prior.get('old_staging_sha'):
                    old = prior['old_staging_sha']
                    new = prior.get('new_staging_sha', new)
                    original_mode = prior.get('integration_mode', mode)
                    validated_tree = prior.get('validated_tree', validated_tree)
                    stack = prior.get('candidate_commits', stack)
                    stage_log = Path(prior.get('log', str(stage_log)))
        else:
            # Prefer a checked fast-forward when staging is an ancestor of the
            # accepted stack. Otherwise use a history-preserving merge commit.
            staging_in_candidate = jj(repo,'log','--no-graph','-r',f'({staging}) & ancestors({rev})','-T','commit_id')
            mode = 'fast-forward' if staging_in_candidate == old else 'merge'
            suffix = '-dry-run' if dry_run else ''
            target = STAGE_ROOT / f'{ISSUE}-{candidate[:12]}-{mode}{suffix}'
            workspace_name = f'symphony-stage-{ISSUE.lower()}-{candidate[:12]}-{mode}{suffix}'
            if target.exists():
                archived = target.with_name(target.name + f'.stale-{int(time.time())}')
                target.rename(archived)
            run(['jj','--ignore-working-copy','-R',str(repo),'workspace','forget',workspace_name], check=False)
            if mode == 'fast-forward':
                jj(repo,'workspace','add','--name',workspace_name,
                    '--revision',rev,'--message',f'Validate {ISSUE} reviewed stack for fast-forward',str(target))
                if jj_commit(target,'@-') != candidate:
                    raise RuntimeError('Fast-forward workspace is not based on the reviewed candidate')
            else:
                jj(repo,'workspace','add','--name',workspace_name,
                    '--revision',staging,'--revision',rev,'--message',f'Integrate {ISSUE} reviewed stack after independent review',str(target))
            log_suffix = '-dry-run-validation.log' if dry_run else '-validation.log'
            stage_log = STAGE_ROOT/f'{ISSUE}-{candidate[:12]}-{mode}{log_suffix}'
            format_command = repo_cfg.get('format_command')
            if format_command:
                format_command = [BB if str(part).lower() == 'bb' else part for part in format_command]
                run(format_command, cwd=target, log=stage_log)
                formatting_diff = run(['jj', '-R', str(target), 'diff', '--stat'])
                if formatting_diff.strip():
                    raise RuntimeError(
                        'Formatting changed the reviewed candidate; run bb fmt in the coding workspace, re-export and re-review. No bookmark update.'
                    )
            before_tests = jj_commit(target,'@')
            validated_tree = commit_tree(repo, before_tests)
            conflicts = jj(target,'log','--no-graph','-r','conflicts() & @','-T','commit_id')
            if conflicts:
                raise RuntimeError('JJ staging workspace contains conflicts; no bookmark update')
            for command in validation:
                command = [BB if str(part).lower() == 'bb' else part for part in command]
                try:
                    run(command,cwd=target,log=stage_log)
                except Exception:
                    if dry_run:
                        print(f'Dry-run validation log: {stage_log}', file=sys.stderr)
                    raise
            if not dry_run:
                audit = maybe_audit(repo, candidate, old, before_tests, validated_tree, target, stage_log)
            if dry_run:
                print(json.dumps({
                    'dry_run': True,
                    'issue': ISSUE,
                    'repo': repo_cfg['name'],
                    'mode': mode,
                    'candidate_sha': candidate,
                    'base_sha': base,
                    'candidate_commits': stack,
                    'validated_tree': validated_tree,
                    'validation_log': str(stage_log),
                    'bookmark_updated': False,
                    'linear_updated': False,
                }, indent=2))
                return
            if jj_commit(target,'@') != before_tests:
                raise RuntimeError('Validation mutated staging commit; no bookmark update')
            if jj_commit(repo,staging) != old:
                raise RuntimeError('Concurrent stage movement detected; no bookmark update')
            if mode == 'fast-forward':
                jj(repo,'bookmark','set','--revision',candidate,staging,log=stage_log)
                new = jj_commit(repo,staging)
                if new != candidate:
                    raise RuntimeError('Fast-forward bookmark did not advance to the reviewed candidate')
            else:
                jj(target,'bookmark','set','--revision','@',staging,log=stage_log)
                new = jj_commit(repo,staging)
                if new != before_tests:
                    raise RuntimeError('Stage bookmark mismatch after integration')
            if jj(repo,'log','--no-graph','-r',f'({rev}) & ancestors({staging})','-T','commit_id') != candidate:
                raise RuntimeError('Candidate not an ancestor of staged history')
            if jj(repo,'log','--no-graph','-r',f'({old}) & ancestors({staging})','-T','commit_id') != old:
                raise RuntimeError('Previous staging head not preserved in ancestry')
        report = {'issue':ISSUE,'repo':repo_cfg['name'],'staging_bookmark':staging,'integration_mode':mode,'original_integration_mode':original_mode,'candidate_sha':candidate,'base_sha':base,'candidate_commits':stack,'old_staging_sha':old,'new_staging_sha':new,'validated_tree':validated_tree,'readmitted':candidate_in_staging == candidate,'log':str(stage_log),'tests':[' '.join(str(x) for x in c) for c in validation],'reviewed_tests':document['reviewed_tests'],'audit':audit}
        if not local:
            receipt = staging_receipt(repo, repo_cfg, candidate, base, stack, old, new, mode,
                                      validated_tree, stage_log, validation,
                                      document['reviewed_tests'], audit)
            report['receipt_path'] = receipt.get('receipt_path')
            report['jj_operation_id'] = receipt.get('jj_operation_id')
            if eligibility is not None:
                eligibility.record_staging(ISSUE, receipt, explicit_config=REPO_CONFIG_PATH)
        decision_path.with_name('integration-result.json').write_text(json.dumps(report,indent=2)+'\n')
        if local:
            print(json.dumps(report,indent=2))
            return
        issue = linear_issue()
        if issue['state']['name'] != 'In Review':
            raise RuntimeError('Issue state changed before final acceptance')
        states = issue['team']['states']['nodes']
        done = [s['id'] for s in states if s['name']=='Done']
        if len(done)!=1:
            raise RuntimeError('Could not identify unique Linear Done state')
        linear_comment(issue['id'], f'**Independent review + JJ integration passed.** Repository `{repo_cfg["name"]}`; mode `{mode}`; reviewed stack `{len(stack)}` commit(s) ending `{candidate}` (base `{base}`) preserved in local `{staging}` tip `{new}` (previous `{old}`). Integration validation: ' + ', '.join(' '.join(str(x) for x in c) for c in validation) + f' passed. Full local log: `{stage_log}`. No GitHub main merge or chain deployment.')
        result = graphql('''mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success issue{state{name}}}}''',
            {'id':issue['id'],'input':{'stateId':done[0]}})
        if not result['issueUpdate']['success'] or result['issueUpdate']['issue']['state']['name'] != 'Done':
            raise RuntimeError('Linear did not confirm issue Done')
        print(json.dumps(report,indent=2))

if __name__ == '__main__':
    if sys.argv[1:2] == ['--preflight']:
        try:
            ISSUE = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('SYMPHONY_ISSUE')
            if not ISSUE or not re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]+', ISSUE):
                raise RuntimeError('Set SYMPHONY_ISSUE or pass --preflight <ISSUE>')
            cfg = resolve_repo(ISSUE)
            BOOKMARK = cfg.get('bookmark_prefix', 'symphony/') + ISSUE
            PRODUCER = Path(cfg['producer_root']).expanduser() / ISSUE
            repo = Path(cfg['jj_repo']).expanduser().resolve()
            if not (repo/'.jj').is_dir():
                raise RuntimeError('Given repository has no .jj directory: ' + str(repo))
            staging = cfg['staging_bookmark']
            validation = cfg.get('validation') or [['bb','validate'],['bb','backstop']]
            print('Repo config:', REPO_CONFIG_PATH or '<legacy-prf>')
            print('Repo:', cfg['name'])
            print('JJ repo:', repo)
            print('Staging bookmark:', staging, '=', jj_commit(repo, staging))
            print('Git:', run(['git', '--version']))
            print('JJ:', run(['jj', '--version']))
            print('bb:', run([BB, '--version']))
            print('Issue:', ISSUE)
            print('Producer:', PRODUCER, 'exists:', PRODUCER.is_dir())
            print('Bookmark:', BOOKMARK)
            print('Allowed paths:', repr(cfg['scopes'].get(ISSUE) or os.environ.get('SYMPHONY_ALLOWED_PATHS','')))
            print('Validation:', [' '.join(str(x) for x in c) for c in validation])
            if cfg['name'] == 'prf':
                evm_root_value = os.environ.get('PRF_EVM_ROOT')
                if not evm_root_value:
                    raise RuntimeError('PRF_EVM_ROOT must be set in the local environment config')
                evm_root = Path(evm_root_value).expanduser().resolve()
                fixture = evm_root / 'evidence/composed-pro-rata-v2-settlement/manifest.json'
                print('PRF_EVM_ROOT:', evm_root, 'exists:', evm_root.is_dir())
                print('Composed settlement fixture:', fixture, 'exists:', fixture.is_file())
                if not evm_root.is_dir() or not fixture.is_file():
                    raise RuntimeError('PRF_EVM_ROOT or composed settlement manifest is missing')
            print('LINEAR_API_KEY present:', bool(os.environ.get('LINEAR_API_KEY')))
            print('Preflight read-only; no bookmark, issue, remote or working tree was changed')
        except Exception as e:
            sys.exit('Preflight blocked: '+str(e))
        sys.exit(0)
    plan_mode = sys.argv[1:2] == ['--plan']
    admit_local = sys.argv[1:2] == ['--admit-local']
    dry_run = sys.argv[1:2] == ['--dry-run']
    manual_apply = sys.argv[1:2] == ['--apply-approved']
    explicit_mode = plan_mode or admit_local or dry_run or manual_apply
    if len(sys.argv) != (3 if explicit_mode else 2):
        sys.exit('Usage: symphony-stage-reviewed.py --preflight [ISSUE] | --plan DECISION.json | --dry-run DECISION.json | --apply-approved DECISION.json | --admit-local DECISION.json | DECISION.json')
    path=Path(sys.argv[2] if explicit_mode else sys.argv[1])
    if not path.is_file():
        print('No approval decision: nothing to stage')
        sys.exit(0)
    try:
        if plan_mode:
            print(json.dumps(plan(path), indent=2))
        else:
            process(path, dry_run=dry_run, manual_apply=manual_apply, local=admit_local)
    except Exception as e:
        print('Integration blocked: '+str(e), file=sys.stderr)
        if not dry_run and not admit_local:
            suspend_review(str(e))
        sys.exit(1)
