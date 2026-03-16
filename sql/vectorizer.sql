-- Optional cleanup
DROP TABLE IF EXISTS articles CASCADE;

-- Load extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgedge_vectorizer;

-- Create table
CREATE TABLE articles (
    id BIGSERIAL PRIMARY KEY,
    title TEXT,
    content TEXT,
    url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable vectorization
SELECT pgedge_vectorizer.enable_vectorization(
    'articles', 'content', 'token_based', 400, 50
);

-- Insert documents
INSERT INTO articles (title, content, url)
VALUES
('Introduction to PostgreSQL', 'PostgreSQL is a powerful, open source object-relational database system...', 'https://example.com/postgres-intro'),
('MySQL Overview', 'MySQL is a popular open source relational database management system used for web applications.', NULL),
('MongoDB Basics', 'MongoDB is a document-oriented NoSQL database used for high volume data storage.', NULL),
('Database Indexing', 'Database indexes improve query performance by allowing faster data retrieval without scanning entire tables.', NULL),
('PostgreSQL Extensions', 'PostgreSQL supports powerful extensions like pgvector for similarity search and PostGIS for geospatial data.', NULL);

-- Wait for background workers to process the queue
SELECT pg_sleep(30);

-- Check queue and embeddings
SELECT * FROM pgedge_vectorizer.queue_status;
SELECT COUNT(*) as total, COUNT(embedding) as with_embeddings FROM articles_content_chunks;

-- Example similarity search
SELECT
    a.title,
    LEFT(c.content, 80) as content_preview,
    c.embedding <=> (
        SELECT embedding FROM articles_content_chunks WHERE source_id = 1 LIMIT 1
    ) AS distance
FROM articles a
JOIN articles_content_chunks c ON a.id = c.source_id
WHERE c.embedding IS NOT NULL
ORDER BY distance
LIMIT 5;


