#!/usr/bin/env bash
# Run BOTH test suites; fail if either fails.
#   1. unit tests: fast, no services
#   2. integration tests: need Qdrant (docker compose up -d qdrant), the cached bge-m3 model,
#      the built index (uv run python -m scripts.build_index) and Saleor channel vn (read only)
set -u
cd "$(dirname "$0")/.."

uv run pytest -q
unit=$?
uv run pytest -q -m integration
integration=$?

echo "unit tests: $([ $unit -eq 0 ] && echo PASS || echo "FAIL ($unit)")"
echo "integration tests: $([ $integration -eq 0 ] && echo PASS || echo "FAIL ($integration)")"
[ $unit -eq 0 ] && [ $integration -eq 0 ]
