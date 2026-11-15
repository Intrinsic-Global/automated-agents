# PRF Symphony operator runbook

This runbook describes the PRF coding, review, and staging path. It assumes the host has the
configured JJ repositories, repo scopes, workspace roots, `bb`, `jj`, Git, Python 3, and Linear
credentials. Interactive/JJQ work owns `agent-c-staging`; Symphony owns `symphony-staging`. Their
writers must not move the other lane's bookmark.

## Choose work and workspace scope

- Use one Linear issue and one Symphony workspace for an independent change that can be reviewed
  and accepted by itself.
- Use a parent issue as the assignment when its child tasks share implementation context or must be
  accepted as one unit. The parent workflow treats the child acceptance criteria as its scope and
  expects child evidence in the handoff.
- Do not group unrelated issues only to reduce workspace count. A bundle is one candidate and one
  review/staging unit, so a blocker in one child blocks the bundle.
- Independent candidates can be reviewed concurrently and staged together when their combined
  changes are compatible. Each issue workspace is pinned to its creation base; the agent must not
  rebase it when staging moves.

## Start and monitor the lanes

1. Put the host repo config, JJ repo paths, workspace roots, and `LINEAR_API_KEY` in the local
   `.env` or export them in the launch shell. The checked-in launcher loads `.env` automatically.
2. Start the intended lane with a supported role name, for example `./symphony coding` or
   `./symphony reviewer`. The launcher runs the configured preflight and acquires the lane lock
   before starting Symphony; a failed preflight stops launch.
3. Coding hands off a committed candidate in Linear `In Review`. The reviewer independently checks
   that exact candidate. An approved decision is fast-screened by the host and added to the pending
   queue; it does not yet change `symphony-staging` or mark the issue Done.
4. Inspect current lane activity with `./scripts/agents-status`. A completed worker invocation is
   not evidence that an issue passed review or staging.

## Inspect and qualify pending candidates

Run these commands from `elixir/` in a shell with the host environment loaded:

```bash
python3 symphony-stage-reviewed.py --list-pending prf
```

Review each selected candidate's issue, changed paths, pinned base, and related Linear scope. Select
only candidates whose combined behavior is understood and compatible. The queue rejects exact path
overlaps, but different paths can still be semantically coupled. Use separate batches when in doubt.

Pass the complete explicit issue list; the command does not consume every queued candidate:

```bash
python3 symphony-stage-reviewed.py --qualify-pending prf PRO-123 PRO-124
```

The host locks the staging lane, rechecks candidate provenance and Linear state, combines selected
candidates with current `symphony-staging`, rejects conflicts, runs configured full validation once,
and advances only `symphony-staging` after validation succeeds. It writes a durable batch record
and per-candidate receipts. Linear issues move to Done only after accepted staging receipts are
written. This is **operator-selected batch qualification**, not automatic merge-per-issue.

If conflict or validation fails, the pending records remain and staging is not advanced. Preserve
the logs and workspaces, resolve the underlying issue, and require a new reviewed candidate when
the candidate changes. If a failure occurs after staging advanced, inspect the batch record,
receipts, staging bookmark, and Linear state before retrying; do not blindly rerun qualification.

## Promote staging to integration

Integration must assemble the explicitly selected `agent-c-staging` and `symphony-staging` histories
into one candidate based on the current `integration` bookmark. Review and validate that exact
assembled candidate, including its combined commit range, before promotion. Create it in a temporary
JJ workspace, never in either staging workspace:

```bash
workspace_parent=$(mktemp -d /tmp/prf-integration-candidate.XXXXXX)
candidate_workspace="$workspace_parent/candidate"
jj -R "$PRF_JJ_REPO" workspace add --name "prf-integration-$(basename "$workspace_parent")" \
  --revision integration "$candidate_workspace"
jj -R "$candidate_workspace" new integration agent-c-staging symphony-staging \
  -m "Assemble reviewed staging streams for integration"
jj --ignore-working-copy -R "$candidate_workspace" log --no-graph -r @ -T commit_id
```

Record that exact candidate commit and resolve conflicts. Run the configured full validation against
the assembled candidate and retain its output outside the checkout. Prepare a
`prf-dev.staging-admission.v1` aggregate record binding that candidate, current integration target,
ordered commit range, selected Linear issues, and digest-bound passing validation receipts. The
integration reviewer will refuse to launch without that record. Then prepare a dedicated
integration-review issue naming the candidate and all included issues. Before routing it to
Symphony, pin the candidate, target, and qualification record in an immutable host manifest:

The staging record follows `../../../prf-dev/schemas/staging-admission.v1.json`. Its `source_commit` and
`source_tree` are the assembled candidate; its `target_commit` and `target_tree` are the current
integration base; `included_commits` is the exact ordered range from target to candidate; and
`issues` lists each included Linear issue once. `validation_evidence` contains one entry per required
command, each with `status: passed`, the exact candidate tree, the command string, an absolute log
path outside the checkout, and that log's SHA-256. The reviewer host rechecks those files and
digests immediately before promotion.

```bash
python3 "$SYMPHONY_ELIXIR_ROOT/scripts/symphony_integration_review.py" prepare \
  --repo "$PRF_JJ_REPO" \
  --issue PRO-900 \
  --source-commit <assembled-candidate-commit> \
  --target-ref integration \
  --staging-record /path/to/aggregate-staging-admission.json \
  --issues PRO-123 PRO-124
```

Use a new integration-review issue ID for each attempt. The command rejects a candidate that does
not descend from current `integration`, records its exact tree and ordered commit range, and refuses
to overwrite an existing manifest. Route that issue to the `integration-reviewer` role with both
`symphony-integration` and `symphony-integration-review` labels in `In Review`:

```bash
./symphony integration-reviewer
```

The reviewer can read the pinned candidate from JJ but cannot modify it, update Linear, or move
bookmarks. The host `before_run` hook rechecks the candidate, integration target, and qualification
digest. On approval, `after_run` writes the formal `prf-dev.integration-review.v1` record and raw
decision, builds the aggregate admission, and runs `prf-dev` preflight, conditional promotion, and
verification. It moves only the local `integration` bookmark after all checks pass, then marks the
integration-review issue Done. Publishing `main` remains a separate authorized action. A rejected
review does not produce approval evidence; the host moves the review issue to Todo and retains its
findings. Address them, prepare a new manifest under a new integration-review issue, and do not rerun
the model against the retained decision. A stale or blocked promotion also moves the review issue to
Todo and leaves integration unchanged; inspect its retained admission, preflight, and promotion
records before reconciliation. If promotion verifies but the Linear state update fails, reconcile
the already-promoted bookmark and record before closing the review issue.

The handoff artifacts are retained under `$SYMPHONY_INTEGRATION_REVIEW_DIR`: prepared manifests,
raw decisions, formal review records, aggregate admissions, preflight reports, and promotion
receipts. Preserve them with the aggregate staging record and validation logs. The one-issue
`prf-dev integration trigger` remains separate and is not used for this multi-issue lane.

## Repair and recovery

For an ordinary candidate conflict or failed check, keep it off the shared staging bookmark, resolve
it in its assigned workspace, rerun affected checks, publish a new candidate, and require review of
that exact candidate. Do not rewrite another agent's workspace or repair a protected bookmark as a
shortcut.

For protected history or authority-bearing repairs, follow PRO-90's separate sequence: assessment,
prior authorization, execution evidence, independent review, verification, and publication receipt
only when separately authorized. See `../../../prf-dev/docs/repairs.md` and its versioned schemas.
A local repair note or successful replay is provenance, not repair authorization or publication
approval.

## Token and retry limits

Coding workflows cap each turn at 50,000 reported tokens, 40 tool calls, and 15 minutes. Reviewer
workflows have their own limits. Token usage is observed when Codex reports it, so a response may
exceed the configured token ceiling before the supervisor stops the turn. These settings reduce
runaway work; they do not impose a hard provider billing limit. Continuation and failure retry
limits bound repeated invocations, and quarantined issues require operator reconciliation.
