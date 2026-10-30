"""Rotas de autenticação: /api/auth/*"""
from flask import Blueprint, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.models import AuthContext
from .errors import RequestValidationError
from .security import clear_session_cookie, login_required, set_session_cookie

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise RequestValidationError([{"field": "body", "message": "Corpo JSON obrigatório."}])
    return data


def _auth_payload(ctx: AuthContext) -> dict:
    u = ctx.user
    return {
        "user": {"id": u.id, "name": u.name, "username": u.username, "role": u.role.value},
        "csrf_token": ctx.csrf_token,
    }


@bp.post("/login")
def login():
    try:
        data = LoginRequest.model_validate(_json_body())
    except ValidationError as exc:
        # Só campo e mensagem: o valor enviado (senha) nunca é devolvido.
        raise RequestValidationError(
            [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
             for e in exc.errors()]
        ) from exc

    from flask import current_app
    container = current_app.extensions["mx_container"]
    token, ctx = container.auth.login(
        data.username, data.password, request.headers.get("User-Agent")
    )
    response = jsonify(_auth_payload(ctx))
    set_session_cookie(response, token)
    return response


@bp.post("/logout")
@login_required()
def logout():
    from flask import current_app
    from .security import _config
    token = request.cookies.get(_config().cookie_name)
    current_app.extensions["mx_container"].auth.logout(token)
    response = jsonify({"ok": True})
    clear_session_cookie(response)
    return response


@bp.get("/me")
@login_required()
def me():
    return jsonify(_auth_payload(g.auth))
