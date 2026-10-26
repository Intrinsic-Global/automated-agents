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

decision=${1:?usage: review_decision_record.sh <review-decision.json>}
issue=$(basename "$PWD")
eligibility_script=${SYMPHONY_ELIGIBILITY_SCRIPT:-$script_dir/symphony_eligibility.py}

python3 "$eligibility_script" record-review --issue "$issue" --decision "$decision" >/dev/null
