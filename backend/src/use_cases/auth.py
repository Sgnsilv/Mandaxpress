"""Casos de uso de autenticação: login, validação de sessão e logout."""
import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Callable, Optional, Tuple

from ..domain.credentials import normalize_username
from ..domain.errors import (
    InvalidCredentialsError,
    NotAuthenticatedError,
    PermissionDeniedError,
)
from ..domain.models import AuthContext, Role
from ..domain.ports import Clock, PasswordHasher, UnitOfWork

logger = logging.getLogger("mandaxpress.auth")

# Evita gravar em toda requisição: só atualiza "último uso" se passou deste intervalo.
_TOUCH_INTERVAL = timedelta(seconds=60)


def hash_token(token: str) -> str:
    """O banco guarda só o SHA-256 do token; vazamento do banco não revela cookies."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        hasher: PasswordHasher,
        clock: Clock,
        idle_timeout: timedelta,
        absolute_timeout: timedelta,
    ):
        self._uow = uow_factory
        self._hasher = hasher
        self._clock = clock
        self._idle = idle_timeout
        self._absolute = absolute_timeout
        # Hash falso para gastar o mesmo tempo quando o usuário não existe.
        self._dummy_hash = hasher.hash("mandaxpress-dummy-password")

    def login(self, username: str, password: str, user_agent: Optional[str] = None
              ) -> Tuple[str, AuthContext]:
        """Retorna (token_da_sessão, contexto). Mensagem de erro idêntica para
        usuário inexistente, senha errada ou conta inativa."""
        username = normalize_username(username)
        with self._uow() as uow:
            user = uow.users.get_by_username(username)
            stored_hash = user.password_hash if user else self._dummy_hash
            password_ok = self._hasher.verify(password, stored_hash)
            if user is None or not password_ok or not user.is_active:
                # Sem o username no log: evita gravar senhas digitadas no campo errado.
                logger.warning("Login recusado")
                raise InvalidCredentialsError("Usuário ou senha inválidos.")

            now = self._clock.now()
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            uow.sessions.add(hash_token(token), csrf, user.id, now,
                             now + self._absolute, (user_agent or "")[:255] or None)
            uow.sessions.delete_expired(now - timedelta(days=1))
            logger.info("Login ok user_id=%s role=%s", user.id, user.role.value)
            return token, AuthContext(user=user, csrf_token=csrf)

    def authenticate(self, token: Optional[str]) -> AuthContext:
        if not token:
            raise NotAuthenticatedError("Autenticação necessária.")
        with self._uow() as uow:
            found = uow.sessions.find_by_token_hash(hash_token(token))
            if found is None:
                raise NotAuthenticatedError("Sessão inválida.")
            session, user = found
            now = self._clock.now()
            if session.revoked_at is not None:
                raise NotAuthenticatedError("Sessão encerrada.")
            if now >= session.expires_at or now >= session.last_seen_at + self._idle:
                raise NotAuthenticatedError("Sessão expirada.")
            if not user.is_active:
                raise NotAuthenticatedError("Conta desativada.")
            if now - session.last_seen_at >= _TOUCH_INTERVAL:
                uow.sessions.touch(session.id, now)
            return AuthContext(user=user, csrf_token=session.csrf_token)

    def logout(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._uow() as uow:
            uow.sessions.revoke(hash_token(token), self._clock.now())

    @staticmethod
    def require_role(ctx: AuthContext, *roles: Role) -> None:
        if roles and ctx.user.role not in roles:
            raise PermissionDeniedError("Permissão insuficiente.")
