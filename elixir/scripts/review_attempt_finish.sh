#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
issue=$(basename "$PWD")
if [[ -f ./review-decision.json ]]; then
  outcome=decision
  reason='review decision recorded'
else
  outcome=inconclusive
  reason='review run ended without a decision; manual reconciliation required'
fi

python3 "$script_dir/symphony_eligibility.py" finish-review \
  --issue "$issue" --outcome "$outcome" --reason "$reason"
