#!/usr/bin/env bash
set -euo pipefail

# Run from the repository root. This fixture makes no network requests.
uv run evog --workspace "${EVOG_DEMO_WORKSPACE:-/tmp/evog-demo}" demo
