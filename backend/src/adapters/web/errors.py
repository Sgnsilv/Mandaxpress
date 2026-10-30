"""Erros HTTP e tratamento centralizado (respostas JSON uniformes)."""
import logging

import pymysql
from flask import Flask, g, jsonify
from werkzeug.exceptions import HTTPException

from ...domain.errors import (
    DomainError,
    InvalidCredentialsError,
    InvalidInputError,
    NotAuthenticatedError,
    PermissionDeniedError,
    UserAlreadyExistsError,
    UserNotFoundError,
    WeakPasswordError,
)

logger = logging.getLogger("mandaxpress.web")


class CsrfError(Exception):
    code = "csrf_failed"

    def __init__(self, message: str = "Requisição recusada."):
        super().__init__(message)
        self.message = message


class RequestValidationError(Exception):
    def __init__(self, details):
        super().__init__("invalid_request")
        self.details = details


_DOMAIN_STATUS = {
    InvalidCredentialsError: 401,
    NotAuthenticatedError: 401,
    PermissionDeniedError: 403,
    WeakPasswordError: 400,
    InvalidInputError: 400,
    UserAlreadyExistsError: 409,
    UserNotFoundError: 404,
}


def _error(status: int, code: str, message: str, details=None):
    body = {"error": {"code": code, "message": message,
                      "request_id": getattr(g, "request_id", None)}}
    if details is not None:
        body["error"]["details"] = details
    response = jsonify(body)
    response.status_code = status
    return response


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(DomainError)
    def handle_domain(exc: DomainError):
        response = _error(_DOMAIN_STATUS.get(type(exc), 400), exc.code, exc.message)
        if isinstance(exc, NotAuthenticatedError):
            # Cookie inválido/expirado: limpa para o navegador não reenviá-lo.
            from .security import clear_session_cookie
            clear_session_cookie(response)
        return response

    @app.errorhandler(CsrfError)
    def handle_csrf(exc: CsrfError):
        logger.warning("CSRF recusado: %s", exc.message)
        return _error(403, exc.code, exc.message)

    @app.errorhandler(RequestValidationError)
    def handle_validation(exc: RequestValidationError):
        return _error(400, "invalid_request", "Dados inválidos.", exc.details)

    @app.errorhandler(HTTPException)
    def handle_http(exc: HTTPException):
        return _error(exc.code or 500, (exc.name or "error").lower().replace(" ", "_"),
                      exc.description or exc.name)

    @app.errorhandler(pymysql.err.OperationalError)
    def handle_db_unavailable(exc):
        logger.error("Banco indisponível: %s", exc.__class__.__name__)
        return _error(503, "service_unavailable", "Serviço temporariamente indisponível.")

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # Stack trace só no log do servidor, nunca na resposta.
        logger.exception("Erro interno não tratado")
        return _error(500, "internal_error", "Erro interno. Informe o código da requisição.")
