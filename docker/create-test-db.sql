-- Runs once when the Postgres container is first created: databases for tests and evals.
CREATE DATABASE parley_test OWNER parley;
CREATE DATABASE parley_eval OWNER parley;
