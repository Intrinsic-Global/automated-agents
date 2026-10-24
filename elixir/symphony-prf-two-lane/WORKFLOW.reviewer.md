---
tracker:
  kind: linear
  provider:
    project_slug: "prf-research-ratification-and-sew-dispute-reference-0a4afbd7f0f1"
    api_key: $LINEAR_API_KEY
  required_labels:
    - symphony-pilot
    - symphony-pilot-review
  active_states:
    - In Review
  terminal_states:
    - Done
    - Canceled
    - Cancelled
    - Duplicate
polling:
  interval_ms: 15000
workspace:
  root: $SYMPHONY_WORKSPACE_ROOT
hooks:
  timeout_ms: 3600000
  after_create: |
    set -eu
    git init -q .
    git remote add origin https://github.com/protocol-robustness/protocol-robustness-framework.git
    git fetch --no-tags origin main
    git checkout -q --detach FETCH_HEAD
    test "$(git rev-parse --is-inside-work-tree)" = true
    test -f protocols_src/resolver_sim/protocols/sew/appeal_bond_projection.clj
  before_run: |
    set -eu
    test "$(git rev-parse --is-inside-work-tree)" = true
    test -f protocols_src/resolver_sim/protocols/sew/appeal_bond_projection.clj
    rm -f ./review-decision.json
    src=$SYMPHONY_WORKSPACES_ROOT/prf-coder-v2/PRO-71
    if git -C "$src" rev-parse --verify refs/heads/symphony/PRO-71 >/dev/null 2>&1; then
      git fetch --no-tags "$src" "+refs/heads/symphony/PRO-71:refs/remotes/producer/PRO-71"
    fi
  after_run: |
    set -eu
    if [ -f ./review-decision.json ]; then
      python3 $SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py ./review-decision.json
    fi
agent:
  max_concurrent_agents: 1
  max_turns: 1
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
    networkAccess: true
---

# Independent reviewer lane — PRO-71 only

Linear issue: {{ issue.identifier }} — {{ issue.title }}
Description: {{ issue.description }}

This is **not** the coding agent. You cannot approve your own work. Inspect the coding agent's exact committed candidate and independently validate it. Do not change the candidate repository or the user's JJ workspaces. Do not merge into main, deploy, or change financial/chain authority. You may write review artifacts **only** in this independent reviewer workspace.

## Locate the candidate

The producer checkout is `$SYMPHONY_WORKSPACES_ROOT/prf-coder-v2/PRO-71`, Git branch `symphony/PRO-71`. `before_run` attempts to fetch its exact revision into `refs/remotes/producer/PRO-71` here. Verify `git rev-parse refs/remotes/producer/PRO-71` succeeds and that there is at least one relevant committed difference from its known original base. Read the Linear Codex Workpad and reproduce the claimed results. Never use an uncommitted producer working tree as review evidence. A draft GitHub PR may supplement but must not substitute for exact local commit verification.

## If incomplete, unreviewable, or tests fail

- Post a structured independent review with revision(s), exact failures, reproduction, scope and next actions in Linear.
- Remove `symphony-pilot-review` label **without removing other labels**. Set the issue back to `In Progress`, and stop. This includes the current PRO-71 handoff, which has no reviewable commit from the first failed bootstrap; returning it to In Progress is a repair, not review acceptance.
- Never create an `approved` decision file on failure, missing source, missing baseline, unrun required test, unverifiable authority or scope drift. Avoid an endless review/rework cycle: require new evidence or a new candidate SHA before accepting a subsequent handoff.

## If independently accepted

1. Confirm the candidate's exact Git SHA and original base; review changed paths, tests and malicious/negative vectors, performance, permissions and fail-closed behavior. Verify no undisclosed changes, including to generated evidence, workflow infrastructure or staging target.
2. Check that `SYMPHONY_JJ_REPO` is present, resolves to an actual PRF JJ repository, and that `agent-c-staging` is a single, existing local JJ bookmark. If absent, keep In Review, remove `symphony-pilot-review` to suspend redispatch, and explain the staging blocker. Do not mark Done.
3. Write `review-decision.json` in **this workspace** as the LAST step of your turn, with strictly this structure:

   ```json
   {"schema":"symphony-pilot-review-decision.v1","issue":"PRO-71","verdict":"approved","candidate_sha":"<40 lowercase hex Git SHA>","base_sha":"<40 lowercase hex Git SHA>","reviewed_tests":["<exact successful test command>"],"finding":"<independent concise finding>"}
   ```

   The host-side `after_run` integration gate validates the decision, imports the exact commit into JJ, merges **only to `agent-c-staging`** in a new dedicated JJ workspace, independently runs staging validation, verifies ancestry and recorded state, and only then changes Linear to Done. Neither this agent nor the coding agent marks Done directly.
4. Leave the issue `In Review` and do not push to Git main. If integration fails, the gate writes a failure report and removes the review label so the issue stays In Review without infinite retries; escalate that blocker. Preserve all existing named agent bookmarks/workspaces.

**Do not attempt a direct merge from inside Codex.** The trusted host integration gate is the only actor permitted to update `agent-c-staging`. Its script and environment must be audited before enabling it on real work.
