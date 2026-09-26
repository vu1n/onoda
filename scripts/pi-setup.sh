#!/usr/bin/env bash
# Install the pinned Pi runtime (pi/package-lock.json) for M3 Pi runs.
# Needs network once; nothing else in the repo installs or downloads code.
# Lifecycle scripts are skipped: the pinned packages need none to run.
set -euo pipefail
cd "$(dirname "$0")/../pi"
npm ci --ignore-scripts --no-audit --no-fund
