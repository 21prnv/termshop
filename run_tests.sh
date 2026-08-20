#!/usr/bin/env bash
# Run the full test suite (plain scripts, no pytest needed).
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
[ -f tests/sample.jpg ] || (cd tests && "$PY" make_sample.py)
for t in tests/*_test.py; do
    echo "== $t"
    "$PY" "$t"
done
echo "ALL GREEN"
