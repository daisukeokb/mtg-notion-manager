# Layer B (Phase 3H-R1): image for running the test suites with no network
# and no production secrets.
#
# Build ONLY from the clean source tree prepared by scripts/run_isolated_tests.sh
# (a fresh checkout of the committed HEAD — untracked files such as .env can
# never be in the build context). Dependencies are installed here, during the
# preparation phase; the test run itself happens with `docker run --network none`
# and must not fetch anything.

# Pinned by digest (Phase 3H-R2; refreshed in Phase 3H-R4). Docker verifies the
# pulled content against this digest, so a re-pushed tag cannot substitute the
# image.
#   image:     docker.io/library/python:3.14.10-slim-trixie (Python 3.14.10)
#   os:        Debian GNU/Linux 13 (trixie), 13.7
#   platform:  linux/amd64 (the GitHub-hosted ubuntu runners); the build is run
#              with --platform linux/amd64
#   digest:    linux/amd64 image manifest (application/vnd.oci.image.manifest.v1+json)
#   index:     sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
#              (OCI image index of the tag, which lists this manifest for linux/amd64)
#   verified:  2026-11-02 against registry-1.docker.io (sha256 of the index,
#              manifest, config and layer bodies; PYTHON_VERSION in the config)
# Update procedure: README "依存関係・Action・ベースイメージの更新手順".
FROM python:3.14.10-slim-trixie@sha256:ec6a14df5b63e3fbb3498e3240f7f640c230f9dca67683d23265db3866697f2d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: one existing test runs `git ls-files`; the isolation self-check also uses
# git (libcurl) as a native, non-Python network probe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin tester

# Layout:
#   /work  build-time checkout the editable installs point to. At run time a
#          tmpfs owned by the test user is mounted over it and the entrypoint
#          fills it with a copy of /app after the isolation self-check: some
#          tests write into the working tree (e.g. reports/), and this is the
#          only writable place besides /tmp.
#   /app   pristine, read-only copy of the same source (owned by root).
WORKDIR /work
COPY . /work

# Defense in depth: the build context must never contain a .env file.
# Third-party packages come only from the hash-verified lock (Phase 3H-R2);
# the two local projects are installed offline (--no-index) without fetching
# their build backend (it is in the lock).
RUN if find /work -name .env -not -path '/work/.git/*' | grep -q .; then \
        echo "ERROR: .env found in the build context" >&2; exit 1; \
    fi \
    && pip install --require-hashes --no-deps --only-binary=:all: -r requirements/ci-lock.txt \
    && pip install --no-deps --no-build-isolation --no-index \
        -e . -e tools/generation_workspace \
    && pip check \
    && cp -R /work /app

WORKDIR /app
USER 10001:10001
CMD ["sh", "ci/isolated-tests/run_in_container.sh"]
