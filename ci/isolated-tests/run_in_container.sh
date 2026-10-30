#!/bin/sh
# Layer B (Phase 3H-R1): entrypoint inside the network-less test container.
# The isolation self-check must pass before anything else runs (fail-closed).
#
# Without arguments: run every suite (the required `test` CI check).
# With arguments: run exactly that command, still only after the self-check
# (used by the generation-workspace CI job).
set -eu
cd /app

python ci/isolated-tests/verify_isolation.py /app

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# No cache/bytecode is written: the application directory is read-only, only
# /tmp is writable, and PYTEST_ADDOPTS disables the pytest cache provider.
pytest
pytest tools/generation_workspace/tests
pytest tools/generation_workspace/tests --collect-only -q
generation-workspace --help >/dev/null
python -m generation_workspace --help >/dev/null
echo "LAYER B TEST RUN PASSED"
