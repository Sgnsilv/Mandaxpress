"""Casos de uso de venda rápida, pagamentos, idempotência e estornos."""
import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple, Union

import pymysql

from ..domain.errors import (
    IdempotencyConflictError,
    InactiveProductError,
    InvalidInputError,
    InvalidPaymentError,
    PermissionDeniedError,
    ProductNotFoundError,
    SaleAlreadyVoidedError,
    SaleNotFoundError,
    ShiftClosedError,
)
from ..domain.finance import (
    ZERO,
    build_payment_line_preview,
    parse_money,
)
from ..domain.models import (
    CashDirection,
    CashMovementKind,
    CustomerRefundStatus,
    PaymentLinePreview,
    PaymentMethod,
    Role,
    Sale,
    SaleItemKind,
    SaleKind,
    SaleStatus,
    ShiftStatus,
    User,
)
from ..domain.ports import Clock, UnitOfWork

_DUPLICATE_ENTRY = 1062
_LOCK_DEADLOCK = 1213
_LOCK_WAIT_TIMEOUT = 1205


@dataclass(frozen=True)
class QuickSaleItemInput:
    product_id: Optional[int] = None
    quantity: int = 1
    custom_description: Optional[str] = None
    custom_amount: Optional[Union[str, int, Decimal]] = None


@dataclass(frozen=True)
class QuickSalePaymentInput:
    method: Union[str, PaymentMethod]
    amount: Union[str, int, Decimal]
    cash_tendered: Optional[Union[str, int, Decimal]] = None


def _validate_idempotency_key(key: str) -> str:
    if not isinstance(key, str):
        raise InvalidInputError("A chave de idempotência é obrigatória.")
    cleaned = key.strip()
    if len(cleaned) < 8 or len(cleaned) > 64:
        raise InvalidInputError("A chave de idempotência deve ter entre 8 e 64 caracteres.")
    return cleaned


def _validate_quantity(qty: int) -> int:
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1 or qty > 1000:
        raise InvalidInputError("A quantidade deve ser um número inteiro entre 1 e 1000.")
    return qty


def _validate_custom_description(desc: Optional[str]) -> str:
    if not isinstance(desc, str):
        raise InvalidInputError("A descrição do item avulso é obrigatória.")
    cleaned = " ".join(desc.strip().split())
    if len(cleaned) < 2 or len(cleaned) > 150:
        raise InvalidInputError("A descrição do item avulso deve ter entre 2 e 150 caracteres.")
    return cleaned


def _normalize_and_fingerprint(
    items: Sequence[QuickSaleItemInput],
    payments: Sequence[QuickSalePaymentInput],
) -> Tuple[List[dict], List[dict], str]:
    if not items:
        raise InvalidInputError("A venda deve conter pelo menos um item.")
    if not payments:
        raise InvalidPaymentError("A venda deve conter pelo menos uma forma de pagamento.")

    normalized_items = []
    for raw_item in items:
        qty = _validate_quantity(raw_item.quantity)
        has_product = raw_item.product_id is not None
        has_custom = raw_item.custom_description is not None or raw_item.custom_amount is not None
        if has_product and has_custom:
            raise InvalidInputError("Um item não pode ser simultaneamente produto cadastrado e item avulso.")
        if not has_product and not has_custom:
            raise InvalidInputError("Cada item deve informar um product_id ou descrição/valor avulso.")

        if has_product:
            if isinstance(raw_item.product_id, bool) or not isinstance(raw_item.product_id, int) or raw_item.product_id < 1:
                raise InvalidInputError("Identificador de produto inválido.")
            normalized_items.append({
                "kind": SaleItemKind.PRODUCT.value,
                "product_id": raw_item.product_id,
                "quantity": qty,
            })
        else:
            desc = _validate_custom_description(raw_item.custom_description)
            unit_price = parse_money(raw_item.custom_amount, field_name="valor do item avulso", allow_zero=False)
            normalized_items.append({
                "kind": SaleItemKind.CUSTOM.value,
                "description": desc,
                "unit_price": str(unit_price),
                "quantity": qty,
            })

    normalized_payments = []
    for raw_pay in payments:
        try:
            method = raw_pay.method if isinstance(raw_pay.method, PaymentMethod) else PaymentMethod(str(raw_pay.method))
        except ValueError as exc:
            raise InvalidPaymentError(f"Forma de pagamento inválida: {raw_pay.method}") from exc

        amount = parse_money(raw_pay.amount, field_name="valor do pagamento", allow_zero=False)
        tendered = (
            parse_money(raw_pay.cash_tendered, field_name="valor entregue em dinheiro", allow_zero=False)
            if raw_pay.cash_tendered is not None
            else None
        )
        normalized_payments.append({
            "method": method.value,
            "amount": str(amount),
            "cash_tendered": str(tendered) if tendered is not None else None,
        })

    canonical = json.dumps(
        {"items": normalized_items, "payments": normalized_payments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return normalized_items, normalized_payments, fingerprint


class QuickSaleService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock):
        self._uow = uow_factory
        self._clock = clock

    def confirm_quick_sale(
        self,
        actor: User,
        idempotency_key: str,
        items: Sequence[QuickSaleItemInput],
        payments: Sequence[QuickSalePaymentInput],
    ) -> Tuple[Sale, bool]:
        """Confirma uma venda rápida de forma atômica e idempotente.

        Retorna `(Sale, created_now)`. Se `created_now` for `False`, trata-se
        de um reenvio idempotente com a mesma chave e mesmo payload.
        """
        clean_key = _validate_idempotency_key(idempotency_key)
        norm_items, norm_payments, fingerprint = _normalize_and_fingerprint(items, payments)

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                return self._execute_confirm_quick_sale(
                    actor=actor,
                    clean_key=clean_key,
                    norm_items=norm_items,
                    norm_payments=norm_payments,
                    fingerprint=fingerprint,
                )
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                    with self._uow() as uow:
                        existing = uow.sales.get_by_idempotency_key(clean_key)
                        if existing is not None:
                            if existing.request_fingerprint != fingerprint:
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                                ) from exc
                            return existing, False
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir a confirmação da venda após novas tentativas.")

    def _execute_confirm_quick_sale(
        self,
        actor: User,
        clean_key: str,
        norm_items: List[dict],
        norm_payments: List[dict],
        fingerprint: str,
    ) -> Tuple[Sale, bool]:
        with self._uow() as uow:
            existing = uow.sales.get_by_idempotency_key(clean_key, for_update=True)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError(
                        "Esta chave de idempotência já foi utilizada com dados diferentes."
                    )
                return existing, False

            now = self._clock.now()
            shift = uow.shifts.get_open_shift(for_update=True)
            if shift is None:
                shift = uow.shifts.create_open_shift(opened_by=actor.id, now=now)

            settings = uow.payment_settings.get(for_update=True)

            product_ids = [
                it["product_id"] for it in norm_items if it["kind"] == SaleItemKind.PRODUCT.value
            ]
            products_map = uow.products.get_by_ids(product_ids, for_update=True)

            resolved_items = []
            subtotal = ZERO
            for it in norm_items:
                qty = it["quantity"]
                if it["kind"] == SaleItemKind.PRODUCT.value:
                    pid = it["product_id"]
                    product = products_map.get(pid)
                    if product is None:
                        raise ProductNotFoundError(f"Produto #{pid} não encontrado.")
                    if not product.is_active:
                        raise InactiveProductError(
                            f"O produto '{product.name}' está inativo e não pode ser vendido."
                        )
                    unit_price = product.price
                    line_total = unit_price * qty
                    subtotal += line_total
                    resolved_items.append({
                        "product_id": pid,
                        "item_kind": SaleItemKind.PRODUCT,
                        "description": product.name,
                        "quantity": qty,
                        "unit_price": unit_price,
                        "line_total": line_total,
                    })
                else:
                    unit_price = Decimal(it["unit_price"])
                    line_total = unit_price * qty
                    subtotal += line_total
                    resolved_items.append({
                        "product_id": None,
                        "item_kind": SaleItemKind.CUSTOM,
                        "description": it["description"],
                        "quantity": qty,
                        "unit_price": unit_price,
                        "line_total": line_total,
                    })

            if subtotal <= ZERO:
                raise InvalidInputError("O subtotal da venda deve ser maior que R$ 0,00.")

            payment_previews: List[PaymentLinePreview] = []
            principal_sum = ZERO
            card_fee_total = ZERO

            for pay in norm_payments:
                method = PaymentMethod(pay["method"])
                amount = Decimal(pay["amount"])
                tendered = Decimal(pay["cash_tendered"]) if pay["cash_tendered"] is not None else None
                preview = build_payment_line_preview(
                    method=method,
                    principal_amount=amount,
                    card_fee_percent=settings.card_fee_percent,
                    card_fee_mode=settings.card_fee_mode,
                    cash_tendered=tendered,
                )
                payment_previews.append(preview)
                principal_sum += preview.amount
                card_fee_total += preview.fee_amount

            if principal_sum < subtotal:
                missing = subtotal - principal_sum
                raise InvalidPaymentError(
                    f"Pagamento insuficiente: faltam R$ {missing:.2f} para quitar o subtotal dos produtos (R$ {subtotal:.2f})."
                )
            if principal_sum > subtotal:
                excess = principal_sum - subtotal
                raise InvalidPaymentError(
                    f"A soma das parcelas destinadas aos produtos (R$ {principal_sum:.2f}) excede o subtotal da venda (R$ {subtotal:.2f}) em R$ {excess:.2f}. Para troco em dinheiro, informe o valor entregue no campo 'cash_tendered'."
                )

            total_charged = subtotal + card_fee_total

            sale_id = uow.sales.create_sale(
                shift_id=shift.id,
                kind=SaleKind.QUICK,
                subtotal=subtotal,
                card_fee_total=card_fee_total,
                total_charged=total_charged,
                operator_id=actor.id,
                created_at=now,
                idempotency_key=clean_key,
                request_fingerprint=fingerprint,
            )

            for item_data in resolved_items:
                uow.sales.add_item(
                    sale_id=sale_id,
                    product_id=item_data["product_id"],
                    item_kind=item_data["item_kind"],
                    description=item_data["description"],
                    quantity=item_data["quantity"],
                    unit_price=item_data["unit_price"],
                    line_total=item_data["line_total"],
                )

            for idx, preview in enumerate(payment_previews, start=1):
                mov_key = f"{clean_key}:p{idx}"[:64]
                uow.cash_movements.add(
                    shift_id=shift.id,
                    sale_id=sale_id,
                    kind=CashMovementKind.SALE_PAYMENT,
                    method=preview.method,
                    direction=CashDirection.IN,
                    amount=preview.amount,
                    fee_percent=preview.fee_percent,
                    fee_mode=preview.fee_mode,
                    fee_amount=preview.fee_amount,
                    gross_amount=preview.gross_amount,
                    cash_tendered=preview.cash_tendered,
                    change_given=preview.change_given,
                    reverses_id=None,
                    operator_id=actor.id,
                    created_at=now,
                    idempotency_key=mov_key,
                    note=f"Pagamento venda #{sale_id}",
                )

            uow.audit_logs.add(
                operator_id=actor.id,
                action="quick_sale_confirmed",
                entity="sale",
                entity_id=sale_id,
                created_at=now,
                details=f"subtotal={subtotal} fee={card_fee_total} total={total_charged}",
            )

            created_sale = uow.sales.get_by_id(sale_id)
            assert created_sale is not None
            return created_sale, True

    def void_quick_sale(
        self,
        actor: User,
        sale_id: int,
        reason: str,
        customer_refund_completed: bool = False,
        customer_refund_method: Optional[Union[str, PaymentMethod]] = None,
        customer_refund_notes: Optional[str] = None,
    ) -> Sale:
        """Estorna integralmente uma venda rápida, preservando o histórico.

        Diferencia explicitamente:
        1. Cancelamento contábil da venda (`sales.status = 'voided'`).
        2. Devolução financeira efetiva ao cliente (`customer_refund_status = 'pending' | 'completed'`):
           - Enquanto a devolução ao cliente NÃO ocorreu (`customer_refund_completed = False`, padrão),
             a venda fica com `customer_refund_status = 'pending'` e NENHUM movimento de saída (`direction = 'out'`)
             é gravado em `cash_movements` (pois o dinheiro ainda não saiu do caixa).
           - Somente quando a devolução efetiva é realizada (`customer_refund_completed = True` ou via
             `confirm_customer_refund`), os movimentos compensatórios (`kind = 'reversal'`, `direction = 'out'`)
             são inseridos em `cash_movements`.
        """
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem estornar vendas.")

        if not isinstance(reason, str):
            raise InvalidInputError("O motivo do estorno é obrigatório.")
        clean_reason = " ".join(reason.strip().split())
        if len(clean_reason) < 3 or len(clean_reason) > 255:
            raise InvalidInputError("Informe um motivo de estorno entre 3 e 255 caracteres.")

        refund_method_enum: Optional[PaymentMethod] = None
        if customer_refund_method is not None:
            try:
                refund_method_enum = (
                    customer_refund_method
                    if isinstance(customer_refund_method, PaymentMethod)
                    else PaymentMethod(str(customer_refund_method))
                )
            except ValueError as exc:
                raise InvalidInputError("Meio de devolução ao cliente inválido.") from exc

        clean_notes = (
            " ".join(customer_refund_notes.strip().split())[:255]
            if isinstance(customer_refund_notes, str) and customer_refund_notes.strip()
            else None
        )

        with self._uow() as uow:
            sale = uow.sales.get_by_id(sale_id, for_update=True)
            if sale is None:
                raise SaleNotFoundError("Venda não encontrada.")
            if sale.status == SaleStatus.VOIDED:
                raise SaleAlreadyVoidedError("Esta venda já foi estornada.")

            shift = uow.shifts.get_by_id(sale.shift_id, for_update=True)
            if shift is None or shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido estornar vendas pertencentes a um turno já fechado.")

            now = self._clock.now()
            movements = uow.cash_movements.list_by_sale(sale.id, for_update=True)
            original_payments = [m for m in movements if m.kind == CashMovementKind.SALE_PAYMENT]

            if customer_refund_completed and refund_method_enum is None and len(original_payments) == 1:
                refund_method_enum = original_payments[0].method

            uow.sales.mark_voided(
                sale_id=sale.id,
                voided_by=actor.id,
                voided_at=now,
                reason=clean_reason,
                customer_refund_completed=bool(customer_refund_completed),
                customer_refund_method=refund_method_enum,
                customer_refund_notes=clean_notes,
            )

            if customer_refund_completed:
                self._record_reversal_movements(
                    uow=uow,
                    sale=sale,
                    original_payments=original_payments,
                    actor=actor,
                    now=now,
                    note=f"Estorno e devolução da venda #{sale.id}: {clean_reason}"[:255],
                )

            uow.audit_logs.add(
                operator_id=actor.id,
                action="quick_sale_voided",
                entity="sale",
                entity_id=sale.id,
                created_at=now,
                reason=clean_reason,
                details=(
                    f"subtotal={sale.subtotal} fee={sale.card_fee_total} total={sale.total_charged} "
                    f"customer_refund_status={'completed' if customer_refund_completed else 'pending'}"
                ),
            )

            updated_sale = uow.sales.get_by_id(sale.id)
            assert updated_sale is not None
            return updated_sale

    @staticmethod
    def _record_reversal_movements(
        uow: UnitOfWork,
        sale: Sale,
        original_payments: list,
        actor: User,
        now,
        note: str,
    ) -> None:
        for mov in original_payments:
            rev_key = f"void:sale:{sale.id}:mov:{mov.id}"[:64]
            uow.cash_movements.add(
                shift_id=sale.shift_id,
                sale_id=sale.id,
                kind=CashMovementKind.REVERSAL,
                method=mov.method,
                direction=CashDirection.OUT,
                amount=mov.amount,
                fee_percent=mov.fee_percent,
                fee_mode=mov.fee_mode,
                fee_amount=mov.fee_amount,
                gross_amount=mov.gross_amount,
                cash_tendered=None,
                change_given=None,
                reverses_id=mov.id,
                operator_id=actor.id,
                created_at=now,
                idempotency_key=rev_key,
                note=note,
            )

    def confirm_customer_refund(
        self,
        actor: User,
        sale_id: int,
        method: Union[str, PaymentMethod],
        notes: Optional[str] = None,
    ) -> Sale:
        """Registra que a devolução financeira efetiva ao cliente de uma venda já estornada foi realizada,
        gravando neste momento a saída (`direction = 'out'`) correspondente em `cash_movements`."""
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem registrar devolução ao cliente.")

        try:
            method_enum = method if isinstance(method, PaymentMethod) else PaymentMethod(str(method))
        except ValueError as exc:
            raise InvalidInputError("Meio de devolução inválido.") from exc

        clean_notes = " ".join(notes.strip().split())[:255] if isinstance(notes, str) and notes.strip() else None

        with self._uow() as uow:
            sale = uow.sales.get_by_id(sale_id, for_update=True)
            if sale is None:
                raise SaleNotFoundError("Venda não encontrada.")
            if sale.status != SaleStatus.VOIDED:
                raise InvalidInputError("Apenas vendas estornadas podem ter devolução ao cliente confirmada.")
            if sale.customer_refund_status == CustomerRefundStatus.COMPLETED:
                raise InvalidInputError("A devolução ao cliente desta venda já foi registrada como concluída.")

            shift = uow.shifts.get_by_id(sale.shift_id, for_update=True)
            if shift is None or shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido alterar registros de um turno já fechado.")

            now = self._clock.now()
            movements = uow.cash_movements.list_by_sale(sale.id, for_update=True)
            original_payments = [m for m in movements if m.kind == CashMovementKind.SALE_PAYMENT]
            already_reversed_ids = {m.reverses_id for m in movements if m.kind == CashMovementKind.REVERSAL}
            pending_reversals = [m for m in original_payments if m.id not in already_reversed_ids]

            self._record_reversal_movements(
                uow=uow,
                sale=sale,
                original_payments=pending_reversals,
                actor=actor,
                now=now,
                note=f"Devolução efetiva venda #{sale.id} ({method_enum.value})"[:255],
            )

            uow.sales.mark_customer_refunded(
                sale_id=sale.id,
                refunded_by=actor.id,
                refunded_at=now,
                method=method_enum,
                notes=clean_notes,
            )
            uow.audit_logs.add(
                operator_id=actor.id,
                action="customer_refund_completed",
                entity="sale",
                entity_id=sale.id,
                created_at=now,
                details=f"method={method_enum.value} notes={clean_notes or ''}",
            )
            updated = uow.sales.get_by_id(sale.id)
            assert updated is not None
            return updated

    def get_sale(self, sale_id: int) -> Sale:
        with self._uow() as uow:
            sale = uow.sales.get_by_id(sale_id)
            if sale is None:
                raise SaleNotFoundError("Venda não encontrada.")
            return sale

    def list_recent_sales(self, limit: int = 50) -> List[Sale]:
        clean_limit = max(1, min(int(limit), 200))
        with self._uow() as uow:
            return uow.sales.list_recent(limit=clean_limit)

    def get_financial_summary(self, shift_id: Optional[int] = None) -> dict:
        with self._uow() as uow:
            return uow.cash_movements.get_financial_summary(shift_id=shift_id)
