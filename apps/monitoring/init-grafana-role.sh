#!/bin/sh
# Mounted into /docker-entrypoint-initdb.d by timescaledb.container: creates the read-only role `grafana` in the default database.
# The image runs initdb scripts only when the data directory is empty (first start of a new volume). Sourced or executed by
# the entrypoint with POSTGRES_USER, POSTGRES_DB and the env file's GRAFANA_DB_PASSWORD in its environment.
psql -v ON_ERROR_STOP=1 -v db="$POSTGRES_DB" -v owner="$POSTGRES_USER" -v pw="$GRAFANA_DB_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE grafana LOGIN PASSWORD :'pw';
ALTER ROLE grafana SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE :"db" TO grafana;
GRANT USAGE ON SCHEMA public TO grafana;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public GRANT SELECT ON TABLES TO grafana;
SQL
