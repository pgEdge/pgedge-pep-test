-- pg_stat_monitor_smoke_test.sql
-- Smoke test for pg_stat_monitor extension (5 core tests)

\set ON_ERROR_STOP on
\timing on

-- Test 1: Extension Creation
\echo '=== Test 1: Create Extension ==='
DROP EXTENSION IF EXISTS pg_stat_monitor CASCADE;
CREATE EXTENSION pg_stat_monitor;
SELECT extname, extversion FROM pg_extension WHERE extname = 'pg_stat_monitor';

-- Test 2: Reset Statistics
\echo '=== Test 2: Reset Statistics ==='
SELECT pg_stat_monitor_reset();
SELECT COUNT(*) as records_after_reset FROM pg_stat_monitor;

-- Test 3: Generate Query Activity
\echo '=== Test 3: Generate Query Activity ==='
DROP TABLE IF EXISTS test_stat_monitor;
CREATE TABLE test_stat_monitor (
    id SERIAL PRIMARY KEY,
    name TEXT,
    value INTEGER
);

INSERT INTO test_stat_monitor (name, value)
SELECT 'test_' || i, i * 10
FROM generate_series(1, 100) i;

SELECT COUNT(*) FROM test_stat_monitor;
SELECT * FROM test_stat_monitor WHERE id = 50;
SELECT AVG(value) FROM test_stat_monitor;
UPDATE test_stat_monitor SET value = value + 1 WHERE id <= 10;

-- Test 4: Query Statistics
\echo '=== Test 4: Query Basic Statistics ==='
SELECT
    LEFT(query, 80) as query_excerpt,
    calls,
    total_exec_time,
    mean_exec_time,
    rows
FROM pg_stat_monitor
WHERE query NOT LIKE '%pg_stat_monitor%'
ORDER BY calls DESC
LIMIT 10;

-- Test 5: Cleanup
\echo '=== Test 5: Cleanup ==='
DROP TABLE IF EXISTS test_stat_monitor CASCADE;
SELECT pg_stat_monitor_reset();

\echo '=== All Smoke Tests Completed Successfully ==='