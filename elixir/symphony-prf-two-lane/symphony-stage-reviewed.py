#!/usr/bin/env python3
"""Constrained PRO-71 post-review gate: JJ agent-c-staging, then Linear Done.

Runs only as a Symphony *host-side after_run hook*; never from Codex.
Requires SYMPHONY_JJ_REPO to point to an existing PRF Jujutsu repository.
This is a first-pilot integration gate: audit and locally dry-run before enabling
on other issues. It never modifies GitHub main or the operator's JJ worktree.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from local_env import load_local_env

load_local_env()

ISSUE = 'PRO-71'
LABEL = 'symphony-pilot-review'
CODE_ROOT = Path(os.environ.get('SYMPHONY_WORKSPACES_ROOT', str(Path.home() / '.local/share/symphony/workspaces'))).expanduser()
PRODUCER = CODE_ROOT / 'prf-coder-v2' / ISSUE
STAGE_ROOT = CODE_ROOT / 'staging-integrations'
RE_SHA = re.compile(r'^[0-9a-f]{40}$')


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
    shared = os.environ.get('SYMPHONY_JJ_REPO')
    if shared and Path(shared).expanduser().resolve() == Path(repo).resolve():
        argv.append('--ignore-working-copy')
    return run([*argv, '-R', str(repo), *args], log=log)


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
        raise RuntimeError('Linear issue lookup does not match PRO-71')
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


def process(decision_path):
    decision_path = decision_path.resolve()
    if not decision_path.is_file():
        return
    # Gate file is owned by the independent reviewer workspace.
    expected_prefix = (CODE_ROOT / 'prf-reviewer-v1' / ISSUE).resolve()
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
    repo = Path(os.environ.get('SYMPHONY_JJ_REPO','')).expanduser().resolve()
    if not os.environ.get('SYMPHONY_JJ_REPO') or not (repo/'.jj').is_dir():
        raise RuntimeError('SYMPHONY_JJ_REPO is missing or not an existing JJ repo')
    if not PRODUCER.is_dir() or not (PRODUCER/'.git').exists():
        raise RuntimeError('Producer Git repository missing')
    current = run(['git','-C',str(PRODUCER),'rev-parse','refs/heads/symphony/PRO-71'])
    if current != candidate:
        raise RuntimeError('Producer branch advanced or SHA mismatch; re-review exact candidate')
    run(['git','-C',str(PRODUCER),'merge-base','--is-ancestor',base,candidate])
    files = run(['git','-C',str(PRODUCER),'diff','--name-only',f'{base}..{candidate}']).splitlines()
    if not files or any('appeal_bond_projection' not in path or not path.endswith('.clj') for path in files):
        raise RuntimeError('Candidate changed outside the PRO-71 appeal-bond projection and focused tests: '+repr(files))
    if linear_issue()['state']['name'] != 'In Review':
        raise RuntimeError('Linear issue is no longer In Review')
    STAGE_ROOT.mkdir(parents=True,exist_ok=True)
    with open(STAGE_ROOT/'agent-c-staging.lock','a+',encoding='utf-8') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old = jj_commit(repo,'agent-c-staging')
        # One uniquely named local remote per immutable reviewed commit. Never touch origin.
        remote = f'symphony_{ISSUE.lower().replace("-", "")}_{candidate[:12]}'
        remotes = jj(repo,'git','remote','list')
        if not any(line.split(' ',1)[0] == remote for line in remotes.splitlines()):
            jj(repo,'git','remote','add',remote,str(PRODUCER))
        else:
            if str(PRODUCER) not in next((line for line in remotes.splitlines() if line.startswith(remote+' ')), ''):
                raise RuntimeError('Existing candidate remote points elsewhere')
        jj(repo,'git','fetch','--remote',remote,'--branch','symphony/PRO-71')
        rev = f'symphony/PRO-71@{remote}'
        if jj_commit(repo,rev) != candidate:
            raise RuntimeError('Imported JJ revision differs from reviewed Git commit')
        # Idempotence: prior successful stage may not have set Linear Done if its API failed.
        ancestry = jj(repo,'log','--no-graph','-r',f'({rev}) & ancestors(agent-c-staging)','-T','commit_id')
        if candidate in ancestry:
            new = jj_commit(repo,'agent-c-staging')
            stage_log = STAGE_ROOT/f'{ISSUE}-{candidate[:12]}-already-staged.log'
            stage_log.write_text(f'already integrated\nold/new stage={new}\ncandidate={candidate}\n')
        else:
            target = STAGE_ROOT / f'{ISSUE}-{candidate[:12]}'
            if target.exists():
                raise RuntimeError(f'Existing staging test workspace; preserve and inspect: {target}')
            jj(repo,'workspace','add','--name',f'symphony-stage-{ISSUE.lower()}-{candidate[:12]}',
                '--revision','agent-c-staging','--revision',rev,'--message',f'Integrate {ISSUE} after independent review',str(target))
            stage_log = STAGE_ROOT/f'{ISSUE}-{candidate[:12]}-validation.log'
            before_tests = jj_commit(target,'@')
            conflicts = jj(target,'log','--no-graph','-r','conflicts() & @','-T','commit_id')
            if conflicts:
                raise RuntimeError('JJ staging merge contains conflicts; no bookmark update')
            # These are intentionally independent validation commands on the merged tree.
            for command in (['bb','validate'],['bb','backstop']):
                run(command,cwd=target,log=stage_log)
            if jj_commit(target,'@') != before_tests:
                raise RuntimeError('Validation mutated staging commit; no bookmark update')
            if jj_commit(repo,'agent-c-staging') != old:
                raise RuntimeError('Concurrent stage movement detected; no bookmark update')
            jj(target,'bookmark','set','--revision','@','agent-c-staging',log=stage_log)
            new = jj_commit(repo,'agent-c-staging')
            if new != before_tests:
                raise RuntimeError('Stage bookmark mismatch after integration')
            if jj(repo,'log','--no-graph','-r',f'({rev}) & ancestors(agent-c-staging)','-T','commit_id') != candidate:
                raise RuntimeError('Candidate not an ancestor of staged merge')
            if jj(repo,'log','--no-graph','-r',f'({old}) & ancestors(agent-c-staging)','-T','commit_id') != old:
                raise RuntimeError('Previous staging head not preserved in ancestry')
        report = {'issue':ISSUE,'candidate_sha':candidate,'base_sha':base,'old_staging_sha':old,'new_staging_sha':new,'log':str(stage_log),'tests':['bb validate','bb backstop'],'reviewed_tests':document['reviewed_tests']}
        decision_path.with_name('integration-result.json').write_text(json.dumps(report,indent=2)+'\n')
        issue = linear_issue()
        if issue['state']['name'] != 'In Review':
            raise RuntimeError('Issue state changed before final acceptance')
        states = issue['team']['states']['nodes']
        done = [s['id'] for s in states if s['name']=='Done']
        if len(done)!=1:
            raise RuntimeError('Could not identify unique Linear Done state')
        linear_comment(issue['id'], f'**Independent review + JJ integration passed.** Candidate `{candidate}` (base `{base}`) is an ancestor of local `agent-c-staging` commit `{new}` (previous `{old}`). Integration validation: `bb validate`, `bb backstop` passed. Full local log: `{stage_log}`. No GitHub main merge or chain deployment.')
        result = graphql('''mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success issue{state{name}}}}''',
            {'id':issue['id'],'input':{'stateId':done[0]}})
        if not result['issueUpdate']['success'] or result['issueUpdate']['issue']['state']['name'] != 'Done':
            raise RuntimeError('Linear did not confirm issue Done')
        print(json.dumps(report,indent=2))

if __name__ == '__main__':
    if sys.argv[1:] == ['--preflight']:
        try:
            raw = os.environ.get('SYMPHONY_JJ_REPO')
            if not raw:
                raise RuntimeError('Set SYMPHONY_JJ_REPO to an existing local PRF JJ repository')
            repo = Path(raw).expanduser().resolve()
            if not (repo/'.jj').is_dir():
                raise RuntimeError('Given repository has no .jj directory: ' + str(repo))
            print('JJ repo:', repo)
            print('agent-c-staging:', jj_commit(repo, 'agent-c-staging'))
            print('Git:', run(['git', '--version']))
            print('JJ:', run(['jj', '--version']))
            print('bb:', run(['bb', '--version']))
            print('LINEAR_API_KEY present:', bool(os.environ.get('LINEAR_API_KEY')))
            print('Preflight read-only; no bookmark, issue, remote or working tree was changed')
        except Exception as e:
            sys.exit('Preflight blocked: '+str(e))
        sys.exit(0)
    if len(sys.argv)!=2:
        sys.exit('Usage: symphony-stage-reviewed.py --preflight | /absolute/reviewer/workspace/review-decision.json')
    path=Path(sys.argv[1])
    if not path.is_file():
        print('No approval decision: nothing to stage')
        sys.exit(0)
    try:
        process(path)
    except Exception as e:
        print('Integration blocked: '+str(e), file=sys.stderr)
        suspend_review(str(e))
        sys.exit(1)
