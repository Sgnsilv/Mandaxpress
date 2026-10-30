"""Regras puras de validação de credenciais."""
import re

from .errors import InvalidInputError, WeakPasswordError

MIN_PASSWORD_LENGTH = 10
# bcrypt só considera os primeiros 72 bytes; recusar acima disso evita truncamento silencioso.
MAX_PASSWORD_BYTES = 72
_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,50}$")


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if not _USERNAME_RE.match(normalized):
        raise InvalidInputError(
            "Usuário deve ter de 3 a 50 caracteres: letras, números, ponto, hífen ou sublinhado."
        )
    return normalized


def validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not 1 <= len(cleaned) <= 100:
        raise InvalidInputError("Nome deve ter de 1 a 100 caracteres.")
    return cleaned


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise WeakPasswordError(f"A senha deve ter no máximo {MAX_PASSWORD_BYTES} bytes.")
    if password.strip() != password or not password.strip():
        raise WeakPasswordError("A senha não pode começar ou terminar com espaços.")
