"""Cookies de sessão, verificação de origem, CSRF e autorização por perfil."""
import functools
import hmac
from typing import Optional

from flask import Response, current_app, g, request

from ...domain.errors import NotAuthenticatedError
from ...domain.models import Role
from .errors import CsrfError

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"


def _config():
    return current_app.extensions.get("mx_config") or current_app.extensions["mx_container"].config


def _auth():
    return current_app.extensions["mx_container"].auth


def set_session_cookie(response: Response, token: str) -> None:
    cfg = _config()
    response.set_cookie(
        cfg.cookie_name,
        token,
        max_age=cfg.session_absolute_hours * 3600,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="Strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    cfg = _config()
    response.set_cookie(
        cfg.cookie_name, "", max_age=0, expires=0,
        httponly=True, secure=cfg.cookie_secure, samesite="Strict", path="/",
    )


def check_origin() -> None:
    """Rejeita requisições que alteram dados vindas de outra origem.

    Navegadores sempre enviam Origin em POST/PUT/PATCH/DELETE. Se presente,
    deve estar na lista permitida. Clientes sem Origin (curl, testes) passam
    aqui, mas ainda precisam do token CSRF nas rotas autenticadas.
    """
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("Origin")
    if origin is not None:
        if origin.rstrip("/") not in _config().allowed_origins:
            raise CsrfError("Origem não autorizada.")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise CsrfError("Requisição entre sites bloqueada.")


def _check_csrf_token(expected: str) -> None:
    sent = request.headers.get(CSRF_HEADER, "")
    if not sent or not hmac.compare_digest(sent.encode(), expected.encode()):
        raise CsrfError("Token CSRF ausente ou inválido.")


def login_required(*roles: Role):
    """Exige sessão válida (e, se informado, um dos perfis).

    Em métodos que alteram dados, exige também o cabeçalho X-CSRF-Token.
    O contexto fica em `g.auth`.
    """
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            token: Optional[str] = request.cookies.get(_config().cookie_name)
            ctx = _auth().authenticate(token)  # NotAuthenticatedError -> 401
            if request.method not in SAFE_METHODS:
                _check_csrf_token(ctx.csrf_token)
            _auth().require_role(ctx, *roles)  # PermissionDeniedError -> 403
            g.auth = ctx
            return view(*args, **kwargs)
        return wrapper
    return decorator


__all__ = ["login_required", "check_origin", "set_session_cookie",
           "clear_session_cookie", "NotAuthenticatedError"]
