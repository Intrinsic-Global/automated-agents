---
tracker:
  kind: linear
  provider:
    project_slug: "your-linear-project-slug"
    api_key: $LINEAR_API_KEY
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
    - Canceled
workspace:
  root: ~/code/symphony-workspaces
hooks:
  after_create: |
    git clone https://github.com/your-org/your-repository.git .
agent:
  max_concurrent_agents: 2
  max_turns: 10
codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
    networkAccess: false
---

Work on issue {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

Follow the repository's contribution instructions. Run the relevant checks and report what changed,
what you verified, and any remaining limitations.
