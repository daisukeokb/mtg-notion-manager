# Layer B (Phase 3H-R1): image for running the test suites with no network
# and no production secrets.
#
# Build ONLY from the clean source tree prepared by scripts/run_isolated_tests.sh
# (a fresh checkout of the committed HEAD — untracked files such as .env can
# never be in the build context). Dependencies are installed here, during the
# preparation phase; the test run itself happens with `docker run --network none`
# and must not fetch anything.

# Pinned by digest for reproducibility (python:3.9-slim-bookworm).
FROM python:3.9-slim-bookworm@sha256:a02e9c5406c416c504d6c9a1a306ff4080c3173f1008d192f953bd20382a2d5c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: one existing test runs `git ls-files`; the isolation self-check also uses
# git (libcurl) as a native, non-Python network probe.
# The repository is owned by root but read by the unprivileged test user, so
# mark it as a safe directory system-wide.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /app \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin tester

WORKDIR /app
COPY . /app

# Defense in depth: the build context must never contain a .env file.
RUN if find /app -name .env -not -path '/app/.git/*' | grep -q .; then \
        echo "ERROR: .env found in the build context" >&2; exit 1; \
    fi \
    && pip install -e ".[dev]" -e tools/generation_workspace

USER 10001:10001
CMD ["sh", "ci/isolated-tests/run_in_container.sh"]
