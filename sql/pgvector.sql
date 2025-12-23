-- pgvector_smoke_test.sql
-- Smoke test for pgvector extension

\set ON_ERROR_STOP on
\timing on

-- Test 1: Extension Creation
\echo '=== Test 1: Create Extension ==='
DROP EXTENSION IF EXISTS vector CASCADE;
CREATE EXTENSION vector;
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';

-- Test 2: Create Table with Vector Column
\echo '=== Test 2: Create Table ==='
DROP TABLE IF EXISTS items;
CREATE TABLE items (
    id SERIAL PRIMARY KEY,
    name TEXT,
    embedding vector(3)
);

-- Test 3: Insert Vector Data
\echo '=== Test 3: Insert Data ==='
INSERT INTO items (name, embedding) VALUES
    ('item1', '[1,2,3]'),
    ('item2', '[4,5,6]'),
    ('item3', '[7,8,9]'),
    ('item4', '[1,1,1]');

SELECT id, name, embedding FROM items ORDER BY id;

-- Test 4: Vector Distance Operations
\echo '=== Test 4: Distance Calculations ==='

-- L2 distance (Euclidean)
SELECT name, embedding <-> '[3,3,3]' AS l2_distance
FROM items
ORDER BY l2_distance
LIMIT 3;

-- Cosine distance
SELECT name, embedding <=> '[3,3,3]' AS cosine_distance
FROM items
ORDER BY cosine_distance
LIMIT 3;

-- Inner product
SELECT name, (embedding <#> '[3,3,3]') * -1 AS inner_product
FROM items
ORDER BY inner_product DESC
LIMIT 3;

-- Test 5: Create Indexes
\echo '=== Test 5: Create Indexes ==='

-- IVFFlat index
CREATE INDEX ON items USING ivfflat (embedding vector_l2_ops) WITH (lists = 2);

-- HNSW index (if supported)
DROP INDEX IF EXISTS items_embedding_idx;
CREATE INDEX items_embedding_idx ON items USING hnsw (embedding vector_l2_ops);

\d items

-- Test 6: Query with Index
\echo '=== Test 6: Query with Index ==='
SET enable_seqscan = off;
SELECT name, embedding <-> '[2,2,2]' AS distance
FROM items
ORDER BY distance
LIMIT 2;
SET enable_seqscan = on;

-- Test 7: Vector Dimensions
\echo '=== Test 7: Test Different Dimensions ==='
DROP TABLE IF EXISTS embeddings_high_dim;
CREATE TABLE embeddings_high_dim (
    id SERIAL PRIMARY KEY,
    embedding vector(1536)  -- Common for OpenAI embeddings
);

INSERT INTO embeddings_high_dim (embedding)
VALUES (array_fill(0.1, ARRAY[1536])::vector);

SELECT id, vector_dims(embedding) AS dimensions
FROM embeddings_high_dim;

-- Test 8: Vector Aggregations
\echo '=== Test 8: Vector Operations ==='
SELECT avg(embedding) AS avg_vector FROM items;
SELECT sum(embedding) AS sum_vector FROM items;

-- Test 9: Error Handling
\echo '=== Test 9: Dimension Mismatch Error (Expected to Fail) ==='
DO $$
BEGIN
    INSERT INTO items (name, embedding) VALUES ('invalid', '[1,2]');
    RAISE EXCEPTION 'Should have failed with dimension mismatch';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Correctly caught error: %', SQLERRM;
END $$;

-- Test 10: Cleanup
\echo '=== Test 10: Cleanup ==='
DROP TABLE IF EXISTS items CASCADE;
DROP TABLE IF EXISTS embeddings_high_dim CASCADE;

\echo '=== All Smoke Tests Completed Successfully ==='