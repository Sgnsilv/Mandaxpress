"""Rotas de venda rápida, histórico, resumo financeiro e estornos: /api/sales/*"""
from typing import List, Optional

from flask import Blueprint, current_app, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.models import CashMovement, Role, Sale, SaleItem
from ...use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput
from .errors import RequestValidationError
from .security import login_required

bp = Blueprint("sales", __name__, url_prefix="/api/sales")


class SaleItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: Optional[int] = None
    quantity: int = Field(default=1, ge=1, le=1000)
    custom_description: Optional[str] = Field(default=None, max_length=150)
    custom_amount: Optional[str] = Field(default=None, max_length=20)


class SalePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(min_length=1, max_length=20)
    amount: str = Field(min_length=1, max_length=20)
    cash_tendered: Optional[str] = Field(default=None, max_length=20)


class CreateQuickSaleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=64)
    items: List[SaleItemRequest] = Field(min_length=1, max_length=100)
    payments: List[SalePaymentRequest] = Field(min_length=1, max_length=20)


class VoidSaleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=255)
    customer_refund_completed: bool = False
    customer_refund_method: Optional[str] = Field(default=None, max_length=20)
    customer_refund_notes: Optional[str] = Field(default=None, max_length=255)


class ConfirmCustomerRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(min_length=1, max_length=20)
    notes: Optional[str] = Field(default=None, max_length=255)


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
            [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
             for e in exc.errors()]
        ) from exc


def _serialize_item(it: SaleItem) -> dict:
    return {
        "id": it.id,
        "product_id": it.product_id,
        "item_kind": it.item_kind.value,
        "description": it.description,
        "quantity": it.quantity,
        "unit_price": f"{it.unit_price:.2f}",
        "line_total": f"{it.line_total:.2f}",
    }


def _serialize_movement(m: CashMovement) -> dict:
    return {
        "id": m.id,
        "tab_id": m.tab_id,
        "sale_id": m.sale_id,
        "kind": m.kind.value,
        "method": m.method.value,
        "direction": m.direction.value,
        "amount": f"{m.amount:.2f}",
        "fee_percent": f"{m.fee_percent:.2f}",
        "fee_mode": m.fee_mode,
        "fee_amount": f"{m.fee_amount:.2f}",
        "gross_amount": f"{m.gross_amount:.2f}",
        "cash_tendered": f"{m.cash_tendered:.2f}" if m.cash_tendered is not None else None,
        "change_given": f"{m.change_given:.2f}" if m.change_given is not None else None,
        "reverses_id": m.reverses_id,
        "operator_id": m.operator_id,
        "created_at": m.created_at.isoformat(),
        "idempotency_key": m.idempotency_key,
        "batch_key": m.batch_key,
        "note": m.note,
        "voided_by": m.voided_by,
        "voided_at": m.voided_at.isoformat() if m.voided_at else None,
        "void_reason": m.void_reason,
        "customer_refund_status": m.customer_refund_status.value,
        "customer_refund_amount": f"{m.customer_refund_amount:.2f}",
        "customer_refunded_at": m.customer_refunded_at.isoformat() if m.customer_refunded_at else None,
        "customer_refunded_by": m.customer_refunded_by,
        "customer_refund_method": m.customer_refund_method.value if m.customer_refund_method else None,
        "customer_refund_notes": m.customer_refund_notes,
    }


def _serialize_sale(s: Sale) -> dict:
    return {
        "id": s.id,
        "shift_id": s.shift_id,
        "tab_id": s.tab_id,
        "kind": s.kind.value,
        "status": s.status.value,
        "subtotal": f"{s.subtotal:.2f}",
        "card_fee_total": f"{s.card_fee_total:.2f}",
        "total_charged": f"{s.total_charged:.2f}",
        "operator_id": s.operator_id,
        "created_at": s.created_at.isoformat(),
        "idempotency_key": s.idempotency_key,
        "voided_by": s.voided_by,
        "voided_at": s.voided_at.isoformat() if s.voided_at else None,
        "void_reason": s.void_reason,
        "customer_refund_status": s.customer_refund_status.value,
        "customer_refund_amount": f"{s.customer_refund_amount:.2f}",
        "customer_refunded_at": s.customer_refunded_at.isoformat() if s.customer_refunded_at else None,
        "customer_refunded_by": s.customer_refunded_by,
        "customer_refund_method": s.customer_refund_method.value if s.customer_refund_method else None,
        "customer_refund_notes": s.customer_refund_notes,
        "items": [_serialize_item(i) for i in s.items],
        "payments": [_serialize_movement(p) for p in s.payments],
    }


@bp.post("/quick")
@login_required()
def create_quick_sale():
    data = _parse_model(CreateQuickSaleRequest)
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
    payments = [
        QuickSalePaymentInput(
            method=pay.method,
            amount=pay.amount,
            cash_tendered=pay.cash_tendered,
        )
        for pay in data.payments
    ]

    sale, created_now = container.sales.confirm_quick_sale(
        actor=g.auth.user,
        idempotency_key=data.idempotency_key,
        items=items,
        payments=payments,
    )
    status_code = 201 if created_now else 200
    return jsonify({"sale": _serialize_sale(sale), "idempotent_replay": not created_now}), status_code


@bp.get("")
@login_required()
def list_sales():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    raw_shift_id = request.args.get("shift_id")
    shift_id: Optional[int] = None
    if raw_shift_id is not None and raw_shift_id.strip():
        try:
            shift_id = int(raw_shift_id)
        except ValueError:
            shift_id = None
    container = current_app.extensions["mx_container"]
    sales = container.sales.list_recent_sales(limit=limit, shift_id=shift_id)
    summary = container.sales.get_financial_summary(shift_id=shift_id)
    return jsonify({
        "sales": [_serialize_sale(s) for s in sales],
        "summary": summary,
    })


@bp.get("/summary")
@login_required()
def get_sales_summary():
    raw_shift_id = request.args.get("shift_id")
    shift_id: Optional[int] = None
    if raw_shift_id is not None and raw_shift_id.strip():
        try:
            shift_id = int(raw_shift_id)
        except ValueError:
            shift_id = None
    container = current_app.extensions["mx_container"]
    summary = container.sales.get_financial_summary(shift_id=shift_id)
    return jsonify({"summary": summary})


@bp.get("/<int:sale_id>")
@login_required()
def get_sale(sale_id: int):
    container = current_app.extensions["mx_container"]
    sale = container.sales.get_sale(sale_id)
    return jsonify({"sale": _serialize_sale(sale)})


@bp.post("/<int:sale_id>/void")
@login_required(Role.ADMIN)
def void_sale(sale_id: int):
    data = _parse_model(VoidSaleRequest)
    container = current_app.extensions["mx_container"]
    sale = container.sales.void_quick_sale(
        actor=g.auth.user,
        sale_id=sale_id,
        reason=data.reason,
        customer_refund_completed=data.customer_refund_completed,
        customer_refund_method=data.customer_refund_method,
        customer_refund_notes=data.customer_refund_notes,
    )
    return jsonify({"sale": _serialize_sale(sale)})


@bp.post("/<int:sale_id>/customer-refund")
@login_required(Role.ADMIN)
def confirm_customer_refund(sale_id: int):
    data = _parse_model(ConfirmCustomerRefundRequest)
    container = current_app.extensions["mx_container"]
    sale = container.sales.confirm_customer_refund(
        actor=g.auth.user,
        sale_id=sale_id,
        method=data.method,
        notes=data.notes,
    )
    return jsonify({"sale": _serialize_sale(sale)})
