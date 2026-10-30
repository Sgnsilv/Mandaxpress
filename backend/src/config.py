"""Configuração por variáveis de ambiente, validada na inicialização.

Não há valores padrão para segredos: se faltarem, a aplicação não sobe.
"""
from dataclasses import dataclass
from typing import Mapping, Tuple

_VALID_ENVS = ("development", "production", "testing")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    pass


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"Variável de ambiente obrigatória ausente: {key}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{key} deve ser true ou false.")


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 1) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} deve ser um inteiro.") from exc
    if value < minimum:
        raise ConfigError(f"{key} deve ser >= {minimum}.")
    return value


@dataclass(frozen=True)
class Config:
    env: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    allowed_origins: Tuple[str, ...]
    cookie_secure: bool
    session_idle_minutes: int
    session_absolute_hours: int
    log_level: str
    migrations_dir: str

    cookie_name: str = "mx_session"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        app_env = _required(env, "APP_ENV").lower()
        if app_env not in _VALID_ENVS:
            raise ConfigError(f"APP_ENV deve ser um de {_VALID_ENVS}.")

        origins = tuple(
            o.strip().rstrip("/") for o in _required(env, "ALLOWED_ORIGINS").split(",") if o.strip()
        )
        if not origins:
            raise ConfigError("ALLOWED_ORIGINS não pode ser vazio.")

        cfg = cls(
            env=app_env,
            db_host=_required(env, "DB_HOST"),
            db_port=_int(env, "DB_PORT", 3306),
            db_name=_required(env, "DB_NAME"),
            db_user=_required(env, "DB_USER"),
            db_password=_required(env, "DB_PASSWORD"),
            allowed_origins=origins,
            cookie_secure=_bool(env, "COOKIE_SECURE", default=True),
            session_idle_minutes=_int(env, "SESSION_IDLE_MINUTES", 120),
            session_absolute_hours=_int(env, "SESSION_ABSOLUTE_HOURS", 12),
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            migrations_dir=env.get("MIGRATIONS_DIR", "/app/migrations"),
        )
        cfg._validate_production()
        return cfg

    def _validate_production(self) -> None:
        if not self.is_production:
            return
        if not self.cookie_secure:
            raise ConfigError("COOKIE_SECURE=false não é permitido em produção.")
        if self.db_password.upper().startswith("CHANGE_ME"):
            raise ConfigError("DB_PASSWORD ainda é o valor de exemplo (CHANGE_ME).")
        for origin in self.allowed_origins:
            if "CHANGE_ME" in origin.upper():
                raise ConfigError("ALLOWED_ORIGINS ainda contém o valor de exemplo.")
            # http só é tolerado para localhost (teste local do modo produção).
            if origin.startswith("http://") and not any(
                h in origin for h in ("//localhost", "//127.0.0.1")
            ):
                raise ConfigError("Em produção, ALLOWED_ORIGINS deve usar https://.")
