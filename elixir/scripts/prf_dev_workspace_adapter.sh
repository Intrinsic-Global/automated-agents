#!/usr/bin/env bash
set -euo pipefail

action=${1:-}
workspace=${2:-}
issue=$(basename "${workspace:-}")
source_repo=${SYMPHONY_JJ_SOURCE_REPO:-}
workspace_root=${SYMPHONY_JJ_WORKSPACE_ROOT:-}
source_revision=${SYMPHONY_JJ_SOURCE_REVISION:-@-}
eligibility_script=${SYMPHONY_ELIGIBILITY_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/symphony_eligibility.py}

fail() {
  printf 'prf-dev workspace adapter: %s\n' "$*" >&2
  exit 1
}

eligibility_check() {
  local phase=$1 output
  if ! output=$(python3 "$eligibility_script" check --issue "$issue" --phase "$phase" 2>&1); then
    printf '%s\n' "$output" >&2
    fail "eligibility $phase rejected $issue"
  fi
}

record_workspace_base() {
  local base=$1 fresh=${2:-}
  local -a args=(--issue "$issue" --workspace "$workspace" --staging-base "$base")
  [[ "$fresh" == fresh ]] && args+=(--fresh)
  python3 "$eligibility_script" record-workspace "${args[@]}" >/dev/null
}

[[ "$action" == create ]] || fail "expected action 'create'"
[[ "$issue" == PRO-106 ]] || fail "this workflow only accepts PRO-106"
[[ -n "$workspace" && "$workspace" == /* ]] || fail 'workspace path must be absolute'
[[ -n "$source_repo" && "$source_repo" == /* ]] || fail 'SYMPHONY_JJ_SOURCE_REPO must be absolute'
[[ -n "$workspace_root" && "$workspace_root" == /* ]] || fail 'SYMPHONY_JJ_WORKSPACE_ROOT must be absolute'
command -v jj >/dev/null || fail 'jj is required'

source_repo=$(cd "$source_repo" 2>/dev/null && pwd -P) || fail "source repository is unavailable: $source_repo"
[[ -f "$source_repo/pyproject.toml" ]] || fail "expected prf-dev pyproject.toml is missing: $source_repo"
[[ -f "$source_repo/src/prf_dev/integration.py" ]] || fail "Python JJ integration guard is missing: $source_repo"
[[ ! -L "$source_repo/.jj" && ! -L "$source_repo/.jj/repo" ]] || fail 'source JJ metadata must not be symlinked'
jj --ignore-working-copy -R "$source_repo" root >/dev/null 2>&1 || fail "source is not a usable JJ repository: $source_repo"
remote=$(jj --ignore-working-copy -R "$source_repo" git remote list | awk '$1 == "origin" { print $2 }')
[[ "$remote" == https://github.com/protocol-robustness/prf-dev.git ]] || fail "unexpected prf-dev origin: ${remote:-missing}"

workspace_root=$(mkdir -p "$workspace_root" && cd "$workspace_root" && pwd -P)
parent=$(dirname "$workspace")
mkdir -p "$parent"
parent=$(cd "$parent" && pwd -P)
workspace="$parent/$(basename "$workspace")"
case "$workspace/" in
  "$workspace_root/"*) ;;
  *) fail "workspace is outside configured workspace root: $workspace" ;;
esac

if [[ -e "$workspace" || -L "$workspace" ]]; then
  [[ ! -L "$workspace" && -d "$workspace" ]] || fail "refusing unsafe existing workspace path: $workspace"
  resolved_workspace=$(cd "$workspace" && pwd -P) || fail "workspace path cannot be resolved: $workspace"
  [[ "$resolved_workspace" == "$workspace" ]] || fail "workspace path resolves elsewhere: $resolved_workspace"
  if [[ -e "$workspace/.jj" ]]; then
    [[ ! -L "$workspace/.jj" && ! -L "$workspace/.jj/repo" ]] || fail 'workspace JJ metadata must not be symlinked'
    jj --ignore-working-copy -R "$workspace" root >/dev/null 2>&1 || fail "existing workspace has unusable JJ data: $workspace"
    [[ -f "$workspace/src/prf_dev/integration.py" ]] || fail "existing workspace is not based on prf-dev: $workspace"
    [[ -f "$workspace/.jj/repo" ]] || fail "existing workspace is not linked to the source JJ repository: $workspace"
    if [[ -d "$source_repo/.jj/repo" ]]; then
      source_store=$(cd "$source_repo/.jj/repo" && pwd -P) || fail 'source JJ repository path is invalid'
    elif [[ -f "$source_repo/.jj/repo" ]]; then
      source_store=$(cd "$source_repo/.jj" && cd "$(dirname "$(cat "$source_repo/.jj/repo")")" && pwd -P) || fail 'source JJ repository pointer is invalid'
    else
      fail 'source JJ repository metadata is missing'
    fi
    if [[ -d "$workspace/.jj/repo" ]]; then
      workspace_store=$(cd "$workspace/.jj/repo" && pwd -P) || fail 'workspace JJ repository path is invalid'
    elif [[ -f "$workspace/.jj/repo" ]]; then
      [[ "$(cat "$workspace/.jj/repo")" != repo-local ]] || fail 'workspace uses an independent JJ repository'
      workspace_store=$(cd "$workspace/.jj" && cd "$(cat "$workspace/.jj/repo")" && pwd -P) || fail 'workspace JJ repository pointer is invalid'
    else
      fail 'workspace JJ repository metadata is missing'
    fi
    [[ "$source_store" == "$workspace_store" ]] || fail "workspace belongs to a different JJ repository: $workspace"
    existing_base=$(jj --ignore-working-copy -R "$workspace" log -r '@-' --no-graph -T commit_id)
    record_workspace_base "$existing_base"
    eligibility_check resume
    exit 0
  fi
  [[ -z "$(find "$workspace" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail "refusing to initialize non-empty path: $workspace"
  rmdir "$workspace"
fi

eligibility_check dispatch
base=$(jj --ignore-working-copy -R "$source_repo" log --no-graph -r "$source_revision" -T commit_id) || fail "could not resolve prf-dev base revision $source_revision"
[[ "$base" =~ ^[0-9a-f]{40}$ ]] || fail "invalid prf-dev base commit: $base"
workspace_name="symphony-prf-dev-$issue"
jj -R "$source_repo" workspace add "$workspace" --name "$workspace_name" --revision "$base" --quiet
[[ -f "$workspace/src/prf_dev/integration.py" ]] || fail "created workspace is missing the Python JJ integration guard: $workspace"
record_workspace_base "$base" fresh
eligibility_check resume
printf 'Created local prf-dev JJ workspace %s from %s at %s\n' "$workspace" "$source_repo" "$base"
