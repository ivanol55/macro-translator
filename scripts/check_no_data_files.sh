#!/bin/sh
# Fail when git tracks a data dump or a generated database.
# The repository must not distribute Open Food Facts or USDA data, except the
# small test fixtures in tests/fixtures (see DATA_LICENSES.md).
set -eu

found=$(git ls-files -- '*.duckdb' '*.duckdb.wal' '*.parquet' '*.csv.gz' '*.zip' 'data/*' '.env' || true)
if [ -n "$found" ]; then
  echo "These files must not be in the repository:"
  echo "$found"
  echo "Remove them with: git rm --cached <file>"
  exit 1
fi
echo "OK: no data dumps or databases are tracked."
