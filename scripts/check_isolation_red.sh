#!/usr/bin/env bash
# Layer B (Phase 3H-R1) red checks: prove the in-container isolation self-check
# (ci/isolated-tests/verify_isolation.py) and the runner fail closed when an
# isolation property is missing. Each case removes exactly one property and
# MUST fail; if any case passes, this script fails.
#
# Run after scripts/run_isolated_tests.sh built the image. No case sends a
# packet to an external network: with a network interface present the
# self-check stops at the structural check before any probe, and all other
# cases run with --network none. Only dummy credentials are used.
set -uo pipefail

IMAGE="${LAYER_B_IMAGE:-mtg-notion-manager-isolated-tests:local}"
DUMMY_ENV=(
  -e HOME=/tmp/home
  -e NOTION_API_KEY=test-dummy-notion-api-key-not-a-real-secret
  -e NOTION_COMMANDER_DATA_SOURCE_ID=test-dummy-commander-data-source-id
  -e NOTION_CARD_DATA_SOURCE_ID=test-dummy-card-data-source-id
)
HARDENED=(
  --rm --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m
  --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001
)
VERIFY=(python ci/isolated-tests/verify_isolation.py /app)
failures=0

expect_failure() {
  local name="$1" expected="$2"
  shift 2
  local output rc
  output="$("$@" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "RED CHECK FAILED: '$name' passed but must fail" >&2
    failures=$((failures + 1))
  elif ! grep -q -- "$expected" <<<"$output"; then
    echo "RED CHECK FAILED: '$name' failed for an unexpected reason:" >&2
    echo "$output" | tail -5 >&2
    failures=$((failures + 1))
  else
    echo "ok: '$name' failed closed ($expected)"
  fi
  if [ "$name" = "network interface present" ] && grep -q "^ok: _socket" <<<"$output"; then
    echo "RED CHECK FAILED: probes ran although a network interface was present" >&2
    failures=$((failures + 1))
  fi
}

expect_failure "network interface present" "STRUCTURAL CHECK FAILED" \
  docker run --network bridge "${HARDENED[@]}" "${DUMMY_ENV[@]}" "$IMAGE" "${VERIFY[@]}"

expect_failure "non-dummy credential" "CREDENTIAL CHECK FAILED" \
  docker run --network none "${HARDENED[@]}" "${DUMMY_ENV[@]}" \
  -e NOTION_API_KEY=not-the-test-dummy "$IMAGE" "${VERIFY[@]}"

expect_failure "secret-like environment variable" "unexpected secret-like environment variables" \
  docker run --network none "${HARDENED[@]}" "${DUMMY_ENV[@]}" \
  -e GITHUB_TOKEN=dummy-for-red-check "$IMAGE" "${VERIFY[@]}"

DOTENV_FILE="$(mktemp)"
trap 'rm -f "$DOTENV_FILE"' EXIT
echo "DUMMY_RED_CHECK=1" >"$DOTENV_FILE"
expect_failure ".env present" "CREDENTIAL CHECK FAILED: .env present" \
  docker run --network none "${HARDENED[@]}" "${DUMMY_ENV[@]}" \
  -v "$DOTENV_FILE:/app/.env:ro" "$IMAGE" "${VERIFY[@]}"

expect_failure "running as root" "PRIVILEGE CHECK FAILED: running as root" \
  docker run --network none --rm --read-only --tmpfs /tmp:rw,size=64m --cap-drop ALL \
  --security-opt no-new-privileges:true --user 0:0 "${DUMMY_ENV[@]}" "$IMAGE" "${VERIFY[@]}"

expect_failure "writable application directory" "FILESYSTEM CHECK FAILED" \
  docker run --network none --rm --tmpfs /tmp:rw,size=64m --cap-drop ALL \
  --security-opt no-new-privileges:true --user 10001:10001 "${DUMMY_ENV[@]}" \
  --mount "type=tmpfs,destination=/app/writable-probe-dir" \
  -w /app "$IMAGE" python ci/isolated-tests/verify_isolation.py /app/writable-probe-dir

expect_failure "docker socket mounted" "PRIVILEGE CHECK FAILED" \
  docker run --network none "${HARDENED[@]}" "${DUMMY_ENV[@]}" \
  --mount "type=tmpfs,destination=/var/run/docker.sock" "$IMAGE" "${VERIFY[@]}"

expect_failure "docker unavailable (no fallback)" "docker is required" \
  env PATH=/nonexistent /bin/bash scripts/run_isolated_tests.sh

if [ "$failures" -ne 0 ]; then
  echo "LAYER B RED CHECKS FAILED: $failures" >&2
  exit 1
fi
echo "LAYER B RED CHECKS PASSED"
