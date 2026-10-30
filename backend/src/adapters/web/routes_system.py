"""Rotas de infraestrutura: health check e verificação de perfil (admin)."""
from flask import Blueprint, current_app, g, jsonify

from ...domain.models import Role
from .security import login_required

bp = Blueprint("system", __name__, url_prefix="/api")


@bp.get("/health")
def health():
    db_ok = current_app.extensions["mx_container"].db.ping()
    body = {"status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "unavailable"}
    return jsonify(body), 200 if db_ok else 503


@bp.get("/admin/ping")
@login_required(Role.ADMIN)
def admin_ping():
    """Rota mínima restrita a admin; valida a autorização por perfil."""
    return jsonify({"ok": True, "user": g.auth.user.username})
