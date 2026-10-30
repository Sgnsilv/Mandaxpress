"""Adaptadores de segurança e tempo."""
from datetime import datetime, timezone

import bcrypt

from ...domain.ports import Clock, PasswordHasher


class BcryptPasswordHasher(PasswordHasher):
    def __init__(self, rounds: int = 12):
        self._rounds = rounds

    def hash(self, password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(self._rounds)).decode("ascii")

    def verify(self, password: str, password_hash: str) -> bool:
        data = password.encode("utf-8")
        if len(data) > 72:  # bcrypt só olha 72 bytes; recusar evita truncamento silencioso
            return False
        try:
            return bcrypt.checkpw(data, password_hash.encode("ascii"))
        except ValueError:
            return False


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
