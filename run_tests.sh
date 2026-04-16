#!/bin/bash
# Run the QuantOpsAI test suite.
# Usage: ./run_tests.sh [pytest args]
#
# Examples:
#   ./run_tests.sh                    # Run all tests
#   ./run_tests.sh tests/test_imports.py  # Run import tests only
#   ./run_tests.sh -x                 # Stop on first failure
#   ./run_tests.sh -k "strategy"      # Run only strategy tests

set -e

cd "$(dirname "$0")"
source venv/bin/activate

echo "============================================"
echo "  QuantOpsAI Test Suite"
echo "============================================"
echo ""

python -m pytest "$@"
