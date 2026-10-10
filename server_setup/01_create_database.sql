-- ADMI FAI Studio — run ONCE on the PostgreSQL server as the postgres superuser, e.g.:
--   psql -U postgres -f 01_create_database.sql
-- Change the password before running.

CREATE ROLE fai_app LOGIN PASSWORD 'CHANGE-ME-strong-password';
CREATE DATABASE fai_studio OWNER fai_app ENCODING 'UTF8';

-- Optional read-only role for auditors / BI tools
CREATE ROLE fai_readonly LOGIN PASSWORD 'CHANGE-ME-too';
\connect fai_studio
GRANT CONNECT ON DATABASE fai_studio TO fai_readonly;
GRANT USAGE ON SCHEMA public TO fai_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE fai_app IN SCHEMA public GRANT SELECT ON TABLES TO fai_readonly;

-- The tables are created automatically the first time FAI Studio connects as fai_app
-- (or run:  psql -U fai_app -d fai_studio -f ../fai_studio/schema.sql )
