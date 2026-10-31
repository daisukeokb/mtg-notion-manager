#!/usr/bin/env bash
# Phase 3H-R2: after actions/checkout, fail if any Git credential was persisted:
# http.<url>.extraheader, credential helpers, includeIf-included files (local,
# global and system configuration) or credentials embedded in a remote URL.
# Prints configuration KEY NAMES only, never values. Fails closed: any error
# reading the configuration is a failure, not "no credentials".
set -euo pipefail

git rev-parse --git-dir >/dev/null  # must be inside a repository

found=""
for scope in --local --global --system; do
  status=0
  errors="$(mktemp)"
  names="$(git config "$scope" --list --name-only 2>"$errors")" || status=$?
  # Only a missing global/system file counts as "no entries"; any other error fails.
  if [ "$status" -ne 0 ]; then
    if [ "$scope" != --local ] && grep -q "No such file or directory" "$errors"; then
      names=""
    else
      echo "ERROR: cannot read git config $scope (exit $status)" >&2
      rm -f "$errors"
      exit 1
    fi
  fi
  rm -f "$errors"
  matches="$(grep -Ei 'extraheader|^credential\.|^includeif\.' <<<"$names" || true)"
  if [ -n "$matches" ]; then
    found="$found"$'\n'"$scope: $matches"
  fi
done

# Remote URLs must not carry user info (https://user:token@host/...).
while IFS= read -r name; do
  [ -n "$name" ] || continue
  if git config --local --get "$name" | grep -Eq '^[a-z][a-z0-9+.-]*://[^/@]+@'; then
    found="$found"$'\n'"--local: $name (credentials in URL)"
  fi
done < <(git config --local --list --name-only | grep -Ei '^remote\..*\.(url|pushurl)$' || true)

if [ -n "$found" ]; then
  echo "ERROR: persisted Git credentials found (keys only):$found" >&2
  exit 1
fi
echo "ok: no persisted Git credentials in git config (local/global/system) or remote URLs"
