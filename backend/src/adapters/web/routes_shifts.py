"""Rotas de abertura, sangria, suprimento, conferência, fechamento e histórico de turnos: /api/shifts/*"""
from typing import Optional, Union

from flask import Blueprint, current_app, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...use_cases.shifts import serialize_drawer_movement, serialize_shift
from .errors import RequestValidationError
from .security import login_required

bp = Blueprint("shifts", __name__, url_prefix="/api/shifts")


class OpenShiftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening_cash: Union[str, int, float] = Field(default="0.00")
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=255)


class DrawerMovementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Union[str, int, float]
    reason: str = Field(min_length=1, max_length=255)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=64)


class PreviewClosingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    counted_cash: Optional[Union[str, int, float]] = None
    counted_pix: Optional[Union[str, int, float]] = None
    counted_card: Optional[Union[str, int, float]] = None
    discrepancy_justification: Optional[str] = Field(default=None, max_length=500)
    shift_id: Optional[int] = None


class CloseShiftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    counted_cash: Optional[Union[str, int, float]] = None
    counted_pix: Optional[Union[str, int, float]] = None
    counted_card: Optional[Union[str, int, float]] = None
    discrepancy_justification: Optional[str] = Field(default=None, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=255)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=64)
    shift_id: Optional[int] = None


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise RequestValidationError([{"field": "body", "message": "Corpo JSON obrigatório."}])
    return data


def _parse_model(model_cls):
    try:
        return model_cls.model_validate(_json_body())
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                for e in exc.errors()
            ]
        ) from exc


@bp.get("/current")
@login_required()
def get_current_shift():
    try:
        history_limit = int(request.args.get("history_limit", "20"))
    except ValueError:
        history_limit = 20
    container = current_app.extensions["mx_container"]
    state = container.shifts.get_current_shift_state(history_limit=history_limit)
    return jsonify(state)


@bp.get("")
@login_required()
def list_shifts():
    status = request.args.get("status")
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    container = current_app.extensions["mx_container"]
    shifts = container.shifts.list_shifts(status=status, limit=limit)
    return jsonify({"shifts": [serialize_shift(s) for s in shifts]})


@bp.get("/<int:shift_id>")
@bp.get("/<int:shift_id>/report")
@login_required()
def get_shift_report(shift_id: int):
    container = current_app.extensions["mx_container"]
    report = container.shifts.get_shift_report(shift_id=shift_id)
    return jsonify({"report": report})


@bp.post("/open")
@login_required()
def open_shift():
    data = _parse_model(OpenShiftRequest)
    container = current_app.extensions["mx_container"]
    shift, created_now = container.shifts.open_shift(
        actor=g.auth.user,
        opening_cash=data.opening_cash,
        idempotency_key=data.idempotency_key,
        notes=data.notes,
    )
    state = container.shifts.get_current_shift_state()
    status_code = 201 if created_now else 200
    return (
        jsonify(
            {
                "shift": serialize_shift(shift),
                "state": state,
                "idempotent_replay": not created_now,
            }
        ),
        status_code,
    )


@bp.post("/withdrawals")
@bp.post("/sangria")
@login_required()
def record_cash_withdrawal():
    data = _parse_model(DrawerMovementRequest)
    container = current_app.extensions["mx_container"]
    movement, created_now = container.shifts.record_cash_withdrawal(
        actor=g.auth.user,
        amount=data.amount,
        reason=data.reason,
        idempotency_key=data.idempotency_key,
    )
    state = container.shifts.get_current_shift_state()
    status_code = 201 if created_now else 200
    return (
        jsonify(
            {
                "movement": serialize_drawer_movement(movement),
                "state": state,
                "idempotent_replay": not created_now,
            }
        ),
        status_code,
    )


@bp.post("/supplies")
@bp.post("/suprimento")
@login_required()
def record_cash_supply():
    data = _parse_model(DrawerMovementRequest)
    container = current_app.extensions["mx_container"]
    movement, created_now = container.shifts.record_cash_supply(
        actor=g.auth.user,
        amount=data.amount,
        reason=data.reason,
        idempotency_key=data.idempotency_key,
    )
    state = container.shifts.get_current_shift_state()
    status_code = 201 if created_now else 200
    return (
        jsonify(
            {
                "movement": serialize_drawer_movement(movement),
                "state": state,
                "idempotent_replay": not created_now,
            }
        ),
        status_code,
    )


@bp.post("/preview-closing")
@login_required()
def preview_closing():
    data = _parse_model(PreviewClosingRequest)
    container = current_app.extensions["mx_container"]
    preview = container.shifts.preview_closing(
        counted_cash=data.counted_cash,
        counted_pix=data.counted_pix,
        counted_card=data.counted_card,
        discrepancy_justification=data.discrepancy_justification,
        shift_id=data.shift_id,
    )
    return jsonify({"preview": preview})


@bp.post("/close")
@login_required()
def close_current_shift():
    data = _parse_model(CloseShiftRequest)
    container = current_app.extensions["mx_container"]
    shift, report, closed_now = container.shifts.close_shift(
        actor=g.auth.user,
        counted_cash=data.counted_cash,
        counted_pix=data.counted_pix,
        counted_card=data.counted_card,
        discrepancy_justification=data.discrepancy_justification,
        notes=data.notes,
        idempotency_key=data.idempotency_key,
        shift_id=data.shift_id,
    )
    return jsonify(
        {
            "shift": serialize_shift(shift),
            "report": report,
            "idempotent_replay": not closed_now,
        }
    )


@bp.post("/<int:shift_id>/close")
@login_required()
def close_shift_by_id(shift_id: int):
    data = _parse_model(CloseShiftRequest)
    container = current_app.extensions["mx_container"]
    shift, report, closed_now = container.shifts.close_shift(
        actor=g.auth.user,
        counted_cash=data.counted_cash,
        counted_pix=data.counted_pix,
        counted_card=data.counted_card,
        discrepancy_justification=data.discrepancy_justification,
        notes=data.notes,
        idempotency_key=data.idempotency_key,
        shift_id=shift_id,
    )
    return jsonify(
        {
            "shift": serialize_shift(shift),
            "report": report,
            "idempotent_replay": not closed_now,
        }
    )


@bp.post("/<int:shift_id>/reopen")
@login_required()
def reopen_shift(shift_id: int):
    container = current_app.extensions["mx_container"]
    container.shifts.reopen_shift(actor=g.auth.user, shift_id=shift_id)
    return jsonify({})
