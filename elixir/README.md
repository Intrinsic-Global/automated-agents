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
codex:
  command: codex app-server
```

Tracker tools run host-side with configured credentials; Symphony removes declared tracker credentials
from the Codex child environment. Raw provider tools retain the access granted to the configured
credential, so use credentials with appropriate scope.

Eligibility checks use `scripts/symphony_eligibility.py` and `symphony-repos.json` by default. Set
`SYMPHONY_REPO_CONFIG` to select another config file. The checked-in example config is intentionally
absent: create one for your environment using the schema described by the scripts and workflows.
Admission can be disabled with `SYMPHONY_ELIGIBILITY_ENFORCE=0`; only do so after reviewing how that
changes your workflow's checks.

The schema supports tracker selection, workspace paths, hooks, concurrency and retry limits, Codex
policy, logging, and optional server settings. See `../SPEC.md` and
`lib/symphony_elixir/config/schema.ex` for the complete field definitions.

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
