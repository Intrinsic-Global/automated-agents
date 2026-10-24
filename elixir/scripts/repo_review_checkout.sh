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

# Repo-scoped reviewer checkout. Usage:
#   repo_review_checkout.sh init   <workspace>
#   repo_review_checkout.sh before <workspace>
# The issue identifier is the workspace directory name.

action=${1:?usage: repo_review_checkout.sh <init|before> <workspace>}
workspace=${2:?usage: repo_review_checkout.sh <init|before> <workspace>}
config=${SYMPHONY_REPO_CONFIG:-$script_dir/../symphony-repos.json}
issue=$(basename "$workspace")
query=(python3 "$script_dir/repo_config.py" --config "$config" --issue "$issue")

remote=$("${query[@]}" --field remote)
producer_root=$("${query[@]}" --field producer_root)

case "$action" in
  init)
    cd "$workspace"
    git init -q .
    git remote add origin "$remote"
    git fetch --no-tags origin main
    git checkout -q --detach FETCH_HEAD
    test "$(git rev-parse --is-inside-work-tree)" = true
    ;;
  before)
    cd "$workspace"
    test "$(git rev-parse --is-inside-work-tree)" = true
    rm -f ./review-decision.json
    src="$producer_root/$issue"
    if git -C "$src" rev-parse --verify "refs/heads/symphony/$issue" >/dev/null 2>&1; then
      git fetch --no-tags "$src" "+refs/heads/symphony/$issue:refs/remotes/producer/$issue"
      git checkout -q --detach "refs/remotes/producer/$issue"
      git reset -q --hard "refs/remotes/producer/$issue"
    else
      printf 'repo-scoped reviewer: no producer candidate for %s at %s\n' "$issue" "$src" >&2
    fi
    state_dir=${SYMPHONY_STATE_DIR:-$SYMPHONY_WORKSPACES_ROOT/.symphony-state}
    eligibility_script=${SYMPHONY_ELIGIBILITY_SCRIPT:-$script_dir/symphony_eligibility.py}
    if [[ -f "$state_dir/issues/$issue.json" ]]; then
      if ! python3 "$eligibility_script" check --issue "$issue" --phase review; then
        printf 'repo-scoped reviewer: eligibility review handoff rejected for %s\n' "$issue" >&2
        exit 1
      fi
    else
      printf 'repo-scoped reviewer: no eligibility state for %s; legacy handoff allowed (run workspace adapter to record it)\n' "$issue" >&2
    fi
    ;;
  *)
    printf 'unknown action: %s\n' "$action" >&2
    exit 2
    ;;
esac
