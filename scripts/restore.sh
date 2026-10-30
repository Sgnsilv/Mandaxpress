#!/usr/bin/env bash
# Restaura um backup gerado por scripts/backup.sh.
# Uso: ./scripts/restore.sh backups/arquivo.sql.gz
# ATENÇÃO: substitui os dados atuais do banco. Pede confirmação explícita
# e faz um backup de segurança do estado atual antes de restaurar.
set -euo pipefail
cd "$(dirname "$0")/.."

file="${1:-}"
[ -n "$file" ] && [ -f "$file" ] || { echo "Uso: $0 <backup.sql.gz>" >&2; exit 1; }
gzip -t "$file"

echo "Isto vai SUBSTITUIR os dados atuais do banco pelo conteúdo de: $file"
read -r -p "Digite RESTAURAR para continuar: " answer
[ "$answer" = "RESTAURAR" ] || { echo "Cancelado."; exit 1; }

safety="backups/pre-restore-$(date +%Y%m%d-%H%M%S).sql.gz"
echo "Backup de segurança do estado atual -> $safety"
"$(dirname "$0")/backup.sh" "$safety"

# Para o backend durante a restauração (evita escrita concorrente).
docker compose stop backend >/dev/null
zcat "$file" | docker compose exec -T db sh -c \
  'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb -uroot'
docker compose up -d backend >/dev/null
echo "Restauração concluída."
