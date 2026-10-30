"""Conexão com MariaDB e Unidade de Trabalho transacional.

Uma conexão por unidade de trabalho (simples e segura para um único caixa).
Todas as conexões usam UTC, charset utf8mb4 e autocommit desligado.
"""
from typing import Optional

import pymysql
import pymysql.cursors

from ...config import Config
from ...domain.ports import UnitOfWork
from .repositories import (
    MariaDbAuditLogRepository,
    MariaDbCashMovementRepository,
    MariaDbPaymentSettingsRepository,
    MariaDbProductRepository,
    MariaDbSaleRepository,
    MariaDbSessionRepository,
    MariaDbShiftRepository,
    MariaDbTabRepository,
    MariaDbUserRepository,
)


class Database:
    def __init__(self, config: Config):
        self._config = config

    def connect(self, autocommit: bool = False, database: Optional[str] = None):
        c = self._config
        return pymysql.connect(
            host=c.db_host,
            port=c.db_port,
            user=c.db_user,
            password=c.db_password,
            database=database or c.db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=autocommit,
            connect_timeout=5,
            read_timeout=15,
            write_timeout=15,
            init_command="SET time_zone = '+00:00'",
        )

    def ping(self) -> bool:
        try:
            conn = self.connect(autocommit=True)
        except pymysql.Error:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except pymysql.Error:
            return False
        finally:
            conn.close()


class MariaDbUnitOfWork(UnitOfWork):
    def __init__(self, db: Database):
        self._db = db
        self._conn = None

    def __enter__(self) -> "MariaDbUnitOfWork":
        self._conn = self._db.connect()
        self.users = MariaDbUserRepository(self._conn)
        self.sessions = MariaDbSessionRepository(self._conn)
        self.products = MariaDbProductRepository(self._conn)
        self.payment_settings = MariaDbPaymentSettingsRepository(self._conn)
        self.shifts = MariaDbShiftRepository(self._conn)
        self.sales = MariaDbSaleRepository(self._conn)
        self.tabs = MariaDbTabRepository(self._conn)
        self.cash_movements = MariaDbCashMovementRepository(self._conn)
        self.audit_logs = MariaDbAuditLogRepository(self._conn)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
            self._conn = None
        return False
