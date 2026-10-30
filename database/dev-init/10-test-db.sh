#!/bin/sh
# Executado pelo MariaDB apenas na PRIMEIRA criação do volume (somente dev).
# Cria o banco usado pelos testes automatizados, isolado dos dados de dev.
set -eu
mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" <<SQL
CREATE DATABASE IF NOT EXISTS \`${MARIADB_DATABASE}_test\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON \`${MARIADB_DATABASE}_test\`.* TO '${MARIADB_USER}'@'%';
FLUSH PRIVILEGES;
SQL
