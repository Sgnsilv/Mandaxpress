"""Repositórios SQL (consultas sempre parametrizadas)."""
from datetime import datetime
from typing import Optional, Tuple

import pymysql

from ...domain.errors import UserAlreadyExistsError
from ...domain.models import Role, SessionRecord, User
from ...domain.ports import SessionRepository, UserRepository

_DUPLICATE_ENTRY = 1062


def _row_to_user(row: dict) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=Role(row["role"]),
        is_active=bool(row["is_active"]),
    )


class MariaDbUserRepository(UserRepository):
    def __init__(self, conn):
        self._conn = conn

    def get_by_username(self, username: str) -> Optional[User]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, username, password_hash, role, is_active "
                "FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
        return _row_to_user(row) if row else None

    def add(self, name: str, username: str, password_hash: str, role: Role) -> User:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (name, username, password_hash, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (name, username, password_hash, role.value),
                )
                new_id = cur.lastrowid
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                raise UserAlreadyExistsError("Já existe um usuário com esse nome de usuário.") from exc
            raise
        return User(id=new_id, name=name, username=username, role=role,
                    is_active=True, password_hash=password_hash)

    def update_password(self, user_id: int, password_hash: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                        (password_hash, user_id))


class MariaDbSessionRepository(SessionRepository):
    def __init__(self, conn):
        self._conn = conn

    def add(self, token_hash, csrf_token, user_id, now, expires_at, user_agent) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (token_hash, csrf_token, user_id, created_at, "
                "last_seen_at, expires_at, user_agent) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (token_hash, csrf_token, user_id, now, now, expires_at, user_agent),
            )

    def find_by_token_hash(self, token_hash: str) -> Optional[Tuple[SessionRecord, User]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT s.id AS session_id, s.csrf_token, s.user_id, s.created_at, "
                "s.last_seen_at, s.expires_at, s.revoked_at, "
                "u.id, u.name, u.username, u.password_hash, u.role, u.is_active "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s",
                (token_hash,),
            )
            row = cur.fetchone()
        if not row:
            return None
        session = SessionRecord(
            id=row["session_id"],
            user_id=row["user_id"],
            csrf_token=row["csrf_token"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )
        return session, _row_to_user(row)

    def touch(self, session_id: int, now: datetime) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UPDATE sessions SET last_seen_at = %s WHERE id = %s", (now, session_id))

    def revoke(self, token_hash: str, now: datetime) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
                (now, token_hash),
            )

    def revoke_all_for_user(self, user_id: int, now: datetime) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
                (now, user_id),
            )

    def delete_expired(self, before: datetime) -> int:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < %s", (before,))
            return cur.rowcount
