#!/usr/bin/env bash
# Spec boundary check: "Vyavasay" and "GST" may appear only inside the Vyavasay
# adapter (and its tests), so the core stays independent of any one accounting system.
# docs/ is excluded because the spec itself talks about both, and evals/datasets/
# because it holds customers' own words, which may mention tax.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

hits=$(git grep -n -I -E 'GST|[Vv]yavasay|VYAVASAY' -- . \
  ':!docs/' \
  ':!evals/datasets/' \
  ':!parley/adapters/accounting/vyavasay/' \
  ':!tests/adapters/vyavasay/' \
  ':!scripts/check_boundary.sh' || true)

if [[ -n "$hits" ]]; then
  echo "Boundary check failed. These lines belong in parley/adapters/accounting/vyavasay/:"
  echo "$hits"
  exit 1
fi
echo "Boundary check passed."
