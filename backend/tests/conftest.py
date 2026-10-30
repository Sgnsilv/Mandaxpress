"""Fixtures: banco MariaDB real (banco de testes isolado) e relógio controlável."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.db import migrator
from src.adapters.security.services import BcryptPasswordHasher
from src.adapters.web.app import create_app
from src.adapters.web.container import build_container
from src.config import Config
from src.domain.models import Role
from src.domain.ports import Clock

ORIGIN = os.environ.get("ALLOWED_ORIGINS", "http://localhost:8080").split(",")[0]
PASSWORD = "senha-de-teste-123"


class FakeClock(Clock):
    def __init__(self):
        self.current = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    def now(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


@pytest.fixture(scope="session")
def config() -> Config:
    cfg = Config.from_env(os.environ)
    assert cfg.env == "testing", "Os testes só rodam com APP_ENV=testing"
    assert cfg.db_name.endswith("_test"), "Recusando rodar fora de um banco *_test"
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated(config):
    from src.adapters.db.database import Database
    migrator.migrate(Database(config), config.migrations_dir)


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def container(config, clock):
    c = build_container(config, clock=clock, hasher=BcryptPasswordHasher(rounds=4))
    conn = c.db.connect(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM users")
    conn.close()
    return c


@pytest.fixture()
def app(config, container):
    return create_app(config, container)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin(container):
    return container.users.create_user("Admin Teste", "admin", PASSWORD, Role.ADMIN)


@pytest.fixture()
def operator(container):
    return container.users.create_user("Operador Teste", "caixa", PASSWORD, Role.OPERATOR)


def login(client, username, password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password},
                       headers={"Origin": ORIGIN})


def logged_in(client, username):
    """Faz login e devolve o cabeçalho CSRF para chamadas que alteram dados."""
    resp = login(client, username)
    assert resp.status_code == 200, resp.get_json()
    return {"X-CSRF-Token": resp.get_json()["csrf_token"], "Origin": ORIGIN}
