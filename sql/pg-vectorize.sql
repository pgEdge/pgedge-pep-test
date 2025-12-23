-- Create extension
CREATE EXTENSION pg_vectorize CASCADE;

-- Check if extension loaded
SELECT * FROM pg_extension WHERE extname = 'vectorize';

-- Create a table for RAG
CREATE TABLE wiki (
  id SERIAL PRIMARY KEY,
  content TEXT
);

-- Insert sample data
INSERT INTO wiki (content) VALUES
('PostgreSQL is a powerful relational database.'),
('Vector embeddings enable semantic search.'),
('RAG combines retrieval with generation.');

-- Create vectorizer (requires embedding service config)
SELECT vectorize.create_vectorizer(
  'wiki_vectorizer',
  source => 'wiki',
  destination => 'wiki_embedding',
  embedding => 'openai/text-embedding-3-small'  -- or other model
);