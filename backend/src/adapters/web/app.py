"""Fábrica da aplicação Flask."""
import logging
import os
import uuid
from typing import Optional

from flask import Flask, g, request

from ...config import Config
from .container import Container, build_container
from . import routes_auth, routes_system
from .errors import register_error_handlers
from .security import check_origin


def create_app(config: Optional[Config] = None, container: Optional[Container] = None) -> Flask:
    config = config or Config.from_env(os.environ)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    app.extensions["mx_container"] = container or build_container(config)

    @app.before_request
    def _prepare_request():
        g.request_id = uuid.uuid4().hex[:12]
        check_origin()

    @app.after_request
    def _security_headers(response):
        response.headers["X-Request-ID"] = g.get("request_id", "-")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(routes_system.bp)
    app.register_blueprint(routes_auth.bp)
    register_error_handlers(app)
    return app
