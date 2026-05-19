# Spock Functions Availability Test

Validates all documented Spock functions exist and work in your PostgreSQL instance.

Functions are parsed dynamically from [Spock documentation](https://github.com/pgEdge/spock/tree/main/docs/spock_functions/functions) markdown files — no hardcoded function lists.

## Prerequisites

```bash
pip3 install psycopg2-binary
```

## Quick Start

```bash
# Clone Spock docs
git clone --depth 1 https://github.com/pgEdge/spock.git /tmp/spock

# Run test
python3 test_spock_functions.py \
    --dsn "host=localhost port=5432 dbname=postgres user=postgres password=postgres" \
    --docs-dir /tmp/spock/docs/spock_functions/functions
```

## Usage

```bash
# Basic availability check (safe, read-only)
python3 test_spock_functions.py --dsn "DSN" --docs-dir /path/to/docs

# With category enrichment
python3 test_spock_functions.py --dsn "DSN" \
    --docs-dir /path/to/docs/spock_functions/functions \
    --index-dir /path/to/docs/spock_functions

# Include smoke tests (calls read-only functions)
python3 test_spock_functions.py --dsn "DSN" --docs-dir /path/to/docs --smoke-test

# Export results to JSON
python3 test_spock_functions.py --dsn "DSN" --docs-dir /path/to/docs --json results.json

# Verbose mode
python3 test_spock_functions.py --dsn "DSN" --docs-dir /path/to/docs -v
```

## Options

| Flag | Required | Description |
|------|----------|-------------|
| `--dsn` | Yes | PostgreSQL connection string |
| `--docs-dir` | Yes | Path to Spock function `.md` files |
| `--index-dir` | No | Path to category index files for better grouping |
| `--smoke-test` | No | Run callable tests on read-only functions |
| `--json` | No | Export results to JSON (filename or `stdout`) |
| `-v` | No | Verbose output with parsing details |

## What It Checks

1. **Parses** all `.md` files in `--docs-dir` to extract documented function names and signatures
2. **Queries** `pg_proc` to verify each function exists in the `spock` schema
3. **Identifies** undocumented functions present in the database but missing from docs
4. **Runs smoke tests** (optional) on safe read-only functions like `spock.spock_version()`
5. **Reports** installed vs documented counts, doc coverage %, and category breakdown

## Example Output

```
  ═ PARSING SPOCK DOCUMENTATION
  · Docs directory: /tmp/spock/docs/spock_functions/functions
  ✓ Parsed 36 functions from 34 markdown files

  ═ SPOCK FUNCTION AVAILABILITY CHECK
  · PostgreSQL: PostgreSQL 17.7
  · Spock: 5.0.7

  ═ ── Node Management ──
  ✓ spock.node_create
  ✓ spock.node_drop
  ✓ spock.node_add_interface
  ✓ spock.node_drop_interface
  ✓ spock.node_info

  ═ ── Subscription Management ──
  ✓ spock.sub_create
  ✓ spock.sub_drop
  ...

  ═ TEST SUMMARY

  ═ INSTALLED IN spock SCHEMA
  · Total objects:         72
  ·   Functions:           65
  ·   Procedures:          7

  ═ DOCUMENTATION COVERAGE
  · Documented functions:   36
  ✓ Found in database:      36/36 (100.0%)
  ✓ Missing from database:  0
  ⚠ Undocumented (in DB):   36
  ⚠ Doc coverage:           36/72 (50.0%)

  ✓ ALL DOCUMENTED FUNCTIONS AVAILABLE AND WORKING
```

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All documented functions found |
| `1` | One or more documented functions missing |