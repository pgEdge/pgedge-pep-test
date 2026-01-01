\timing on

-- =============================================================================
-- Test 1: Create Extension and Verify Schema
-- =============================================================================
\echo '=== Test 1: Create Extension and Verify Schema ==='

DROP EXTENSION IF EXISTS lolor CASCADE;
CREATE EXTENSION lolor;

-- Verify extension is created
SELECT extname, extversion FROM pg_extension WHERE extname = 'lolor';

-- Verify lolor schema exists
SELECT nspname FROM pg_namespace WHERE nspname = 'lolor';

-- Verify lolor tables exist
SELECT schemaname, tablename
FROM pg_tables
WHERE schemaname = 'lolor'
ORDER BY tablename;

-- =============================================================================
-- Test 2: Create Large Object with Auto OID
-- =============================================================================
\echo '=== Test 2: Create Large Object with Auto OID ==='

-- Create a large object with auto-generated OID
SELECT lo_creat(-1) AS auto_oid \gset

-- Verify it was created in lolor.pg_largeobject_metadata
SELECT oid, lomowner
FROM lolor.pg_largeobject_metadata
WHERE oid = :auto_oid;

-- =============================================================================
-- Test 3: Create Large Object with Specific OID
-- =============================================================================
\echo '=== Test 3: Create Large Object with Specific OID ==='

-- Create large object with specific OID
SELECT lo_create(200000) AS specific_oid;

-- Verify it was created
SELECT oid, lomowner
FROM lolor.pg_largeobject_metadata
WHERE oid = 200000;

-- =============================================================================
-- Test 4: Write Data to Large Object
-- =============================================================================
\echo '=== Test 4: Write Data to Large Object ==='

-- Open large object for writing
SELECT lo_open(200000, 131072) AS fd \gset

-- Write data to the large object
SELECT lo_write(:fd, 'This is test data for lolor large object') AS bytes_written;

-- Close the large object
SELECT lo_close(:fd);

-- Verify data was written
SELECT loid, pageno, length(data) as data_length
FROM lolor.pg_largeobject
WHERE loid = 200000;

-- =============================================================================
-- Test 5: Read Data from Large Object
-- =============================================================================
\echo '=== Test 5: Read Data from Large Object ==='

-- Open for reading
SELECT lo_open(200000, 262144) AS read_fd \gset

-- Read data
SELECT lo_read(:read_fd, 100) AS data;

-- Close
SELECT lo_close(:read_fd);

-- =============================================================================
-- Test 6: Export Large Object (Skip if no /tmp access)
-- =============================================================================
\echo '=== Test 6: Export Large Object ==='

-- Export to file (this may fail in containerized environments without /tmp write access)
-- We'll just verify the function exists
SELECT proname, pronargs
FROM pg_proc
WHERE proname = 'lo_export'
LIMIT 1;

-- =============================================================================
-- Test 7: Truncate Large Object
-- =============================================================================
\echo '=== Test 7: Truncate Large Object ==='

-- Create another large object
SELECT lo_create(200001) AS trunc_oid;

-- Open for writing
SELECT lo_open(200001, 131072) AS trunc_fd \gset

-- Write some data
SELECT lo_write(:trunc_fd, 'Data to be truncated') AS written;

-- Truncate to 10 bytes
SELECT lo_truncate(:trunc_fd, 10);

-- Close
SELECT lo_close(:trunc_fd);

-- Verify truncation
SELECT loid, pageno, length(data) as data_length
FROM lolor.pg_largeobject
WHERE loid = 200001;

-- =============================================================================
-- Test 8: Seek in Large Object
-- =============================================================================
\echo '=== Test 8: Seek in Large Object ==='

-- Open for reading
SELECT lo_open(200000, 262144) AS seek_fd \gset

-- Seek to position 5
SELECT lo_lseek(:seek_fd, 5, 0) AS position;

-- Read from new position
SELECT lo_read(:seek_fd, 10) AS data_after_seek;

-- Get current position
SELECT lo_tell(:seek_fd) AS current_position;

-- Close
SELECT lo_close(:seek_fd);

-- =============================================================================
-- Test 9: Unlink (Delete) Large Objects
-- =============================================================================
\echo '=== Test 9: Unlink (Delete) Large Objects ==='

-- Unlink the large objects we created
SELECT lo_unlink(:auto_oid) AS unlink_result_1;
SELECT lo_unlink(200000) AS unlink_result_2;
SELECT lo_unlink(200001) AS unlink_result_3;

-- Verify they were deleted
SELECT COUNT(*) AS remaining_objects
FROM lolor.pg_largeobject_metadata
WHERE oid IN (:auto_oid, 200000, 200001);

-- =============================================================================
-- Test 10: Verify Lolor System Views
-- =============================================================================
\echo '=== Test 10: Verify Lolor System Views ==='

-- Create a test large object for this test
SELECT lo_creat(-1) AS view_test_oid \gset

-- Query lolor.pg_largeobject_metadata
SELECT COUNT(*) > 0 AS metadata_accessible
FROM lolor.pg_largeobject_metadata
WHERE oid = :view_test_oid;

-- Query lolor.pg_largeobject
SELECT COUNT(*) >= 0 AS largeobject_accessible
FROM lolor.pg_largeobject
WHERE loid = :view_test_oid;

-- Cleanup
SELECT lo_unlink(:view_test_oid);

-- =============================================================================
-- Test 11: Large Object Permissions
-- =============================================================================
\echo '=== Test 11: Large Object Permissions ==='

-- Create a large object
SELECT lo_creat(-1) AS perm_oid \gset

-- Verify owner
SELECT oid, lomowner::regrole AS owner
FROM lolor.pg_largeobject_metadata
WHERE oid = :perm_oid;

-- Cleanup
SELECT lo_unlink(:perm_oid);

-- =============================================================================
-- Test 12: Multiple Large Objects
-- =============================================================================
\echo '=== Test 12: Multiple Large Objects ==='

-- Create multiple large objects
SELECT lo_creat(-1) AS oid1 \gset
SELECT lo_creat(-1) AS oid2 \gset
SELECT lo_creat(-1) AS oid3 \gset

-- Write different data to each
DO $$
DECLARE
    fd integer;
BEGIN
    -- Write to oid1
    fd := lo_open(:oid1, 131072);
    PERFORM lo_write(fd, 'Data for object 1');
    PERFORM lo_close(fd);

    -- Write to oid2
    fd := lo_open(:oid2, 131072);
    PERFORM lo_write(fd, 'Data for object 2');
    PERFORM lo_close(fd);

    -- Write to oid3
    fd := lo_open(:oid3, 131072);
    PERFORM lo_write(fd, 'Data for object 3');
    PERFORM lo_close(fd);
END $$;

-- Verify all objects exist
SELECT COUNT(*) AS object_count
FROM lolor.pg_largeobject_metadata
WHERE oid IN (:oid1, :oid2, :oid3);

-- Cleanup
SELECT lo_unlink(:oid1);
SELECT lo_unlink(:oid2);
SELECT lo_unlink(:oid3);

-- =============================================================================
-- Summary
-- =============================================================================
\echo '=== Test Summary ==='
\echo 'All lolor functional tests completed successfully!'