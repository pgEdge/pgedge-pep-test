-- pg_tokenizer_smoke_test.sql
-- Smoke test for pg_tokenizer extension

\set ON_ERROR_STOP on
\timing on

-- Test 1: Extension Creation
\echo '=== Test 1: Create Extension ==='
DROP EXTENSION IF EXISTS pg_tokenizer CASCADE;
CREATE EXTENSION pg_tokenizer;
SELECT extname, extversion FROM pg_extension WHERE extname = 'pg_tokenizer';

-- Test 2: Create BERT Tokenizer
\echo '=== Test 2: Create BERT Tokenizer ==='
SELECT create_tokenizer('bert_tokenizer', $$
model = "bert-base-uncased"
$$);

-- Verify tokenizer was created
SELECT * FROM tokenizers WHERE name = 'bert_tokenizer';

-- Test 3: Tokenize Simple Text
\echo '=== Test 3: Tokenize Simple Text ==='
SELECT tokenize('bert_tokenizer', 'Hello world! This is a test.');

-- Test 4: Tokenize with Special Characters
\echo '=== Test 4: Tokenize with Special Characters ==='
SELECT tokenize('bert_tokenizer', 'PostgreSQL@2024 - AI/ML integration #database');

-- Test 5: Tokenize Multiple Sentences
\echo '=== Test 5: Tokenize Multiple Sentences ==='
SELECT tokenize('bert_tokenizer',
    'PostgreSQL is a powerful database. It supports many extensions. AI integration is exciting!'
);

-- Test 6: Create Table with Tokenized Data
\echo '=== Test 6: Create Table and Store Tokenized Results ==='
DROP TABLE IF EXISTS documents;
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    content TEXT,
    tokens TEXT[]
);

INSERT INTO documents (content, tokens) VALUES
    ('Machine learning with PostgreSQL', tokenize('bert_tokenizer', 'Machine learning with PostgreSQL')),
    ('Natural language processing', tokenize('bert_tokenizer', 'Natural language processing')),
    ('Database performance optimization', tokenize('bert_tokenizer', 'Database performance optimization'));

SELECT id, content, array_length(tokens, 1) as token_count, tokens
FROM documents
ORDER BY id;

-- Test 7: Token Count Analysis
\echo '=== Test 7: Token Count Analysis ==='
SELECT
    content,
    array_length(tokenize('bert_tokenizer', content), 1) as token_count
FROM documents
ORDER BY token_count DESC;

-- Test 8: Create Additional Tokenizer (if supported)
\echo '=== Test 8: Create Additional Tokenizer Types ==='
-- Try creating a different tokenizer type
DO $$
BEGIN
    PERFORM create_tokenizer('simple_tokenizer', $$
    model = "bert-base-cased"
    $$);
    RAISE NOTICE 'Successfully created simple_tokenizer';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Could not create additional tokenizer: %', SQLERRM;
END $$;

-- Test 9: Tokenize Empty and Edge Cases
\echo '=== Test 9: Edge Cases ==='

-- Empty string
SELECT tokenize('bert_tokenizer', '') as empty_string_tokens;

-- Single character
SELECT tokenize('bert_tokenizer', 'a') as single_char_tokens;

-- Numbers
SELECT tokenize('bert_tokenizer', '12345 67890') as number_tokens;

-- Very long word
SELECT tokenize('bert_tokenizer', 'supercalifragilisticexpialidocious') as long_word_tokens;

-- Test 10: List All Tokenizers
\echo '=== Test 10: List All Tokenizers ==='
SELECT name, config FROM tokenizers ORDER BY name;

-- Test 11: Compare Token Counts
\echo '=== Test 11: Compare Token Counts for Similar Phrases ==='
WITH test_phrases AS (
    SELECT 'PostgreSQL database' as phrase
    UNION ALL
    SELECT 'PostgreSQL databases'
    UNION ALL
    SELECT 'The PostgreSQL database system'
)
SELECT
    phrase,
    array_length(tokenize('bert_tokenizer', phrase), 1) as token_count,
    tokenize('bert_tokenizer', phrase) as tokens
FROM test_phrases;

-- Test 12: Performance Test (Small Scale)
\echo '=== Test 12: Performance Test ==='
\timing on
SELECT COUNT(*) FROM (
    SELECT tokenize('bert_tokenizer', 'Sample text for performance testing ' || generate_series)
    FROM generate_series(1, 100)
) sub;
\timing off

-- Test 13: Drop Tokenizer
\echo '=== Test 13: Drop Tokenizer ==='
SELECT drop_tokenizer('bert_tokenizer');

-- Verify it's gone
SELECT COUNT(*) as remaining_tokenizers FROM tokenizers WHERE name = 'bert_tokenizer';

-- Test 14: Cleanup
\echo '=== Test 14: Cleanup ==='
DROP TABLE IF EXISTS documents CASCADE;

\echo '=== All Smoke Tests Completed Successfully ==='