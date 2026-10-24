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

# Symphony hook adapter for an existing local jj repository. It intentionally
# creates only missing per-issue workspaces and never removes or resets one.
# Read-only checks use --ignore-working-copy; workspace add cannot, because it
# must update the newly created workspace's working copy.
# Usage: jj_workspace_adapter.sh create <workspace-path>

action=${1:-}
workspace=${2:-}
source_repo=${SYMPHONY_JJ_SOURCE_REPO:-}
source_revision=${SYMPHONY_JJ_SOURCE_REVISION:-@}
workspace_root=${SYMPHONY_JJ_WORKSPACE_ROOT:-}
eligibility_script=${SYMPHONY_ELIGIBILITY_SCRIPT:-$script_dir/symphony_eligibility.py}

fail() {
  printf 'jj workspace adapter: %s\n' "$*" >&2
  exit 1
}

eligibility_check() {
  local phase=$1
  local output
  if ! output=$(python3 "$eligibility_script" check --issue "$issue" --phase "$phase" 2>&1); then
    printf '%s\n' "$output" >&2
    fail "eligibility $phase rejected $issue"
  fi
}

record_workspace_base() {
  local base=$1
  shift || true
  python3 "$eligibility_script" record-workspace \
    --issue "$issue" \
    --workspace "$workspace" \
    --staging-base "$base" "$@" >/dev/null
}

check_expected_source() {
  [[ -f "$workspace/deps.edn" ]] || fail "expected Clojure repository is missing deps.edn: $workspace"
}

check_shared_repo() {
  [[ ! -L "$workspace/.jj" && ! -L "$workspace/.jj/repo" ]] ||     fail "workspace JJ metadata must not be symlinked: $workspace"
  [[ -f "$workspace/.jj/repo" ]] || fail "workspace is not a shared JJ workspace (missing .jj/repo): $workspace"
  [[ "$(cat "$workspace/.jj/repo")" != "repo-local" ]] ||     fail "workspace is an independent jj repo (repo-local); refusing to run an agent in it: $workspace"
  local ws_repo src_repo
  ws_repo=$(cd "$workspace/.jj" && cd "$(dirname "$(cat "$workspace/.jj/repo")")" 2>/dev/null && pwd -P) ||     fail "workspace repo pointer is unresolvable: $workspace"
  src_repo=$(cd "$source_repo/.jj" && cd "$(dirname "$(cat "$source_repo/.jj/repo")")" 2>/dev/null && pwd -P) ||     fail "source repo pointer is unresolvable: $source_repo"
  [[ "$ws_repo" == "$src_repo" ]] ||     fail "workspace points to a different JJ repo: $ws_repo (expected $src_repo)"
}

[[ "$action" == create ]] || fail "expected action 'create'"
[[ -n "$workspace" && "$workspace" == /* ]] || fail "workspace path must be absolute"
[[ -n "$source_repo" && "$source_repo" == /* ]] || fail "SYMPHONY_JJ_SOURCE_REPO must be absolute"
[[ -n "$workspace_root" && "$workspace_root" == /* ]] || fail "SYMPHONY_JJ_WORKSPACE_ROOT must be absolute"
command -v jj >/dev/null || fail "jj is required"

source_repo=$(cd "$source_repo" 2>/dev/null && pwd -P) || fail "source repo is unavailable"
workspace_root=$(mkdir -p "$workspace_root" && cd "$workspace_root" && pwd -P)
parent=$(dirname "$workspace")
mkdir -p "$parent"
parent=$(cd "$parent" && pwd -P)
workspace="$parent/$(basename "$workspace")"
issue=$(basename "$workspace")
cd "$parent"

case "$workspace/" in
  "$workspace_root/"*) ;;
  *) fail "workspace is outside configured workspace root: $workspace" ;;
esac

if [[ -e "$workspace" || -L "$workspace" ]]; then
  [[ ! -L "$workspace" ]] || fail "refusing symlink workspace path: $workspace"
  [[ -d "$workspace" ]] || fail "workspace path exists and is not a directory: $workspace"
  resolved_workspace=$(cd "$workspace" 2>/dev/null && pwd -P) || fail "workspace path cannot be resolved: $workspace"
  case "$resolved_workspace/" in
    "$workspace_root/"*) ;;
    *) fail "resolved workspace is outside configured workspace root: $resolved_workspace" ;;
  esac
  if [[ -e "$workspace/.jj" ]]; then
    jj --ignore-working-copy -R "$workspace" root >/dev/null 2>&1 || fail "existing .jj data is not a usable jj workspace: $workspace"
    check_expected_source
    check_shared_repo
    existing_base=$(jj --ignore-working-copy -R "$workspace" log -r '@-' --no-graph -T commit_id)
    record_workspace_base "$existing_base"
    eligibility_check resume
    exit 0
  fi
  [[ -z "$(find "$workspace" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail "refusing to initialize non-empty existing path: $workspace"
  rmdir "$workspace"
fi

eligibility_check dispatch
staging_base=$(jj --ignore-working-copy -R "$source_repo" log -r "$source_revision" --no-graph -T commit_id)
[[ "$staging_base" =~ ^[0-9a-f]{40}$ ]] || fail "could not resolve a pinned staging base from $source_revision"
workspace_name="symphony-$(basename "$workspace_root")-$(basename "$workspace")"
jj -R "$source_repo" workspace add "$workspace" --name "$workspace_name" --revision "$staging_base" --quiet
check_expected_source
check_shared_repo
record_workspace_base "$staging_base" --fresh

printf 'Created local jj workspace %s from %s\n' "$workspace" "$source_repo"
