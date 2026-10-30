"""Casos de uso administrativos de usuários (usados pela CLI nesta etapa)."""
from datetime import datetime
from typing import Callable

from ..domain.credentials import (
    validate_name,
    validate_password_strength,
    validate_username,
)
from ..domain.errors import UserNotFoundError
from ..domain.models import Role, User
from ..domain.ports import Clock, PasswordHasher, UnitOfWork


class UserAdminService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], hasher: PasswordHasher, clock: Clock):
        self._uow = uow_factory
        self._hasher = hasher
        self._clock = clock

    def create_user(self, name: str, username: str, password: str, role: Role) -> User:
        name = validate_name(name)
        username = validate_username(username)
        validate_password_strength(password)
        password_hash = self._hasher.hash(password)
        with self._uow() as uow:
            return uow.users.add(name, username, password_hash, role)

    def set_password(self, username: str, password: str) -> None:
        """Troca a senha e revoga todas as sessões do usuário."""
        username = validate_username(username)
        validate_password_strength(password)
        password_hash = self._hasher.hash(password)
        with self._uow() as uow:
            user = uow.users.get_by_username(username)
            if user is None:
                raise UserNotFoundError("Usuário não encontrado.")
            uow.users.update_password(user.id, password_hash)
            uow.sessions.revoke_all_for_user(user.id, self._clock.now())
