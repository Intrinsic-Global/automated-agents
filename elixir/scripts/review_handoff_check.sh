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

issue=$(basename "$PWD")
eligibility_script=${SYMPHONY_ELIGIBILITY_SCRIPT:-$script_dir/symphony_eligibility.py}

set +e
python3 "$eligibility_script" check --issue "$issue" --phase review
status=$?
set -e
if [[ $status -eq 75 ]]; then
  printf 'review already claimed or completed for this candidate; skipping Codex for %s\n' "$issue" >&2
  exit 75
elif [[ $status -ne 0 ]]; then
  printf 'review handoff rejected by eligibility for %s\n' "$issue" >&2
  exit "$status"
fi

set +e
python3 "$eligibility_script" claim-review --issue "$issue"
status=$?
set -e
if [[ $status -eq 75 ]]; then
  printf 'review identity was claimed concurrently; skipping Codex for %s\n' "$issue" >&2
  exit 75
elif [[ $status -ne 0 ]]; then
  printf 'could not claim review identity for %s\n' "$issue" >&2
  exit "$status"
fi
