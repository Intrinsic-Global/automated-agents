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

if ! python3 "$eligibility_script" check --issue "$issue" --phase review; then
  printf 'review handoff rejected by eligibility for %s\n' "$issue" >&2
  exit 1
fi
