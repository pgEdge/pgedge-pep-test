-- Create BM25 index
CALL paradedb.create_bm25(
  index_name => 'search_idx',
  schema_name => 'public',
  table_name => 'mock_items',
  key_field => 'id',
  text_fields => '{description: {tokenizer: {type: "en_stem"}}, category: {}}',
  numeric_fields => '{rating: {}}'
);

-- Basic search
SELECT * FROM search_idx.search('description:keyboard');

-- Search with limit
SELECT * FROM search_idx.search('description:keyboard', limit_rows => 10);

-- Complex boolean search
SELECT description, rating, category
FROM search_idx.search(
  '(description:keyboard OR category:electronics) AND rating:>2',
  limit_rows => 5
);

-- Get BM25 relevance score
SELECT id, description, paradedb.rank_bm25(id)
FROM search_idx.search('description:keyboard');

-- Using @@@ operator
SELECT * FROM mock_items WHERE mock_items @@@ ('search_idx', 'keyboard');