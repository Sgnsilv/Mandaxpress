"""Rotas de configuração de pagamentos (taxa de cartão): /api/settings/payment"""
from flask import Blueprint, current_app, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.models import PaymentSettings, Role
from .errors import RequestValidationError
from .security import login_required

bp = Blueprint("settings", __name__, url_prefix="/api/settings")


class UpdatePaymentSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_fee_percent: str = Field(min_length=1, max_length=10)
    card_fee_mode: str = Field(default="simple_addition", max_length=30)


def _serialize_settings(s: PaymentSettings) -> dict:
    return {
        "card_fee_percent": f"{s.card_fee_percent:.2f}",
        "card_fee_mode": s.card_fee_mode.value,
        "updated_by": s.updated_by,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@bp.get("/payment")
@login_required()
def get_payment_settings():
    container = current_app.extensions["mx_container"]
    settings = container.settings.get_settings()
    return jsonify({"settings": _serialize_settings(settings)})


@bp.put("/payment")
@login_required(Role.ADMIN)
def update_payment_settings():
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        raise RequestValidationError([{"field": "body", "message": "Corpo JSON obrigatório."}])
    try:
        data = UpdatePaymentSettingsRequest.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
             for e in exc.errors()]
        ) from exc

    container = current_app.extensions["mx_container"]
    settings = container.settings.update_settings(
        actor=g.auth.user,
        card_fee_percent=data.card_fee_percent,
        card_fee_mode=data.card_fee_mode,
    )
    return jsonify({"settings": _serialize_settings(settings)})
