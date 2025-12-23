-- Create test table with sample data
CALL paradedb.create_bm25_test_table(
  schema_name => 'public',
  table_name => 'mock_items'
);