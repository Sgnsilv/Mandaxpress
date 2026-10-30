#!/usr/bin/env bash
# Gera um .env com segredos aleatórios a partir do modelo.
# Uso: ./scripts/init-env.sh [dev|prod]
set -euo pipefail
cd "$(dirname "$0")/.."

mode="${1:-dev}"
case "$mode" in
  dev)  template=".env.example" ;;
  prod) template=".env.production.example" ;;
  *) echo "Uso: $0 [dev|prod]" >&2; exit 1 ;;
esac

if [ -e .env ]; then
  echo "O arquivo .env já existe; nada foi alterado." >&2
  exit 1
fi

rand() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32; }

umask 077
sed -e "s/^MARIADB_PASSWORD=CHANGE_ME$/MARIADB_PASSWORD=$(rand)/" \
    -e "s/^MARIADB_ROOT_PASSWORD=CHANGE_ME$/MARIADB_ROOT_PASSWORD=$(rand)/" \
    "$template" > .env

echo ".env criado (modo $mode) com senhas de banco aleatórias."
if [ "$mode" = "prod" ]; then
  echo "AJUSTE SITE_ADDRESS e ALLOWED_ORIGINS no .env antes de subir."
fi
