#!/usr/bin/env sh
# Run the full yugo test suite inside python:3.13-slim.
#
# WHY: the DeetOps tool-cache Python 3.12.14 is not ABI-compatible with the
# host glibc, so `import filesystem_tools` → `import ctypes` raises
# "undefined symbol: _PyErr_SetLocaleString". The yugo image is built on
# python:3.13-slim, which ships its own glibc-compatible interpreter. This
# script mounts the source tree into a throwaway container built from that
# same base, so the suite runs in a compatible environment without altering
# CI or the runtime image.
#
# Usage (from repo root):
#   sh yugo/scripts/test-in-docker.sh
#
# Requirements: docker accessible on PATH; repo checked out locally.
# No host-specific paths — $(pwd) resolves at call time.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# Floor under the current collected count (~1071). Must be raised as the suite grows.
MIN_TESTS=1050

echo "==> Running yugo test suite in python:3.13-slim (min ${MIN_TESTS} tests)"
echo "    Repo root: ${REPO_ROOT}"

docker run --rm \
  -v "${REPO_ROOT}:/src" \
  -w /src/yugo \
  -e PYTEST_MIN_COLLECTED="${MIN_TESTS}" \
  python:3.13-slim \
  sh -c '
    set -e
    apt-get update -qq && apt-get install -y --no-install-recommends git -qq
    git config --global --add safe.directory /src
    pip install -r test-requirements.txt -r requirements.txt -q
    python -m pytest test/ -v --tb=short
  '
