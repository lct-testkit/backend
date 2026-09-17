#!/bin/bash
# Создаёт отдельную базу для Keycloak при первичной инициализации кластера.
# Keycloak и CRM не должны делить одну схему.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE keycloak'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
EOSQL

echo "база keycloak готова"
