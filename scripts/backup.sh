#!/usr/bin/env bash
# Backup lógico do banco (mariadb-dump) comprimido em ./backups/.
# Uso: ./scripts/backup.sh [arquivo-de-saida.sql.gz]
# O arquivo contém dados financeiros: guarde-o fora do repositório e do servidor.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p backups
chmod 700 backups
out="${1:-backups/mandaxpress-$(date +%Y%m%d-%H%M%S).sql.gz}"

umask 077
docker compose exec -T db sh -c \
  'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb-dump -uroot --single-transaction --routines --triggers --databases "$MARIADB_DATABASE"' \
  | gzip > "$out"

# Falha se o dump veio vazio ou truncado.
gzip -t "$out"
if ! zcat "$out" | grep -q "CREATE TABLE"; then
  echo "Backup inválido (sem tabelas): $out" >&2
  rm -f "$out"
  exit 1
fi
echo "Backup criado: $out ($(du -h "$out" | cut -f1))"
