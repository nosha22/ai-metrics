#!/bin/sh
# Corre uma vez, quando o volume do Postgres é criado: utilizador só de leitura para o Grafana.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE grafana LOGIN PASSWORD '${GRAFANA_DB_PASSWORD}';
GRANT USAGE ON SCHEMA public TO grafana;
ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public GRANT SELECT ON TABLES TO grafana;
SQL
