---
tracker:
  kind: linear
  provider:
    project_slug: "prf-research-ratification-and-sew-dispute-reference-0a4afbd7f0f1"
    api_key: $LINEAR_API_KEY
  required_labels:
    - symphony-pilot
  active_states:
    - Todo
    - In Progress
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
  after_create: |
    set -eu
    git init -q .
    git remote add origin https://github.com/protocol-robustness/protocol-robustness-framework.git
    git fetch --no-tags origin main
    git checkout -q -B symphony/PRO-71 FETCH_HEAD
    test "$(git rev-parse --is-inside-work-tree)" = true
    test -f protocols_src/resolver_sim/protocols/sew/appeal_bond_projection.clj
    git rev-parse HEAD
  before_run: |
    set -eu
    test "$(git rev-parse --is-inside-work-tree)" = true
    test -f protocols_src/resolver_sim/protocols/sew/appeal_bond_projection.clj
    test "$(git branch --show-current)" = "symphony/PRO-71"
agent:
  max_concurrent_agents: 1
  max_turns: 6
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
    networkAccess: true
---

# PRF coding lane — PRO-71 only

Issue: {{ issue.identifier }} — {{ issue.title }}
Description: {{ issue.description }}

**Scope:** Only PRO-71. If another issue is presented, do not edit anything. The checkout is a dedicated Git workspace, not a JJ staging workspace. Do not modify `agent-c-staging`, other `.workspaces`, `main`, release state, or the Linear task of another issue. Do not execute deployments or transactions.

1. Validate the repository checkout and exact Git HEAD before implementing. If invalid, record a typed environment blocker in the issue and **leave the issue in In Progress** (never mislabel it In Review). End without trying to repair other users' workspaces.
2. Move Todo to In Progress. Read the workpad and actual issue acceptance. Reproduce the alleged missing-custody/orphan-bond defect on the pinned commit, and retain focused failing regression cases.
3. Implement a minimal, fail-closed repair preserving aggregate-only multi-bond claim attribution. Run the required focused checks and `bb validate` plus `bb backstop` if available; record exact commands and exits. Preserve errors rather than claiming unrun checks passed.
4. Commit on `symphony/PRO-71` only. Record exact baseline and final Git SHA, changed paths, test artifacts, and remaining limitations in a persistent Codex Workpad comment. Ensure `git status --porcelain` is clean (ignoring only documented workspace-generated artifacts).
5. **Only when there is a real reviewable committed change** and evidence: add the `symphony-pilot-review` label (preserving other labels), move issue to `In Review`, then stop. This label is the review-agent handoff. Pushing a topic branch or draft PR is optional; no merge, no Done.
6. If the reviewer returns the issue to `In Progress`, use the existing workpad, review findings and same workspace. Do not reset or rewrite the reviewed commit without explaining the successor hash. Remove any stale approval marker before continuing.

Do not use In Review as a generic blocked/failed state. Never mark Done yourself. Review and JJ integration occur in another Symphony process with independent read-only candidate access.
