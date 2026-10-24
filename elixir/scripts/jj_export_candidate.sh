#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
env_file=${SYMPHONY_ENV_FILE:-$script_dir/../.env}
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

# Export the reviewed candidate bookmark from the shared JJ repo into the Git
# producer checkout expected by the reviewer and staging gate.
# Usage: jj_export_candidate.sh <issue-identifier>

issue=${1:?usage: jj_export_candidate.sh <issue-identifier>}
bookmark="symphony/$issue"
jj_workspace=${SYMPHONY_JJ_SOURCE_REPO:-}
producer_root=${SYMPHONY_PRODUCER_ROOT:-${SYMPHONY_WORKSPACES_ROOT:-$HOME/.local/share/symphony/workspaces}/producers}

[[ -n "$jj_workspace" && -d "$jj_workspace/.jj" ]] || {
  printf 'jj export: source workspace is unavailable: %s\n' "$jj_workspace" >&2
  exit 1
}

if [[ -f "$jj_workspace/.jj/repo" ]]; then
  repo_pointer=$(cat "$jj_workspace/.jj/repo")
  [[ -n "$repo_pointer" ]] || {
    printf 'jj export: missing repo pointer in %s/.jj/repo\n' "$jj_workspace" >&2
    exit 1
  }
  git_repo=$(cd "$jj_workspace/.jj" && cd "$(dirname "$repo_pointer")/.." && pwd -P)
elif [[ -d "$jj_workspace/.jj/repo" && -d "$jj_workspace/.git" ]]; then
  # Colocated repo-local JJ workspace: the Git object store is the workspace.
  git_repo=$(cd "$jj_workspace" && pwd -P)
else
  printf 'jj export: no shared or colocated git store for %s\n' "$jj_workspace" >&2
  exit 1
fi
[[ -d "$git_repo/.git" ]] || {
  printf 'jj export: shared git store is unavailable: %s\n' "$git_repo" >&2
  exit 1
}

if ! commit=$(jj --ignore-working-copy -R "$jj_workspace" log -r "$bookmark" --no-graph -T commit_id 2>/dev/null); then
  if [[ -f "$PWD/.jj/repo" && "$(cat "$PWD/.jj/repo")" == "repo-local" ]]; then
    printf 'jj export: candidate workspace is an isolated repo-local jj repo; the shared bookmark %s is invisible\n' "$bookmark" >&2
    exit 1
  fi
  printf 'jj export: no %s bookmark; nothing to export\n' "$bookmark" >&2
  exit 0
fi
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || {
  printf 'jj export: invalid commit for %s: %s\n' "$bookmark" "$commit" >&2
  exit 1
}

producer="$producer_root/$issue"
mkdir -p "$producer"
[[ -d "$producer/.git" ]] || git init -q "$producer"

git -C "$producer" fetch --no-tags "$git_repo" "+$commit:refs/heads/$bookmark"
exported=$(git -C "$producer" rev-parse "refs/heads/$bookmark")
[[ "$exported" == "$commit" ]] || {
  printf 'jj export: producer branch mismatch: %s != %s\n' "$exported" "$commit" >&2
  exit 1
}

eligibility_script=${SYMPHONY_ELIGIBILITY_SCRIPT:-$script_dir/symphony_eligibility.py}
python3 "$eligibility_script" record-candidate --issue "$issue" --candidate "$commit" >/dev/null

printf 'Exported %s -> %s (%s)\n' "$bookmark" "$producer" "$commit"
