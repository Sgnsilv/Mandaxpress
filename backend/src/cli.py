"""CLI administrativa do Mandaxpress.

Uso (dentro do container):
  python -m src.cli migrate
  python -m src.cli migrate-status
  python -m src.cli create-user --username ana --name "Ana" --role admin
  python -m src.cli set-password --username ana

A senha é sempre pedida de forma interativa (sem eco) ou lida do stdin com
--password-stdin. Nunca é aceita como argumento nem fica no histórico do shell.
"""
import argparse
import getpass
import os
import sys

from .adapters.db import migrator
from .adapters.web.container import build_container
from .config import Config, ConfigError
from .domain.errors import DomainError
from .domain.models import Role


def _read_password(from_stdin: bool, confirm: bool = True) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("Senha: ")
    if confirm and getpass.getpass("Confirme a senha: ") != password:
        raise SystemExit("As senhas não conferem.")
    return password


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mandaxpress")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Aplica migrations pendentes")
    sub.add_parser("migrate-status", help="Mostra o estado das migrations")

    cu = sub.add_parser("create-user", help="Cria um usuário (admin ou operator)")
    cu.add_argument("--username", required=True)
    cu.add_argument("--name", required=True)
    cu.add_argument("--role", choices=[r.value for r in Role], required=True)
    cu.add_argument("--password-stdin", action="store_true")

    sp = sub.add_parser("set-password", help="Redefine a senha e revoga as sessões")
    sp.add_argument("--username", required=True)
    sp.add_argument("--password-stdin", action="store_true")

    args = parser.parse_args(argv)

    try:
        config = Config.from_env(os.environ)
        container = build_container(config)

        if args.command == "migrate":
            applied = migrator.migrate(container.db, config.migrations_dir)
            print("Migrations aplicadas: " + (", ".join(applied) if applied else "nenhuma (banco em dia)"))
        elif args.command == "migrate-status":
            for row in migrator.get_status(container.db, config.migrations_dir):
                state = "aplicada" if row["applied"] else "PENDENTE"
                if row["applied"] and not row["checksum_ok"]:
                    state = "ALTERADA após aplicação!"
                print(f"{row['version']}: {state}")
        elif args.command == "create-user":
            password = _read_password(args.password_stdin)
            user = container.users.create_user(args.name, args.username, password, Role(args.role))
            print(f"Usuário criado: {user.username} ({user.role.value})")
        elif args.command == "set-password":
            password = _read_password(args.password_stdin)
            container.users.set_password(args.username, password)
            print("Senha redefinida; sessões anteriores revogadas.")
    except (ConfigError, DomainError, migrator.MigrationError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
