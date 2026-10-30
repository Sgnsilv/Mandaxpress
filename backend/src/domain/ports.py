"""Portas (interfaces) exigidas pelo núcleo. Os adaptadores as implementam."""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Tuple

from .models import Role, SessionRecord, User


class UserRepository(ABC):
    @abstractmethod
    def get_by_username(self, username: str) -> Optional[User]: ...

    @abstractmethod
    def add(self, name: str, username: str, password_hash: str, role: Role) -> User:
        """Levanta UserAlreadyExistsError se o username já existir."""

    @abstractmethod
    def update_password(self, user_id: int, password_hash: str) -> None: ...


class SessionRepository(ABC):
    @abstractmethod
    def add(self, token_hash: str, csrf_token: str, user_id: int, now: datetime,
            expires_at: datetime, user_agent: Optional[str]) -> None: ...

    @abstractmethod
    def find_by_token_hash(self, token_hash: str) -> Optional[Tuple[SessionRecord, User]]: ...

    @abstractmethod
    def touch(self, session_id: int, now: datetime) -> None: ...

    @abstractmethod
    def revoke(self, token_hash: str, now: datetime) -> None: ...

    @abstractmethod
    def revoke_all_for_user(self, user_id: int, now: datetime) -> None: ...

    @abstractmethod
    def delete_expired(self, before: datetime) -> int: ...


class UnitOfWork(ABC):
    """Fronteira transacional. Commit ao sair sem erro; rollback com erro."""
    users: UserRepository
    sessions: SessionRepository

    @abstractmethod
    def __enter__(self) -> "UnitOfWork": ...

    @abstractmethod
    def __exit__(self, exc_type, exc, tb) -> bool: ...


class PasswordHasher(ABC):
    @abstractmethod
    def hash(self, password: str) -> str: ...

    @abstractmethod
    def verify(self, password: str, password_hash: str) -> bool: ...


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Data/hora atual em UTC, sem tzinfo (naive)."""
