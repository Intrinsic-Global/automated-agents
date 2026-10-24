---
tracker:
  kind: linear
  provider:
    project_slug: "deployment-bound-forensics-v0-ac0351862195"
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
    issue=$(basename "$PWD")
    src=$SYMPHONY_WORKSPACES_ROOT/prf-coder-v2/$issue
    if git -C "$src" rev-parse --verify "refs/heads/symphony/$issue" >/dev/null 2>&1; then
      git fetch --no-tags "$src" "+refs/heads/symphony/$issue:refs/remotes/producer/$issue"
      git checkout -q --detach "refs/remotes/producer/$issue"
      git reset -q --hard "refs/remotes/producer/$issue"
    fi
    $SYMPHONY_ELIXIR_ROOT/scripts/review_handoff_check.sh
  after_run: |
    set -eu
    if [ -f ./review-decision.json ]; then
      $SYMPHONY_ELIXIR_ROOT/scripts/review_decision_record.sh ./review-decision.json
      python3 $SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py ./review-decision.json
    fi
agent:
  max_concurrent_agents: 2
  max_turns: 1
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
    networkAccess: true
---

# Independent reviewer lane — {{ issue.identifier }}

Linear issue: {{ issue.identifier }} — {{ issue.title }}
Description: {{ issue.description }}

This is **not** the coding agent. You cannot approve your own work. Inspect the coding agent's exact committed candidate and independently validate it. Do not change the candidate repository or the user's JJ workspaces. Do not merge into main, deploy, or change financial/chain authority. You may write review artifacts **only** in this independent reviewer workspace.

Use the `linear_graphql` tool for every Linear write (`commentCreate`, `issueUpdate`). The app connector can require an unavailable approval and silently drop the write.

## Locate the candidate

The producer checkout is `$SYMPHONY_WORKSPACES_ROOT/prf-coder-v2/{{ issue.identifier }}`, Git branch `symphony/{{ issue.identifier }}`. `before_run` attempts to fetch its exact revision into `refs/remotes/producer/{{ issue.identifier }}` here. Verify `git rev-parse refs/remotes/producer/{{ issue.identifier }}` succeeds and that there is at least one relevant committed difference from its known original base. The `before_run` hook fetches the producer and detaches this workspace at the exact candidate before your turn starts. Verify `git rev-parse HEAD` equals `git rev-parse refs/remotes/producer/{{ issue.identifier }}`; if it does not, stop and report the mismatch. Also read the latest Codex Workpad handoff, if one exists, and extract the candidate SHA it names. A missing Workpad is not by itself a blocker when the producer ref and issue evidence pin the candidate. If a Workpad names a candidate and `HEAD` is not that exact SHA, treat the handoff as stale (for example, a coding lane that moved the issue to In Review before its producer export completed): do not approve and do not re-review the older commit. Post the mismatch, remove `symphony-pilot-review`, keep `symphony-pilot`, and move the issue back to In Progress so the coding lane republishes the exact named candidate; then re-review the new SHA. Do not attempt to change Git metadata yourself (the inner sandbox may deny it). Read the Linear Codex Workpad and reproduce the claimed results. Never use an uncommitted producer working tree as review evidence. A draft GitHub PR may supplement but must not substitute for exact local commit verification.

## If incomplete, unreviewable, or tests fail

- Post a structured independent review with revision(s), exact failures, reproduction, scope and next actions in Linear **before** changing any Linear state. Symphony stops this turn as soon as the issue leaves `In Review`, so changing state first can discard the review.
- For a **code finding** (tests fail, implementation incomplete, scope drift): remove only `symphony-pilot-review`, set the issue back to `In Progress`, and stop so coding can fix it.
- For a **host/environment blocker** (missing producer ref, missing `bb` or other required tool, read-only shared store, gate preflight failure): do **not** set the issue to `In Progress`. Post the exact blocker, remove **both** `symphony-pilot` and `symphony-pilot-review`, leave the issue in `In Review`, and stop. This prevents both lanes from retrying until a human repairs the environment and re-adds `symphony-pilot`.
- Never create an `approved` decision file on failure, missing source, missing baseline, unrun required test, unverifiable authority or scope drift. Avoid an endless review/rework cycle: require new evidence or a new candidate SHA before accepting a subsequent handoff.

## If independently accepted

1. Confirm the candidate's exact Git SHA and original base; review changed paths, tests and malicious/negative vectors, performance, permissions and fail-closed behavior. Verify no undisclosed changes, including to generated evidence, workflow infrastructure or staging target.
2. The host gate source is `$SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py`; audit it there. Use `python3`, not `python` (the sandbox shell may lack a `python` alias). The sandbox shell may not expose `LINEAR_API_KEY`; the host `after_run` gate owns that credential and fails closed if it is absent, so do not block a decision solely because your shell reports it missing. Check the shared store read-only: `jj --ignore-working-copy -R "$SYMPHONY_JJ_REPO" log --no-graph -r agent-c-staging -T commit_id` and `jj --ignore-working-copy -R "$SYMPHONY_JJ_REPO" log --no-graph -r "agent-c-staging & bookmarks()" -T bookmarks`. The reviewer sandbox intentionally lacks write access to the shared store; `--ignore-working-copy` is required and a lock/write error without it is **not** a blocker. Only if the explicit `--ignore-working-copy` read fails, or `SYMPHONY_JJ_REPO` is missing/not a PRF JJ repo, keep In Review, remove both pilot labels, and explain the staging blocker. Do not mark Done.
3. Write `review-decision.json` in **this workspace** as the LAST step of your turn, with strictly this structure:

   ```json
   {"schema":"symphony-pilot-review-decision.v1","issue":"{{ issue.identifier }}","verdict":"approved","candidate_sha":"<40 lowercase hex Git SHA>","base_sha":"<40 lowercase hex Git SHA>","reviewed_tests":["<exact successful test command>"],"finding":"<independent concise finding>"}
   ```

   The host-side `after_run` integration gate validates the decision, imports the exact commit into JJ, merges **only to `agent-c-staging`** in a new dedicated JJ workspace, independently runs staging validation, verifies ancestry and recorded state, and only then changes Linear to Done. Neither this agent nor the coding agent marks Done directly.
4. Leave the issue `In Review` and do not push to Git main. If integration fails, the gate writes a failure report and removes the review label so the issue stays In Review without infinite retries; escalate that blocker. Preserve all existing named agent bookmarks/workspaces.

**Do not attempt a direct merge from inside Codex.** The trusted host integration gate is the only actor permitted to update `agent-c-staging`. Its script and environment must be audited before enabling it on real work.
