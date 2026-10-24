# Two-lane Symphony pilot: coding, independent review, local JJ staging

This bundle creates **two Symphony processes**, not two workers in one queue.

| Lane | Linear state | Required labels | Worktree | Outcome |
|---|---|---|---|---|
| Coding | Todo, In Progress | `symphony-pilot` | `.symphony-workspaces/prf-coder-v2/PRO-71` | Tested Git commit, reviewer label and `In Review` |
| Review | In Review | `symphony-pilot` + `symphony-pilot-review` | `.symphony-workspaces/prf-reviewer-v1/PRO-71` | Rework → In Progress, or host-gated JJ stage → Done |

**Only PRO-71 is allowed in this pilot.** Neither lane modifies GitHub `main` or any user's existing agent worktree. The `agent-c-staging` bookmark is a **local JJ bookmark**, not a GitHub branch.

## Install into your existing Symphony tree

```bash
cd $SYMPHONY_ELIXIR_ROOT
cp WORKFLOW.md WORKFLOW.before-two-lane.md
cp /path/to/WORKFLOW.coder.md ./WORKFLOW.md
cp /path/to/WORKFLOW.reviewer.md ./WORKFLOW.reviewer.md
cp /path/to/symphony-stage-reviewed.py ./symphony-stage-reviewed.py
chmod 700 ./symphony-stage-reviewed.py
```

In your existing `bwrap` launcher, add `unset LINEAR_ASSIGNEE` near the top (PRO-71 is unassigned). Keep all its current mounts and flags but replace its final hardcoded `./WORKFLOW.md` argument with this variable:

```bash
workflow="${SYMPHONY_WORKFLOW:-./WORKFLOW.md}"
exec bwrap "${args[@]}" mise exec -- ./bin/symphony \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails \
  "$workflow" "$@"
```

Add to the launcher's `args` before executing `bwrap`:

```bash
args+=(--setenv SYMPHONY_JJ_REPO "${SYMPHONY_JJ_REPO:-}")
```

Never put the Linear API key inside either workflow or the staging script.

## Start two processes (separate terminals)

```bash
# Coding lane; keep the existing LINEAR_API_KEY setup
SYMPHONY_WORKFLOW=./WORKFLOW.md ./your-bwrap-launcher \
  --logs-root $SYMPHONY_ELIXIR_ROOT/log-coder --port 4000
```

```bash
# Review lane. Set this to your *actual existing PRF JJ repository root*.
export SYMPHONY_JJ_REPO=$CODE_ROOT/REPLACE_WITH_ACTUAL_PRF_JJ_REPO
python3 $SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py --preflight
SYMPHONY_WORKFLOW=./WORKFLOW.reviewer.md ./your-bwrap-launcher \
  --logs-root $SYMPHONY_ELIXIR_ROOT/log-reviewer --port 4001
```

`--preflight` is read-only. Do not run the reviewer with automatic staging enabled until it reports the actual `agent-c-staging` bookmark correctly. The integration gate is prepared and Python syntax checked, but it **cannot be end-to-end tested against your host JJ repository from this environment**. Confirm your installed JJ CLI and repo topology locally before allowing the first accepted merge.

## The expected next transition

PRO-71 currently says `In Review`, but its workpad reports **no reviewable Git commit** after the first workspace bootstrap failure. It has been marked with `symphony-pilot-review` so the independent reviewer may inspect it. The reviewer should remove that review label and move the issue back to `In Progress`, supplying an exact environment-blocker review. Then the coding agent uses a new isolated repository clone, fixes/tests/commits the issue, and re-adds the review label.

When a real candidate is approved, the reviewer writes `review-decision.json` **at the end of its turn**. The Symphony `after_run` hook runs the host-side `symphony-stage-reviewed.py` gate. The gate verifies reviewed hashes and changed paths, acquires an integration lock, imports the candidate into JJ, creates **a new dedicated merge-testing JJ workspace**, checks conflicts, runs `bb validate` and `bb backstop`, moves *only* `agent-c-staging` after confirming the staging bookmark did not change concurrently, verifies candidate and prior-staging ancestry, and **then** updates Linear to Done. Failures remove the pilot review label while keeping the issue In Review. They never mark Done.

## Operational constraints

- Pilot's candidate path and staging script are pinned to **PRO-71**. Do not reuse as-is for another issue.
- Distinct log directories, dashboard ports, and workspaces are mandatory.
- The existing `symphony-review` label belongs to another, read-only JJ review workflow; **do not reuse it**. This pilot uses `symphony-pilot-review`.
- The current outer `bwrap` binds `$CODE_ROOT` read-write. Symphony's Codex `workspace-write` sandbox gives another layer, but for production tighten the outer mount and expose the staging repo only to the trusted host integration gate. Do not grant the reviewer Codex process `danger-full-access`.
- Reviewer workspace and stage logs are retained. Do not delete any existing dirty JJ workspaces to repair a failed clone.
- Stage is **not main**. GitHub PR merge, testnet deployment, main advancement, and broader scope decisions remain out of scope.
- Terminal paths should be reviewed before installation; automation does not run merely because these files exist here.
