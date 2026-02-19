CREATE ROLE authenticator WITH LOGIN PASSWORD 'postgres' NOINHERIT;

-- Create the anon role (used for unauthenticated requests)
CREATE ROLE anon NOLOGIN;

-- Grant anon to authenticator so it can switch into it
GRANT anon TO authenticator;

-- Optional but recommended: grant usage on public schema to anon
GRANT USAGE ON SCHEMA public TO anon;

-- If you want anon to read tables, e.g.:
GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon;
