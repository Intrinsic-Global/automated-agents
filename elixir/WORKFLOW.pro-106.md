---
tracker:
  kind: linear
  provider:
    issue_identifiers:
      - PRO-106
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

workspace:
  root: $SYMPHONY_WORKSPACE_ROOT
  cleanup_on_terminal: false

agent:
  max_concurrent_agents: 1
  max_turns: 6
  max_failure_retries: 3

codex:
  command: $CODEX_BIN --config 'model="gpt-6-luna"' --config model_reasoning_effort=medium app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
    writableRoots:
      - $SYMPHONY_WORKSPACE_ROOT
      - $SEW_SIMULATION_JJ_ROOT
      - $SEW_SIMULATION_GIT_ROOT
    readOnlyAccess:
      type: fullAccess
    networkAccess: false
    excludeTmpdirEnvVar: false
    excludeSlashTmp: false

hooks:
  before_run: $SYMPHONY_ELIXIR_ROOT/scripts/prf_dev_workspace_adapter.sh create "$PWD"
---

PRF development tooling — {{ issue.identifier }}
You are assigned Linear issue {{ issue.identifier }}.

Title: {{ issue.title }}
Status: {{ issue.state }}
URL: {{ issue.url }}
Labels: {{ issue.labels }}
Description: {{ issue.description }}

## Repository and safety boundary

- This lane is exclusively for PRO-106 and the `/home/user/Code/prf-dev` JJ repository. Verify `jj root` and `src/prf_dev/integration.py` before editing. If either check fails, stop and report the environment blocker.
- Work only in the Symphony-created issue workspace. It is pinned to the source repository's `@-` revision at dispatch. Never edit the `prf-dev` source workspace, PRF's `agent-c-staging`, `integration`, protected refs, or another issue workspace.
- Do not clone or reinitialize JJ metadata. Do not rebase, abandon, forget, rename, or clean existing workspaces. Preserve all candidate state.
- Do not publish or push, and do not move `main`, `integration`, or a staging bookmark. This repository has no configured Symphony staging bookmark or candidate export path yet.
- Only PRO-106 is authorized. Do not make changes to unrelated repositories or issue scopes.

## Work procedure

1. Read the issue and its comments. Record the workspace's baseline change and commit IDs, repository root, and clean/dirty state in a persistent `## Codex Workpad` comment.
2. Audit existing Python JJ guard code and current Symphony submission/review/staging receipts before changing code. Reuse existing controls; do not add a second scheduler, duplicate guard, identity service, or custom cryptography.
3. Reproduce a focused behavior before implementation. Add narrow tests for the accepted and rejected cases from PRO-106. Do not claim tests or offline verification that were not run.
4. Keep the implementation within this `prf-dev` workspace. Record exact commands and results, changed paths, candidate change/commit IDs, and any remaining limits in the Workpad.
5. The standard PRF `jj_export_candidate.sh` and staging gate do not apply to this repository. Keep the issue In Progress and do not add `symphony-pilot-review` until a repository-specific independent review and staging handoff exists. Leave the workspace and all evidence intact for that handoff.
