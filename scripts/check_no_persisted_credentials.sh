#!/usr/bin/env bash
# Phase 3H-R2: after actions/checkout, fail if any Git credential was persisted
# in the repository configuration (http.<url>.extraheader, credential helpers,
# includeIf-included credential files). Prints configuration KEY NAMES only,
# never values.
set -euo pipefail

found="$(git config --local --list --name-only | grep -Ei 'extraheader|^credential\.|^includeif\.' || true)"
if [ -n "$found" ]; then
  echo "ERROR: credentials persisted in .git/config (keys only):" >&2
  echo "$found" >&2
  exit 1
fi
echo "ok: no persisted Git credentials in .git/config"
