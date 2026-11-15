---
tracker:
  kind: linear
  provider:
    excluded_project_slugs:
      - "pro-rata-production-readiness-af3912df6995"
      - "protected-operations-base-sepolia-b90b18edae8f"
      - "researcher-challenges-v0-dfe8a65db477"
      - "deployment-bound-forensics-v0-ac0351862195"
    excluded_issue_identifiers:
      - PRO-106
    api_key: $LINEAR_API_KEY
  required_labels:
    - symphony-integration
    - symphony-integration-review
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
  cleanup_on_terminal: false
hooks:
  timeout_ms: 3600000
  before_run: |
    set -eu
    issue=$(basename "$PWD")
    python3 "$SYMPHONY_ELIXIR_ROOT/scripts/symphony_integration_review.py" context \
      --repo "$SYMPHONY_JJ_SOURCE_REPO" --issue "$issue" --workspace "$PWD"
  after_run: |
    set -eu
    issue=$(basename "$PWD")
    test -f ./review-decision.json || {
      echo "integration reviewer did not produce review-decision.json" >&2
      exit 1
    }
    python3 "$SYMPHONY_ELIXIR_ROOT/scripts/symphony_integration_review.py" record \
      --repo "$SYMPHONY_JJ_SOURCE_REPO" --issue "$issue" --decision ./review-decision.json
agent:
  max_concurrent_agents: 1
  max_turns: 1
  max_continuation_retries: 0
  max_failure_retries: 0
codex:
  turn_timeout_ms: 720000
  max_tool_calls_per_turn: 30
  max_tokens_per_turn: 50000
  command: $CODEX_BIN --config 'model="gpt-6-luna"' --config model_reasoning_effort=medium app-server
  approval_policy: never
  stall_timeout_ms: 3900000
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
---

# PRF integration reviewer — {{ issue.identifier }}

You are the independent reviewer for one assembled PRF staging candidate. This is a distinct
integration review, not an issue implementation review. The host prepared `review-context.json`
from an immutable candidate commit and the current `integration` target. If that context is absent,
stale, malformed, or the candidate does not descend from the target, stop without approval.

Read the issue description and `review-context.json`. Review every commit in `reviewed_commits`
and the combined diff. Use the context's exact commit IDs; do not review moving staging bookmarks.
Inspect the diff with the supplied `diff_command`, then inspect relevant file contents and tests
using read-only JJ commands. Consider cross-issue interactions, missed acceptance criteria,
regressions, security, data migrations, and whether the exact combined candidate is coherent.
The staging qualification and integration preflight are separate host gates; do not claim they
passed unless the evidence is present and binds this exact tree.

Do not edit the candidate, run a merge, move bookmarks, change Linear, publish, push, or modify
another workspace. Write only `review-decision.json` in this reviewer workspace. Return
`status: approved` only when the exact aggregate candidate is acceptable. Otherwise use
`status: changes_requested` and list actionable findings. Include every context identity field
unchanged, identify yourself as `symphony-integration-reviewer`, and retain findings even when
the list is empty.

The host after-run hook validates the decision against the prepared manifest, current JJ refs, and
the exact-tree aggregate staging qualification record. On approval it writes a
`prf-dev.integration-review.v1` record, assembles the aggregate admission, and runs
`prf-dev` preflight, conditional promotion, and verification. It advances only `integration`, and
only if every evidence digest, workspace audit, source/target identity, and fast-forward check
passes. It marks this Linear review issue Done only after promotion verification. A rejected review
or blocked gate moves it to Todo after retaining evidence, removing it from this lane's active state
and preventing a repeated model turn. It never pushes or publishes `main`.

Write this exact JSON shape:

```json
{
  "schema": "symphony.integration-review-decision.v1",
  "status": "approved",
  "source_commit": "40-character commit ID from review-context.json",
  "target_commit": "40-character commit ID from review-context.json",
  "source_tree": "40-character tree ID from review-context.json",
  "target_tree": "40-character tree ID from review-context.json",
  "reviewed_commits": ["ordered commit IDs from review-context.json"],
  "reviewer": "symphony-integration-reviewer",
  "issues": ["issue IDs from review-context.json"],
  "findings": []
}
```
