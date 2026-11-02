#!/usr/bin/env bash
# Phase 3H-R3 (D-1): replace the pip that setup-python's installer upgraded on
# the runner (unpinned, not hash-checked) with the pip pinned in
# requirements/ci-pip-lock.txt, before any dependency is installed.
#
# The wheel is downloaded over HTTPS only and its sha256 is verified with
# coreutils before any of its code runs. It is then installed by running pip
# from inside that verified wheel (offline, --require-hashes), so the
# unverified pip on the runner is never executed again.
#
# Boundary (not covered here): the interpreter that setup-python downloads and
# the pip its installer ran once during setup. See README.
#
# Every step checks its own result; `set -e` is not relied on for fail-closed
# behaviour.
set -uo pipefail

LOCK=requirements/ci-pip-lock.txt

fail() {
  echo "PIP BOOTSTRAP FAILED: $*" >&2
  exit 1
}

[ -f "$LOCK" ] || fail "$LOCK not found"
entries="$(grep -Ev '^[[:space:]]*(#|$)' "$LOCK")" || fail "cannot read $LOCK"
[ "$(printf '%s\n' "$entries" | wc -l | tr -d ' ')" = 2 ] || fail "$LOCK must contain exactly one entry"
version="$(printf '%s\n' "$entries" | sed -n 's/^pip==\([0-9][0-9.]*\) \\$/\1/p')"
digest="$(printf '%s\n' "$entries" | sed -n 's/^ *--hash=sha256:\([0-9a-f]\{64\}\)$/\1/p')"
[ -n "$version" ] && [ "$(printf '%s\n' "$version" | wc -l | tr -d ' ')" = 1 ] \
  || fail "no single pinned pip version in $LOCK"
[ -n "$digest" ] && [ "$(printf '%s\n' "$digest" | wc -l | tr -d ' ')" = 1 ] \
  || fail "no single sha256 in $LOCK"

work="$(mktemp -d)" || fail "mktemp failed"
trap 'rm -rf "$work"' EXIT
wheel="$work/pip-${version}-py3-none-any.whl"

curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --tlsv1.2 --retry 3 --output "$wheel" \
  "https://files.pythonhosted.org/packages/py3/p/pip/pip-${version}-py3-none-any.whl" \
  || fail "download of pip ${version} failed"
printf '%s  %s\n' "$digest" "$wheel" | sha256sum --check --strict --quiet \
  || fail "sha256 of the downloaded pip ${version} wheel does not match $LOCK"

python -I "$wheel/pip" install --require-hashes --no-deps --no-index --only-binary=:all: --force-reinstall --find-links "$work" -r requirements/ci-pip-lock.txt \
  || fail "installing the verified pip ${version} failed"

installed="$(python -I -m pip --version)" || fail "python -I -m pip --version failed"
case "$installed" in
  "pip ${version} from "*) echo "ok: hash-verified $installed" ;;
  *) fail "unexpected pip after bootstrap: $installed" ;;
esac
