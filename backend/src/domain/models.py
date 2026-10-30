"""Entidades de domínio da fundação (usuários e sessões)."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Role(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"


@dataclass(frozen=True)
class User:
    id: int
    name: str
    username: str
    role: Role
    is_active: bool
    # repr=False: o hash nunca deve aparecer em logs ou tracebacks.
    password_hash: str = field(repr=False)


@dataclass(frozen=True)
class SessionRecord:
    id: int
    user_id: int
    csrf_token: str = field(repr=False)
    created_at: datetime = None  # type: ignore[assignment]
    last_seen_at: datetime = None  # type: ignore[assignment]
    expires_at: datetime = None  # type: ignore[assignment]
    revoked_at: Optional[datetime] = None


@dataclass(frozen=True)
class AuthContext:
    """Resultado de uma autenticação bem-sucedida."""
    user: User
    csrf_token: str = field(repr=False)
