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
    if [[ -f ./review-decision.json ]]; then
      archive_root="$SYMPHONY_WORKSPACES_ROOT/.symphony-review-decisions/$issue"
      mkdir -p "$archive_root"
      mv ./review-decision.json "$archive_root/review-decision.$(date -u +%Y%m%dT%H%M%SZ).$$.json"
    fi
    [[ -z "$(git status --porcelain --untracked-files=all)" ]] || {
      printf 'repo-scoped reviewer: workspace has uncommitted changes; preserving it for reconciliation: %s\n' "$workspace" >&2
      exit 1
    }
    src="$producer_root/$issue"
    if git -C "$src" rev-parse --verify "refs/heads/symphony/$issue" >/dev/null 2>&1; then
      git fetch --no-tags "$src" "+refs/heads/symphony/$issue:refs/remotes/producer/$issue"
      git checkout -q --detach "refs/remotes/producer/$issue"
    else
      printf 'repo-scoped reviewer: no producer candidate for %s at %s\n' "$issue" "$src" >&2
    fi
    eligibility_script=${SYMPHONY_ELIGIBILITY_SCRIPT:-$script_dir/symphony_eligibility.py}
    set +e
    python3 "$eligibility_script" check --issue "$issue" --phase review
    status=$?
    set -e
    if [[ $status -eq 75 ]]; then
      printf 'repo-scoped reviewer: review already attempted for %s; skipping Codex\n' "$issue" >&2
      exit 75
    elif [[ $status -ne 0 ]]; then
      printf 'repo-scoped reviewer: eligibility review handoff rejected for %s\n' "$issue" >&2
      exit "$status"
    fi
    set +e
    python3 "$eligibility_script" claim-review --issue "$issue"
    status=$?
    set -e
    if [[ $status -eq 75 ]]; then
      printf 'repo-scoped reviewer: concurrent review claim for %s; skipping Codex\n' "$issue" >&2
      exit 75
    elif [[ $status -ne 0 ]]; then
      printf 'repo-scoped reviewer: could not claim review identity for %s\n' "$issue" >&2
      exit "$status"
    fi
    ;;
  *)
    printf 'unknown action: %s\n' "$action" >&2
    exit 2
    ;;
esac
