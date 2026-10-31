#!/usr/bin/env bash
# Layer B (Phase 3H-R1): run the test suites in a container with no network
# interface other than loopback and without production secrets.
#
# Preparation (network allowed): copy the committed HEAD into a fresh
# directory (untracked files such as .env are never included), build the test
# image there and install dependencies.
# Execution (network denied): `docker run --network none` with a read-only
# root filesystem, an unprivileged user, no capabilities, no volume mounts, no
# Docker socket and only explicit dummy credentials. The container runs an
# isolation self-check first and refuses to run tests if it fails.
#
# Usage: run_isolated_tests.sh [COMMAND...]
#   Without arguments every suite runs; with arguments only COMMAND runs (in
#   the same isolated container, after the same self-check).
#
# There is deliberately no fallback: if Docker is unavailable or any step
# fails, this script fails. Only committed changes are tested.
set -euo pipefail

IMAGE="${LAYER_B_IMAGE:-mtg-notion-manager-isolated-tests:local}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required for the isolated test run; refusing to run tests without network isolation." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: the Docker daemon is not reachable; refusing to run tests without network isolation." >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- Preparation: clean source of the committed HEAD -------------------------
SRC="$WORK/src"
git init -q "$SRC"
git -C "$SRC" fetch -q --depth=1 "file://$REPO_ROOT" "$HEAD_SHA"
git -C "$SRC" checkout -q --detach FETCH_HEAD
if find "$SRC" -name .env -not -path "$SRC/.git/*" | grep -q .; then
  echo "ERROR: a .env file is part of the committed tree; refusing to build." >&2
  exit 2
fi
echo "Isolated test source: $HEAD_SHA (committed HEAD only)"

docker build --quiet --platform linux/amd64 -f "$SRC/ci/isolated-tests/Dockerfile" -t "$IMAGE" "$SRC"

# --- Execution: no network, no secrets ---------------------------------------
# docker run does not inherit the host environment; only the -e values below
# are present in the container.
docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=1g \
  --tmpfs /work:rw,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=0700 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user 10001:10001 \
  --pids-limit 512 \
  -e HOME=/tmp/home \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e "PYTEST_ADDOPTS=-p no:cacheprovider" \
  -e NOTION_API_KEY=test-dummy-notion-api-key-not-a-real-secret \
  -e NOTION_COMMANDER_DATA_SOURCE_ID=test-dummy-commander-data-source-id \
  -e NOTION_CARD_DATA_SOURCE_ID=test-dummy-card-data-source-id \
  "$IMAGE" sh ci/isolated-tests/run_in_container.sh "$@"
