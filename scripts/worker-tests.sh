#!/bin/sh
# Run the Actual API worker contract tests without Node on the host.
set -e
cd "$(dirname "$0")/.."
exec docker run --rm -v "$PWD":/work -w /work node:22-bookworm-slim \
  sh -c "npm ci --omit=dev --no-audit --no-fund >/dev/null 2>&1 && npm test"
