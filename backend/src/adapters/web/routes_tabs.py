"""Rotas de comandas abertas, lançamentos incrementais, pagamentos parciais, crédito antecipado e encerramento: /api/tabs/*"""
from typing import List, Optional, Union

from flask import Blueprint, current_app, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.models import Role, Tab
from ...use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput
from .errors import RequestValidationError
from .routes_sales import (
    ConfirmCustomerRefundRequest,
    SaleItemRequest,
    SalePaymentRequest,
    VoidSaleRequest,
    _serialize_movement,
    _serialize_sale,
)
from .security import login_required

bp = Blueprint("tabs", __name__, url_prefix="/api/tabs")


class OpenTabRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: Union[int, str]
    customer_name: Optional[str] = Field(default=None, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=255)


class AddTabItemsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=64)
    items: List[SaleItemRequest] = Field(min_length=1, max_length=100)


class RecordTabPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=64)
    payments: List[SalePaymentRequest] = Field(min_length=1, max_length=20)
    note: Optional[str] = Field(default=None, max_length=255)


class RecordTabCreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=64)
    payments: Optional[List[SalePaymentRequest]] = Field(default=None, min_length=1, max_length=20)
    method: Optional[str] = Field(default=None, min_length=1, max_length=16)
    amount: Optional[Union[str, int, float]] = None
    cash_tendered: Optional[Union[str, int, float]] = None
    note: Optional[str] = Field(default=None, max_length=255)


class RefundTabCreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=64)
    method: str = Field(min_length=1, max_length=16)
    amount: Union[str, int, float]
    note: Optional[str] = Field(default=None, max_length=255)


class VoidTabCreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=255)
    customer_refund_completed: bool = False
    customer_refund_method: Optional[str] = Field(default=None, max_length=16)
    customer_refund_notes: Optional[str] = Field(default=None, max_length=255)
    refund_method: Optional[str] = Field(default=None, max_length=16)
    notes: Optional[str] = Field(default=None, max_length=255)


class CloseTabRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: Optional[str] = Field(default=None, max_length=255)


def _json_body(allow_empty: bool = False) -> dict:
    if allow_empty and not request.get_data():
        return {}
    data = request.get_json(silent=True)
    if data is None and allow_empty:
        return {}
    if not isinstance(data, dict):
        raise RequestValidationError([{"field": "body", "message": "Corpo JSON obrigatório."}])
    return data


def _parse_model(model_cls, allow_empty: bool = False):
    try:
        return model_cls.model_validate(_json_body(allow_empty=allow_empty))
    except ValidationError as exc:
        raise RequestValidationError(
            [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
             for e in exc.errors()]
        ) from exc


def _serialize_tab(t: Tab) -> dict:
    return {
        "id": t.id,
        "shift_id": t.shift_id,
        "number": t.number,
        "customer_name": t.customer_name,
        "status": t.status.value,
        "opened_by": t.opened_by,
        "opened_at": t.opened_at.isoformat(),
        "closed_by": t.closed_by,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "notes": t.notes,
        "subtotal_consumed": f"{t.subtotal_consumed:.2f}",
        "voided_subtotal": f"{t.voided_subtotal:.2f}",
        "principal_paid": f"{t.principal_paid:.2f}",
        "net_principal_paid": f"{t.net_principal_paid:.2f}",
        "credit_deposited": f"{t.credit_deposited:.2f}",
        "credit_card_fee_paid": f"{t.credit_card_fee_paid:.2f}",
        "credit_applied": f"{t.credit_applied:.2f}",
        "credit_refunded": f"{t.credit_refunded:.2f}",
        "credit_balance": f"{t.credit_balance:.2f}",
        "sale_card_fee_paid": f"{t.sale_card_fee_paid:.2f}",
        "card_fee_paid": f"{t.card_fee_paid:.2f}",
        "total_charged_paid": f"{t.total_charged_paid:.2f}",
        "settled_subtotal": f"{t.settled_subtotal:.2f}",
        "balance_due": f"{t.balance_due:.2f}",
        "pending_refund_total": f"{t.pending_refund_total:.2f}",
        "completed_refund_total": f"{t.completed_refund_total:.2f}",
        "can_close": t.can_close,
        "close_blockers": t.close_blockers,
        "launches": [_serialize_sale(s) for s in t.launches],
        "payments": [_serialize_movement(m) for m in t.payments],
        "credit_deposits": [_serialize_movement(m) for m in t.credit_deposits],
        "credit_refunds": [_serialize_movement(m) for m in t.credit_refunds],
    }


@bp.post("")
@login_required()
def open_tab():
    data = _parse_model(OpenTabRequest)
    container = current_app.extensions["mx_container"]
    tab = container.tabs.open_tab(
        actor=g.auth.user,
        number=data.number,
        customer_name=data.customer_name,
        notes=data.notes,
    )
    return jsonify({"tab": _serialize_tab(tab)}), 201


@bp.get("")
@login_required()
def list_tabs():
    status = request.args.get("status")
    number = request.args.get("number")
    search = request.args.get("search")
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100

    container = current_app.extensions["mx_container"]
    tabs = container.tabs.list_tabs(
        status=status,
        number=number,
        search=search,
        limit=limit,
    )
    return jsonify({"tabs": [_serialize_tab(t) for t in tabs]})


@bp.get("/by-number/<number>")
@login_required()
def get_tabs_by_number(number: str):
    include_closed = request.args.get("include_closed", "true").lower() not in ("0", "false", "no")
    container = current_app.extensions["mx_container"]
    open_tab, history = container.tabs.get_tabs_by_number(
        number=number,
        include_closed=include_closed,
    )
    return jsonify({
        "open_tab": _serialize_tab(open_tab) if open_tab else None,
        "history": [_serialize_tab(t) for t in history],
    })


@bp.get("/<int:tab_id>")
@login_required()
def get_tab(tab_id: int):
    container = current_app.extensions["mx_container"]
    tab = container.tabs.get_tab(tab_id)
    return jsonify({"tab": _serialize_tab(tab)})


@bp.post("/<int:tab_id>/items")
@login_required()
def add_tab_items(tab_id: int):
    data = _parse_model(AddTabItemsRequest)
    container = current_app.extensions["mx_container"]

    items = [
        QuickSaleItemInput(
            product_id=it.product_id,
            quantity=it.quantity,
            custom_description=it.custom_description,
            custom_amount=it.custom_amount,
        )
        for it in data.items
    ]

    tab, launch, created_now = container.tabs.add_items(
        actor=g.auth.user,
        tab_id=tab_id,
        idempotency_key=data.idempotency_key,
        items=items,
    )
    status_code = 201 if created_now else 200
    return jsonify({
        "tab": _serialize_tab(tab),
        "launch": _serialize_sale(launch),
        "idempotent_replay": not created_now,
    }), status_code


@bp.post("/<int:tab_id>/payments")
@login_required()
def record_tab_payment(tab_id: int):
    data = _parse_model(RecordTabPaymentRequest)
    container = current_app.extensions["mx_container"]

    payments = [
        QuickSalePaymentInput(
            method=pay.method,
            amount=pay.amount,
            cash_tendered=pay.cash_tendered,
        )
        for pay in data.payments
    ]

    tab, movements, created_now = container.tabs.record_payment(
        actor=g.auth.user,
        tab_id=tab_id,
        idempotency_key=data.idempotency_key,
        payments=payments,
        note=data.note,
    )
    status_code = 201 if created_now else 200
    return jsonify({
        "tab": _serialize_tab(tab),
        "payments": [_serialize_movement(m) for m in movements],
        "idempotent_replay": not created_now,
    }), status_code


@bp.post("/<int:tab_id>/credits")
@login_required()
def record_tab_credit(tab_id: int):
    data = _parse_model(RecordTabCreditRequest)
    container = current_app.extensions["mx_container"]

    if data.payments is not None:
        payments = [
            QuickSalePaymentInput(
                method=pay.method,
                amount=pay.amount,
                cash_tendered=pay.cash_tendered,
            )
            for pay in data.payments
        ]
    elif data.method is not None and data.amount is not None:
        payments = [
            QuickSalePaymentInput(
                method=data.method,
                amount=data.amount,
                cash_tendered=data.cash_tendered,
            )
        ]
    else:
        raise RequestValidationError([
            {"field": "payments", "message": "Informe 'payments' ou 'method' + 'amount'."}
        ])

    tab, movements, created_now = container.tabs.record_credit_deposit(
        actor=g.auth.user,
        tab_id=tab_id,
        idempotency_key=data.idempotency_key,
        payments=payments,
        note=data.note,
    )
    status_code = 201 if created_now else 200
    return jsonify({
        "tab": _serialize_tab(tab),
        "credit_deposits": [_serialize_movement(m) for m in movements],
        "idempotent_replay": not created_now,
    }), status_code


@bp.post("/<int:tab_id>/credit-refunds")
@login_required()
def refund_tab_credit(tab_id: int):
    data = _parse_model(RefundTabCreditRequest)
    container = current_app.extensions["mx_container"]
    tab, movement, created_now = container.tabs.refund_credit(
        actor=g.auth.user,
        tab_id=tab_id,
        idempotency_key=data.idempotency_key,
        method=data.method,
        amount=data.amount,
        note=data.note,
    )
    status_code = 201 if created_now else 200
    return jsonify({
        "tab": _serialize_tab(tab),
        "credit_refund": _serialize_movement(movement),
        "idempotent_replay": not created_now,
    }), status_code


@bp.post("/<int:tab_id>/credits/<int:movement_id>/void")
@login_required(Role.ADMIN)
def void_tab_credit(tab_id: int, movement_id: int):
    data = _parse_model(VoidTabCreditRequest)
    container = current_app.extensions["mx_container"]
    tab, reversal = container.tabs.void_credit_deposit(
        actor=g.auth.user,
        tab_id=tab_id,
        movement_id=movement_id,
        reason=data.reason,
        customer_refund_completed=data.customer_refund_completed,
        customer_refund_method=data.customer_refund_method,
        customer_refund_notes=data.customer_refund_notes,
        refund_method=data.refund_method,
        notes=data.notes,
    )
    return jsonify({
        "tab": _serialize_tab(tab),
        "reversal": _serialize_movement(reversal) if reversal else None,
    })


@bp.post("/<int:tab_id>/credits/<int:movement_id>/customer-refund")
@login_required(Role.ADMIN)
def confirm_tab_credit_refund(tab_id: int, movement_id: int):
    data = _parse_model(ConfirmCustomerRefundRequest)
    container = current_app.extensions["mx_container"]
    tab, reversal = container.tabs.confirm_credit_deposit_refund(
        actor=g.auth.user,
        tab_id=tab_id,
        movement_id=movement_id,
        method=data.method,
        notes=data.notes,
    )
    return jsonify({
        "tab": _serialize_tab(tab),
        "reversal": _serialize_movement(reversal),
    })


@bp.post("/<int:tab_id>/close")
@login_required()
def close_tab(tab_id: int):
    data = _parse_model(CloseTabRequest, allow_empty=True)
    container = current_app.extensions["mx_container"]
    tab = container.tabs.close_tab(
        actor=g.auth.user,
        tab_id=tab_id,
        notes=data.notes,
    )
    return jsonify({"tab": _serialize_tab(tab)})


@bp.post("/<int:tab_id>/items/<int:sale_id>/void")
@login_required(Role.ADMIN)
def void_tab_item(tab_id: int, sale_id: int):
    data = _parse_model(VoidSaleRequest)
    container = current_app.extensions["mx_container"]
    tab, launch = container.tabs.void_tab_item(
        actor=g.auth.user,
        tab_id=tab_id,
        sale_id=sale_id,
        reason=data.reason,
        customer_refund_completed=data.customer_refund_completed,
        customer_refund_method=data.customer_refund_method,
        customer_refund_notes=data.customer_refund_notes,
    )
    return jsonify({
        "tab": _serialize_tab(tab),
        "launch": _serialize_sale(launch),
    })


@bp.post("/<int:tab_id>/items/<int:sale_id>/customer-refund")
@login_required(Role.ADMIN)
def confirm_tab_customer_refund(tab_id: int, sale_id: int):
    data = _parse_model(ConfirmCustomerRefundRequest)
    container = current_app.extensions["mx_container"]
    tab, launch = container.tabs.confirm_tab_customer_refund(
        actor=g.auth.user,
        tab_id=tab_id,
        sale_id=sale_id,
        method=data.method,
        notes=data.notes,
    )
    return jsonify({
        "tab": _serialize_tab(tab),
        "launch": _serialize_sale(launch),
    })
