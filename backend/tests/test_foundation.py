"""Testes de configuração, validação de credenciais e migrations."""
import shutil
from pathlib import Path

import pytest

from src.adapters.db import migrator
from src.adapters.db.database import Database
from src.config import Config, ConfigError
from src.domain.credentials import validate_password_strength, validate_username
from src.domain.errors import InvalidInputError, UserAlreadyExistsError, WeakPasswordError
from src.domain.models import Role

BASE_ENV = {
    "APP_ENV": "production", "DB_HOST": "db", "DB_NAME": "x", "DB_USER": "u",
    "DB_PASSWORD": "s3nh4-real-aleatoria", "ALLOWED_ORIGINS": "https://caixa.example.org",
    "COOKIE_SECURE": "true",
}


def test_config_requires_secrets_without_defaults():
    for key in ("DB_PASSWORD", "ALLOWED_ORIGINS", "APP_ENV", "DB_HOST"):
        env = {k: v for k, v in BASE_ENV.items() if k != key}
        with pytest.raises(ConfigError):
            Config.from_env(env)


def test_production_rejects_insecure_cookie():
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, "COOKIE_SECURE": "false"})


def test_production_rejects_placeholder_password_and_http_origin():
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, "DB_PASSWORD": "CHANGE_ME"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, "ALLOWED_ORIGINS": "http://caixa.example.org"})


def test_production_valid_config_and_cookie_secure_default():
    env = {k: v for k, v in BASE_ENV.items() if k != "COOKIE_SECURE"}
    cfg = Config.from_env(env)
    assert cfg.cookie_secure is True and cfg.is_production


def test_password_policy():
    validate_password_strength("uma-senha-boa-123")
    for bad in ("curta", " espaco-no-inicio-123", "x" * 73):
        with pytest.raises(WeakPasswordError):
            validate_password_strength(bad)


def test_username_validation():
    assert validate_username(" Ana.Silva ") == "ana.silva"
    with pytest.raises(InvalidInputError):
        validate_username("a b")


def test_password_is_stored_hashed(container, admin):
    conn = container.db.connect(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (admin.id,))
        stored = cur.fetchone()["password_hash"]
    conn.close()
    assert stored.startswith("$2") and "senha-de-teste" not in stored


def test_duplicate_username_rejected(container, admin):
    with pytest.raises(UserAlreadyExistsError):
        container.users.create_user("Outro", "ADMIN", "outra-senha-forte-1", Role.OPERATOR)


def test_migrations_are_idempotent_and_tracked(config):
    db = Database(config)
    assert migrator.migrate(db, config.migrations_dir) == []
    status = migrator.get_status(db, config.migrations_dir)
    assert status and all(s["applied"] and s["checksum_ok"] for s in status)


def test_modified_applied_migration_is_refused(config, tmp_path):
    copy = tmp_path / "migrations"
    shutil.copytree(config.migrations_dir, copy)
    first = sorted(Path(copy).glob("001_*.sql"))[0]
    first.write_text(first.read_text() + "\n-- adulterada\n")
    with pytest.raises(migrator.MigrationError):
        migrator.migrate(Database(config), str(copy))


def test_split_statements_ignores_comments_with_semicolons():
    sql = "-- comentario; com ponto e virgula\nCREATE TABLE a (id INT);\nCREATE TABLE b (id INT);"
    assert migrator.split_statements(sql) == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]
