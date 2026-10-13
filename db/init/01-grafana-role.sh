#!/bin/sh
# Runs once, when the Postgres volume is created: read-only user for Grafana.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE grafana LOGIN PASSWORD '${GRAFANA_DB_PASSWORD}';
GRANT USAGE ON SCHEMA public TO grafana;
ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public GRANT SELECT ON TABLES TO grafana;
SQL
