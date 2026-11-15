# Symphony Elixir

This directory contains the Elixir/OTP implementation of this Symphony fork. It follows the
repository's [`SPEC.md`](../SPEC.md) and adds optional admission checks and review/staging
integrations. The service is experimental; review host hooks, workflows, and sandbox settings before
using it with a project.

## How it works

Symphony polls a configured issue tracker, creates an isolated workspace for each issue, and starts
Codex in app-server mode with a project workflow prompt. It can expose a Phoenix dashboard and JSON
API when enabled in configuration.

## Prerequisites

- Elixir 1.19.5 and Erlang/OTP 28 (versions are pinned in `mise.toml`).
- [mise](https://mise.jdx.dev/) to install the pinned runtimes.
- Codex CLI and credentials for the configured tracker.
- Python 3 and PyYAML for launcher and eligibility preflights (enabled by default).

## Set up

From this directory:

```bash
mise install
mise trust
mise exec -- mix setup
cp .env.example .env
cp WORKFLOW.example.md WORKFLOW.md
```

Edit `.env` for local paths and `WORKFLOW.md` for your tracker, project, and Codex policy. The
launcher and helper scripts load `.env`; when starting the service directly, source it first with
`set -a; source .env; set +a`. Keep credentials in environment variables; do not commit a real
`WORKFLOW.md` containing secrets.

For Linear, set `LINEAR_API_KEY`. Other adapters and their required settings are described below.

## Run

```bash
mise exec -- mix build
mise exec -- ./bin/symphony ./WORKFLOW.md
```

Optional flags include `--logs-root PATH` and `--port PORT` (enables the Phoenix dashboard/API).
Without an explicit workflow path, Symphony reads `./WORKFLOW.md`.

## Agent status summary

Generate `status.md` from every lane's current and rotated Symphony logs:

```bash
./scripts/agents-status
```

The table shows each lane's latest agent invocation separately from its dispatch scans over the
last 15 minutes. Scans are scheduler polls, not agent invocations. It includes the age and freshness
of the latest log event so old failures are not mistaken for current ones. “Completed” means the worker invocation ended; it does not mean Linear
moved the issue to Done. “Recent” means there was recent log activity, not that the process is
confirmed alive. The report does not infer process liveness. It reads `$SYMPHONY_LOGS_ROOT` when
set, otherwise `~/Code/.symphony-logs`. The generated file is local and ignored by Git. Use
`--output PATH` to write elsewhere or `--json` to print machine-readable records. Run the command
again to refresh it.

## Configuration

`WORKFLOW.md` contains YAML front matter and a Markdown body used as the Codex prompt. Start from
[`WORKFLOW.example.md`](WORKFLOW.example.md). The included role-specific workflow files are
examples; adapt them to your own repositories before use.

A minimal workflow looks like this:

```yaml
tracker:
  kind: linear
  provider:
    project_slugs:
      - "your-project-slug"
    api_key: $LINEAR_API_KEY
workspace:
  root: ~/code/workspaces
hooks:
  after_create: |
    git clone https://github.com/your-org/your-repository.git .
agent:
  max_concurrent_agents: 2
  max_turns: 10
  max_continuation_retries: 3
codex:
  command: codex app-server
```

Tracker tools run host-side with configured credentials; Symphony removes declared tracker credentials
from the Codex child environment. Raw provider tools retain the access granted to the configured
credential, so use credentials with appropriate scope.

Normally completed workers that leave an issue active are continued only up to
`agent.max_continuation_retries`. Continuation and failure retry counts are persisted under the
workspace root and survive an orchestrator restart; exceeding a configured limit quarantines the
issue for operator reconciliation. This bounds repeated invocations, while `max_turns` bounds turns
inside one invocation.

Reviewer workflows also claim a durable review identity before Codex starts. It hashes the issue
description, repository, exact candidate commit and tree, pinned base, declared scope, and review
policy version; comments and ordinary tracker metadata are excluded. A completed, crashed, or
inconclusive attempt blocks automatic review of that same identity. A changed candidate or policy
creates a new identity. Each reviewer workflow sets a 12 minute turn timeout, 30 tool-call limit,
and 100,000-token limit. Token limits take effect when Codex reports usage, so one response can
exceed the configured value before the supervisor sees it. The 100,000-token value is a provisional
ceiling and should be adjusted from observed successful review usage.

An operator can authorize one deliberate retry of a blocked review identity with:

```bash
python3 scripts/symphony_eligibility.py authorize-review-retry \
  --issue PRO-123 --identity REVIEW_ID --reason "candidate reconciled after failed run"
```

The identity is printed by the `check --phase review` and `claim-review` commands and stored in the
per-issue state record.

The orchestrator accepts an injectable runner module at its runner boundary. ExUnit uses this to run
a deterministic coder and independent reviewer through real issue dispatch, workspace creation,
JJ candidate creation, and tracker-state transitions without Codex credentials.

Eligibility checks use `scripts/symphony_eligibility.py` and `symphony-repos.json` by default. Set
`SYMPHONY_REPO_CONFIG` to select another config file. The checked-in example config is intentionally
absent: create one for your environment using the schema described by the scripts and workflows.
Admission can be disabled with `SYMPHONY_ELIGIBILITY_ENFORCE=0`; only do so after reviewing how that
changes your workflow's checks.

PRF issue workspaces keep the exact `symphony-staging` base recorded when they are created. This
bookmark is the automated Symphony lane; interactive/JJQ work continues to use `agent-c-staging`.
The refs are intentionally separate so their writers cannot race on one staging bookmark. If
Symphony staging advances while an independent issue is still being coded, the agent may continue from that
pinned base; the host does not rebase the workspace. Review and staging admission accept an older
base only when it remains an ancestor of current staging. The host combines each reviewed candidate
with current `symphony-staging`, rejects conflicts or diverged history, and runs bb backstop:fast before
recording it in the pending staging queue. Pending candidates do not move symphony-staging, receive
an accepted staging receipt, or unlock Linear dependencies. Operators batch compatible candidates
with python3 symphony-stage-reviewed.py --qualify-pending prf <ISSUE ...>, from a shell with the
Symphony host environment loaded (repo config, workspace root, and Linear credentials). Inspect
the queue with --list-pending prf. Select issue IDs explicitly; the batch command does not consume
the entire queue by default. It runs configured full validation once against the exact combined
tree, then advances symphony-staging and records a receipt for each candidate. Candidates that touch
the same paths must be qualified in separate batches, and operators must select candidates with
compatible implementation scope;
the exact-path overlap check does not detect semantic coupling across different files. A durable
batch record is retained under staging-integrations/batch-records. Integration must explicitly
assemble and qualify the selected interactive and Symphony staging histories; neither lane may
advance the other's staging bookmark.
Linear blockers remain strict and require the full batch's accepted staging receipt.
Conflict and validation failures preserve pending records without moving staging. If receipt writing
or a Linear update fails after staging advances, reconcile the staging result and logs before retrying.

The schema supports tracker selection, workspace paths, hooks, concurrency and retry limits, Codex
policy, logging, and optional server settings. See `../SPEC.md` and
`lib/symphony_elixir/config/schema.ex` for the complete field definitions.

For the PRF host workflow, workspace granularity, pending candidate qualification, and recovery
steps, see the [PRF Symphony operator runbook](docs/prf-automation-runbook.md). Staging qualification
requires an operator to choose a compatible batch; promotion from staging to integration remains a
separate guarded repository operation. For an assembled candidate, pin a review manifest with
`scripts/symphony_integration_review.py prepare`, then run the isolated
`./symphony integration-reviewer` lane using the `symphony-integration` and
`symphony-integration-review` labels. Its host hooks require exact-tree aggregate validation
evidence, then run the `prf-dev integration preflight/promote/verify` gate after independent
approval. Promotion advances only local `integration`; publishing `main` remains separate.

For PRF lane launches, `./symphony` validates the selected workflow's project or exact issue scope,
state, routing labels, workspace mount, and host tools before launch. It holds a `flock` lock for the lane's workspace root
so a second systemd, tmux, or manual process cannot own the same lane. PRF workflows retain terminal
workspaces; clear `.symphony-quarantined` only after reconciling the workspace and passing its
eligibility preflight. Use `scripts/symphony_eligibility.py clear-quarantine --issue ISSUE
--candidate SHA` to bind the reconciled candidate, validate its published producer revision and
staging base, then clear the durable issue quarantine record and workspace markers; do not remove
markers directly. Quarantine is stored under the configured workspace root even when an attempt
fails before creating its per-issue workspace, and dispatch skips quarantined issues after restart.

The default coding and reviewer workflows use `excluded_project_slugs`: they accept
`symphony-pilot` issues from every Linear project except projects assigned to dedicated pro-rata,
protected-operations, researcher-challenges, and forensic workflows. Those dedicated workflows
remain restricted to their own project. Use either `project_slug`/`project_slugs` to allow specific
projects or `excluded_project_slugs` to accept every project except the listed ones. Use
`issue_identifiers` for a workflow bound to specific issue identifiers and
`excluded_issue_identifiers` to keep those issues out of a broader poll.

The local `coding-pro-106` role uses `WORKFLOW.pro-106.md` and a separate JJ workspace based on
`prf-dev`'s current revision. Run it directly with `./symphony coding-pro-106`. It is intentionally
excluded from the generic coding poll. This lane does not publish a candidate or update review
state; its repository has no configured Symphony staging bookmark or candidate handoff yet.

## Tracker adapters

- **Linear:** configure `provider.project_slugs` (or the legacy single `provider.project_slug`),
  `provider.excluded_project_slugs`, or exact `provider.issue_identifiers`, and `provider.api_key`;
  the key defaults to `LINEAR_API_KEY`.
  Optional assignee routing uses `LINEAR_ASSIGNEE`.
- **GitHub Issues:** configure `provider.repo` as `owner/repo`; token defaults to `GITHUB_TOKEN`.
  Set active and terminal states explicitly (`open` and `closed`). Pull requests are not dispatched.
- **Jira Cloud:** configure `provider.base_url`, `email`, `api_token`, and `project_key`; credentials
  default to `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN`.
- **Asana:** configure the provider workspace/project and token; token defaults to `ASANA_ACCESS_TOKEN`.
- **GitLab:** configure the provider project and token; token defaults to `GITLAB_TOKEN`.

Each adapter scopes issue reads to its configured projects or repository. Provider tools may still
access anything permitted by the configured credential.

## Development

```bash
mise exec -- mix test
mise exec -- mix specs.check
```

The Elixir-specific contributor notes are in [`AGENTS.md`](AGENTS.md). The scripts under `scripts/`
include eligibility checks, health inspection, and review/staging helpers; read their usage and the
corresponding workflow before enabling host-side actions.
