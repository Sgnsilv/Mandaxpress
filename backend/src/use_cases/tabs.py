"""Casos de uso de comandas abertas: abertura, consumo incremental, pagamentos parciais,
cancelamento auditável de lançamentos, devolução efetiva e encerramento."""
import hashlib
import json
import time
from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple, Union

import pymysql

from ..domain.errors import (
    IdempotencyConflictError,
    InactiveProductError,
    InsufficientCreditError,
    InvalidInputError,
    InvalidPaymentError,
    PermissionDeniedError,
    ProductNotFoundError,
    SaleAlreadyVoidedError,
    SaleNotFoundError,
    ShiftClosedError,
    TabAlreadyOpenError,
    TabBalancePendingError,
    TabClosedError,
    TabCreditBalancePendingError,
    TabIncompleteOperationsError,
    TabNotFoundError,
)
from ..domain.finance import (
    ZERO,
    build_payment_line_preview,
    parse_money,
    plan_tab_excess_reversals,
)
from ..domain.models import (
    CashDirection,
    CashMovement,
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
    Tab,
    TabStatus,
    User,
)
from ..domain.ports import Clock, UnitOfWork
from .sales import (
    QuickSaleItemInput,
    QuickSalePaymentInput,
    _validate_custom_description,
    _validate_idempotency_key,
    _validate_quantity,
)

_DUPLICATE_ENTRY = 1062
_LOCK_DEADLOCK = 1213
_LOCK_WAIT_TIMEOUT = 1205


def _validate_tab_number(number: Union[int, str]) -> int:
    if isinstance(number, bool) or isinstance(number, float):
        raise InvalidInputError("O número da comanda deve ser um número inteiro positivo.")
    if isinstance(number, str):
        cleaned = number.strip()
        if not cleaned or not cleaned.isdigit():
            raise InvalidInputError("O número da comanda deve conter apenas dígitos numéricos.")
        num = int(cleaned)
    elif isinstance(number, int):
        num = number
    else:
        raise InvalidInputError("O número da comanda é obrigatório.")

    if num < 1 or num > 999999:
        raise InvalidInputError("O número da comanda deve estar entre 1 e 999999.")
    return num


def _validate_customer_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    if not isinstance(name, str):
        raise InvalidInputError("Nome do cliente inválido.")
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        return None
    if len(cleaned) > 120:
        raise InvalidInputError("O nome do cliente deve ter no máximo 120 caracteres.")
    return cleaned


def _validate_notes(notes: Optional[str]) -> Optional[str]:
    if notes is None:
        return None
    if not isinstance(notes, str):
        raise InvalidInputError("Observação inválida.")
    cleaned = " ".join(notes.strip().split())
    if not cleaned:
        return None
    if len(cleaned) > 255:
        raise InvalidInputError("A observação deve ter no máximo 255 caracteres.")
    return cleaned


def _normalize_tab_items(
    tab_id: int,
    items: Sequence[QuickSaleItemInput],
) -> Tuple[List[dict], str]:
    if not items:
        raise InvalidInputError("O lançamento na comanda deve conter pelo menos um item.")

    normalized_items = []
    for raw_item in items:
        qty = _validate_quantity(raw_item.quantity)
        has_product = raw_item.product_id is not None
        has_custom = raw_item.custom_description is not None or raw_item.custom_amount is not None
        if has_product and has_custom:
            raise InvalidInputError(
                "Um item não pode ser simultaneamente produto cadastrado e item avulso."
            )
        if not has_product and not has_custom:
            raise InvalidInputError(
                "Cada item deve informar um product_id ou descrição/valor avulso."
            )

        if has_product:
            if (
                isinstance(raw_item.product_id, bool)
                or not isinstance(raw_item.product_id, int)
                or raw_item.product_id < 1
            ):
                raise InvalidInputError("Identificador de produto inválido.")
            normalized_items.append({
                "kind": SaleItemKind.PRODUCT.value,
                "product_id": raw_item.product_id,
                "quantity": qty,
            })
        else:
            desc = _validate_custom_description(raw_item.custom_description)
            unit_price = parse_money(
                raw_item.custom_amount,
                field_name="valor do item avulso",
                allow_zero=False,
            )
            normalized_items.append({
                "kind": SaleItemKind.CUSTOM.value,
                "description": desc,
                "unit_price": str(unit_price),
                "quantity": qty,
            })

    canonical = json.dumps(
        {"op": "tab_add_items", "tab_id": tab_id, "items": normalized_items},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return normalized_items, fingerprint


def _normalize_tab_payments(
    tab_id: int,
    payments: Sequence[QuickSalePaymentInput],
    op: str = "tab_payment",
) -> Tuple[List[dict], str]:
    if not payments:
        raise InvalidPaymentError("Informe pelo menos uma forma de pagamento.")

    normalized_payments = []
    for raw_pay in payments:
        try:
            method = (
                raw_pay.method
                if isinstance(raw_pay.method, PaymentMethod)
                else PaymentMethod(str(raw_pay.method))
            )
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
        {"op": op, "tab_id": tab_id, "payments": normalized_payments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return normalized_payments, fingerprint


class TabService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock):
        self._uow = uow_factory
        self._clock = clock

    def open_tab(
        self,
        actor: User,
        number: Union[int, str],
        customer_name: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Tab:
        clean_number = _validate_tab_number(number)
        clean_name = _validate_customer_name(customer_name)
        clean_notes = _validate_notes(notes)

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                with self._uow() as uow:
                    existing_open = uow.tabs.get_open_by_number(clean_number, for_update=True)
                    if existing_open is not None:
                        raise TabAlreadyOpenError(
                            f"Já existe uma comanda aberta com o número {clean_number}."
                        )

                    now = self._clock.now()
                    shift = uow.shifts.get_open_shift(for_update=True)
                    if shift is None:
                        shift = uow.shifts.create_open_shift(opened_by=actor.id, now=now)

                    tab_id = uow.tabs.create_tab(
                        shift_id=shift.id,
                        number=clean_number,
                        customer_name=clean_name,
                        opened_by=actor.id,
                        opened_at=now,
                        notes=clean_notes,
                    )

                    uow.audit_logs.add(
                        operator_id=actor.id,
                        action="tab_opened",
                        entity="tab",
                        entity_id=tab_id,
                        created_at=now,
                        details=f"number={clean_number} customer_name={clean_name or ''}",
                    )

                    created = uow.tabs.get_by_id(tab_id)
                    assert created is not None
                    return created
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir a abertura da comanda após novas tentativas.")

    def get_tab(self, tab_id: int) -> Tab:
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        with self._uow() as uow:
            tab = uow.tabs.get_by_id(tab_id)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            return tab

    def get_tabs_by_number(
        self,
        number: Union[int, str],
        include_closed: bool = True,
    ) -> Tuple[Optional[Tab], List[Tab]]:
        clean_number = _validate_tab_number(number)
        with self._uow() as uow:
            open_tab = uow.tabs.get_open_by_number(clean_number)
            status_filter = None if include_closed else TabStatus.OPEN
            history = uow.tabs.list_tabs(status=status_filter, number=clean_number, limit=100)
            return open_tab, history

    def list_tabs(
        self,
        status: Optional[Union[str, TabStatus]] = None,
        number: Optional[Union[int, str]] = None,
        search: Optional[str] = None,
        limit: int = 100,
    ) -> List[Tab]:
        status_enum: Optional[TabStatus] = None
        if status is not None and str(status).strip() and str(status).strip().lower() != "all":
            try:
                status_enum = (
                    status
                    if isinstance(status, TabStatus)
                    else TabStatus(str(status).strip().lower())
                )
            except ValueError as exc:
                raise InvalidInputError("Filtro de status de comanda inválido.") from exc

        clean_number = _validate_tab_number(number) if number is not None and str(number).strip() != "" else None
        clean_search = search.strip() if isinstance(search, str) and search.strip() else None
        clean_limit = max(1, min(int(limit), 200))

        with self._uow() as uow:
            return uow.tabs.list_tabs(
                status=status_enum,
                number=clean_number,
                search=clean_search,
                limit=clean_limit,
            )

    def add_items(
        self,
        actor: User,
        tab_id: int,
        idempotency_key: str,
        items: Sequence[QuickSaleItemInput],
    ) -> Tuple[Tab, Sale, bool]:
        """Adiciona um lançamento incremental de consumo a uma comanda aberta.

        Retorna `(Tab, Sale, created_now)`.
        """
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        clean_key = _validate_idempotency_key(idempotency_key)
        norm_items, fingerprint = _normalize_tab_items(tab_id, items)

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                return self._execute_add_items(
                    actor=actor,
                    tab_id=tab_id,
                    clean_key=clean_key,
                    norm_items=norm_items,
                    fingerprint=fingerprint,
                )
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                    with self._uow() as uow:
                        existing = uow.sales.get_by_idempotency_key(clean_key)
                        if existing is not None:
                            if existing.request_fingerprint != fingerprint or existing.tab_id != tab_id:
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                                ) from exc
                            tab = uow.tabs.get_by_id(tab_id)
                            assert tab is not None
                            return tab, existing, False
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir o lançamento na comanda após novas tentativas.")

    def _execute_add_items(
        self,
        actor: User,
        tab_id: int,
        clean_key: str,
        norm_items: List[dict],
        fingerprint: str,
    ) -> Tuple[Tab, Sale, bool]:
        with self._uow() as uow:
            existing = uow.sales.get_by_idempotency_key(clean_key, for_update=True)
            if existing is not None:
                if existing.request_fingerprint != fingerprint or existing.tab_id != tab_id:
                    raise IdempotencyConflictError(
                        "Esta chave de idempotência já foi utilizada com dados diferentes."
                    )
                tab = uow.tabs.get_by_id(tab_id)
                assert tab is not None
                return tab, existing, False

            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Não é permitido lançar consumo em uma comanda já encerrada.")
            if tab.pending_refund_total > ZERO:
                raise TabIncompleteOperationsError(
                    "Há uma devolução pendente ao cliente nesta comanda. Confirme a devolução antes de realizar novos lançamentos."
                )

            now = self._clock.now()
            tab_shift = uow.shifts.get_by_id(tab.shift_id, for_update=True)
            if tab_shift is not None and tab_shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido lançar consumo em um turno já fechado.")
            shift = uow.shifts.get_open_shift(for_update=True)
            if shift is None:
                shift = uow.shifts.create_open_shift(opened_by=actor.id, now=now)

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
                raise InvalidInputError("O subtotal do lançamento deve ser maior que R$ 0,00.")

            sale_id = uow.sales.create_sale(
                shift_id=shift.id,
                tab_id=tab.id,
                kind=SaleKind.TAB_ITEM,
                subtotal=subtotal,
                card_fee_total=ZERO,
                total_charged=subtotal,
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

            updated_tab = uow.tabs.get_by_id(tab.id)
            created_sale = uow.sales.get_by_id(sale_id)
            assert updated_tab is not None and created_sale is not None

            credit_applied_delta = max(ZERO, updated_tab.credit_applied - tab.credit_applied)
            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_items_added",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                details=(
                    f"number={tab.number} sale_id={sale_id} subtotal={subtotal} "
                    f"credit_applied_now={credit_applied_delta} "
                    f"credit_balance_after={updated_tab.credit_balance} "
                    f"balance_due_after={updated_tab.balance_due}"
                ),
            )
            return updated_tab, created_sale, True

    def record_payment(
        self,
        actor: User,
        tab_id: int,
        idempotency_key: str,
        payments: Sequence[QuickSalePaymentInput],
        note: Optional[str] = None,
    ) -> Tuple[Tab, List[CashMovement], bool]:
        """Registra um pagamento parcial ou integral (único ou dividido) em uma comanda aberta.

        Retorna `(Tab, List[CashMovement], created_now)`.
        """
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        clean_key = _validate_idempotency_key(idempotency_key)
        clean_note = _validate_notes(note)
        norm_payments, fingerprint = _normalize_tab_payments(tab_id, payments)

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                return self._execute_record_payment(
                    actor=actor,
                    tab_id=tab_id,
                    clean_key=clean_key,
                    norm_payments=norm_payments,
                    fingerprint=fingerprint,
                    clean_note=clean_note,
                )
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                    with self._uow() as uow:
                        existing = uow.cash_movements.list_by_batch_key(clean_key)
                        if existing:
                            if (
                                existing[0].request_fingerprint != fingerprint
                                or existing[0].tab_id != tab_id
                            ):
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                                ) from exc
                            tab = uow.tabs.get_by_id(tab_id)
                            assert tab is not None
                            return tab, existing, False
                        raise IdempotencyConflictError(
                            "Esta chave de idempotência já foi utilizada em outra operação."
                        ) from exc
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir o pagamento da comanda após novas tentativas.")

    def _execute_record_payment(
        self,
        actor: User,
        tab_id: int,
        clean_key: str,
        norm_payments: List[dict],
        fingerprint: str,
        clean_note: Optional[str],
    ) -> Tuple[Tab, List[CashMovement], bool]:
        with self._uow() as uow:
            existing_batch = uow.cash_movements.list_by_batch_key(clean_key, for_update=True)
            if existing_batch:
                if (
                    existing_batch[0].request_fingerprint != fingerprint
                    or existing_batch[0].tab_id != tab_id
                ):
                    raise IdempotencyConflictError(
                        "Esta chave de idempotência já foi utilizada com dados diferentes."
                    )
                tab = uow.tabs.get_by_id(tab_id)
                assert tab is not None
                return tab, existing_batch, False

            first_mov_key = f"{clean_key}:p1"[:64]
            if (
                uow.cash_movements.get_by_idempotency_key(first_mov_key, for_update=True) is not None
                or uow.cash_movements.get_by_idempotency_key(clean_key, for_update=True) is not None
            ):
                raise IdempotencyConflictError(
                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                )

            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Não é permitido registrar pagamentos em uma comanda já encerrada.")
            if tab.pending_refund_total > ZERO:
                raise TabIncompleteOperationsError(
                    "Há uma devolução pendente ao cliente nesta comanda. Confirme a devolução antes de registrar novos pagamentos."
                )
            if tab.balance_due <= ZERO:
                raise InvalidPaymentError("Esta comanda não possui saldo pendente para pagamento.")

            now = self._clock.now()
            tab_shift = uow.shifts.get_by_id(tab.shift_id, for_update=True)
            if tab_shift is not None and tab_shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido registrar pagamentos em um turno já fechado.")
            shift = uow.shifts.get_open_shift(for_update=True)
            if shift is None:
                shift = uow.shifts.create_open_shift(opened_by=actor.id, now=now)

            settings = uow.payment_settings.get(for_update=True)

            payment_previews: List[PaymentLinePreview] = []
            principal_sum = ZERO
            card_fee_total = ZERO
            gross_sum = ZERO

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
                gross_sum += preview.gross_amount

            if principal_sum > tab.balance_due:
                excess = principal_sum - tab.balance_due
                raise InvalidPaymentError(
                    f"O valor de principal informado (R$ {principal_sum:.2f}) excede o saldo pendente da comanda "
                    f"(R$ {tab.balance_due:.2f}) em R$ {excess:.2f}. Para troco em dinheiro, informe o valor entregue "
                    f"no campo 'cash_tendered'."
                )

            created_movements: List[CashMovement] = []
            for idx, preview in enumerate(payment_previews, start=1):
                mov_key = f"{clean_key}:p{idx}"[:64]
                mov = uow.cash_movements.add(
                    shift_id=shift.id,
                    tab_id=tab.id,
                    sale_id=None,
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
                    batch_key=clean_key,
                    request_fingerprint=fingerprint,
                    note=clean_note or f"Pagamento comanda #{tab.number}",
                )
                created_movements.append(mov)

            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_payment_recorded",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                details=(
                    f"number={tab.number} principal={principal_sum} fee={card_fee_total} "
                    f"gross={gross_sum} balance_after={tab.balance_due - principal_sum}"
                ),
            )

            updated_tab = uow.tabs.get_by_id(tab.id)
            assert updated_tab is not None
            return updated_tab, created_movements, True

    def record_credit_deposit(
        self,
        actor: User,
        tab_id: int,
        idempotency_key: str,
        method: Optional[Union[str, PaymentMethod]] = None,
        amount: Optional[Union[str, int, float, Decimal]] = None,
        cash_tendered: Optional[Union[str, int, float, Decimal]] = None,
        payments: Optional[Sequence[QuickSalePaymentInput]] = None,
        note: Optional[str] = None,
    ) -> Tuple[Tab, List[CashMovement], bool]:
        """Registra depósito de crédito antecipado (`kind = 'credit_deposit'`) vinculado à comanda.

        - Depósito antecipado é entrada financeira e passivo perante o cliente, nunca uma venda.
        - Em depósito por cartão, a taxa repassada incide sobre o principal depositado (`gross = amount + fee`),
          sem aumentar o saldo de crédito (`credit_deposited` e `credit_balance` recebem apenas `amount`).
        - Se já houver consumo em aberto na comanda, o crédito depositado é aplicado automaticamente.
        """
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        clean_key = _validate_idempotency_key(idempotency_key)
        clean_note = _validate_notes(note)

        if payments is None:
            if method is None or amount is None:
                raise InvalidPaymentError("Informe o método e o valor do depósito de crédito.")
            resolved_inputs: Sequence[QuickSalePaymentInput] = [
                QuickSalePaymentInput(
                    method=method,
                    amount=amount,
                    cash_tendered=cash_tendered,
                )
            ]
        else:
            resolved_inputs = payments

        norm_payments, fingerprint = _normalize_tab_payments(
            tab_id,
            resolved_inputs,
            op="tab_credit_deposit",
        )

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                return self._execute_record_credit_deposit(
                    actor=actor,
                    tab_id=tab_id,
                    clean_key=clean_key,
                    norm_payments=norm_payments,
                    fingerprint=fingerprint,
                    clean_note=clean_note,
                )
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                    with self._uow() as uow:
                        existing = uow.cash_movements.list_by_batch_key(clean_key)
                        if existing:
                            if (
                                existing[0].request_fingerprint != fingerprint
                                or existing[0].tab_id != tab_id
                                or existing[0].kind != CashMovementKind.CREDIT_DEPOSIT
                            ):
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                                ) from exc
                            tab = uow.tabs.get_by_id(tab_id)
                            assert tab is not None
                            return tab, existing, False
                        raise IdempotencyConflictError(
                            "Esta chave de idempotência já foi utilizada em outra operação."
                        ) from exc
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir o depósito de crédito após novas tentativas.")

    def _execute_record_credit_deposit(
        self,
        actor: User,
        tab_id: int,
        clean_key: str,
        norm_payments: List[dict],
        fingerprint: str,
        clean_note: Optional[str],
    ) -> Tuple[Tab, List[CashMovement], bool]:
        with self._uow() as uow:
            existing_batch = uow.cash_movements.list_by_batch_key(clean_key, for_update=True)
            if existing_batch:
                if (
                    existing_batch[0].request_fingerprint != fingerprint
                    or existing_batch[0].tab_id != tab_id
                    or existing_batch[0].kind != CashMovementKind.CREDIT_DEPOSIT
                ):
                    raise IdempotencyConflictError(
                        "Esta chave de idempotência já foi utilizada com dados diferentes."
                    )
                tab = uow.tabs.get_by_id(tab_id)
                assert tab is not None
                return tab, existing_batch, False

            first_mov_key = f"{clean_key}:c1"[:64]
            if (
                uow.cash_movements.get_by_idempotency_key(first_mov_key, for_update=True) is not None
                or uow.cash_movements.get_by_idempotency_key(clean_key, for_update=True) is not None
            ):
                raise IdempotencyConflictError(
                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                )

            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Não é permitido registrar depósito de crédito em uma comanda já encerrada.")
            if tab.pending_refund_total > ZERO:
                raise TabIncompleteOperationsError(
                    "Há uma devolução pendente ao cliente nesta comanda. Confirme a devolução antes de registrar depósito de crédito."
                )

            now = self._clock.now()
            tab_shift = uow.shifts.get_by_id(tab.shift_id, for_update=True)
            if tab_shift is not None and tab_shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido registrar depósito de crédito em um turno já fechado.")
            shift = uow.shifts.get_open_shift(for_update=True)
            if shift is None:
                shift = uow.shifts.create_open_shift(opened_by=actor.id, now=now)

            settings = uow.payment_settings.get(for_update=True)

            payment_previews: List[PaymentLinePreview] = []
            principal_sum = ZERO
            card_fee_total = ZERO
            gross_sum = ZERO

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
                gross_sum += preview.gross_amount

            created_movements: List[CashMovement] = []
            for idx, preview in enumerate(payment_previews, start=1):
                mov_key = f"{clean_key}:c{idx}"[:64] if len(payment_previews) > 1 else clean_key
                mov = uow.cash_movements.add(
                    shift_id=shift.id,
                    tab_id=tab.id,
                    sale_id=None,
                    kind=CashMovementKind.CREDIT_DEPOSIT,
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
                    batch_key=clean_key,
                    request_fingerprint=fingerprint,
                    note=clean_note or f"Depósito de crédito comanda #{tab.number}",
                )
                created_movements.append(mov)

            updated_tab = uow.tabs.get_by_id(tab.id)
            assert updated_tab is not None

            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_credit_deposited",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                details=(
                    f"number={tab.number} credit_principal={principal_sum} fee={card_fee_total} "
                    f"gross={gross_sum} credit_applied={updated_tab.credit_applied} "
                    f"credit_balance={updated_tab.credit_balance} balance_due={updated_tab.balance_due}"
                ),
            )
            return updated_tab, created_movements, True

    def refund_credit(
        self,
        actor: User,
        tab_id: int,
        idempotency_key: str,
        method: Union[str, PaymentMethod],
        amount: Union[str, int, float, Decimal],
        note: Optional[str] = None,
    ) -> Tuple[Tab, CashMovement, bool]:
        """Registra devolução parcial ou integral de saldo de crédito disponível (`kind = 'credit_refund'`).

        - Permite devolver por método diferente do depósito original (`pix`, `cash` ou `card`),
          preservando os movimentos reais de entrada e saída por método.
        - Bloqueia devolução superior ao `credit_balance` disponível na comanda.
        - Não gera taxa de cartão nem troco fictício.
        """
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        clean_key = _validate_idempotency_key(idempotency_key)
        clean_note = _validate_notes(note)

        try:
            method_enum = method if isinstance(method, PaymentMethod) else PaymentMethod(str(method))
        except ValueError as exc:
            raise InvalidPaymentError(f"Meio de devolução inválido: {method}") from exc

        parsed_amount = parse_money(amount, field_name="valor da devolução de crédito", allow_zero=False)

        canonical = json.dumps(
            {
                "op": "tab_credit_refund",
                "tab_id": tab_id,
                "method": method_enum.value,
                "amount": str(parsed_amount),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                return self._execute_refund_credit(
                    actor=actor,
                    tab_id=tab_id,
                    clean_key=clean_key,
                    method_enum=method_enum,
                    parsed_amount=parsed_amount,
                    fingerprint=fingerprint,
                    clean_note=clean_note,
                )
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                    with self._uow() as uow:
                        existing = uow.cash_movements.get_by_idempotency_key(clean_key)
                        if existing is not None:
                            if (
                                existing.kind != CashMovementKind.CREDIT_REFUND
                                or existing.tab_id != tab_id
                                or existing.request_fingerprint != fingerprint
                            ):
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                                ) from exc
                            tab = uow.tabs.get_by_id(tab_id)
                            assert tab is not None
                            return tab, existing, False
                        raise IdempotencyConflictError(
                            "Esta chave de idempotência já foi utilizada em outra operação."
                        ) from exc
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir a devolução de crédito após novas tentativas.")

    def _execute_refund_credit(
        self,
        actor: User,
        tab_id: int,
        clean_key: str,
        method_enum: PaymentMethod,
        parsed_amount: Decimal,
        fingerprint: str,
        clean_note: Optional[str],
    ) -> Tuple[Tab, CashMovement, bool]:
        with self._uow() as uow:
            existing = uow.cash_movements.get_by_idempotency_key(clean_key, for_update=True)
            if existing is not None:
                if (
                    existing.kind != CashMovementKind.CREDIT_REFUND
                    or existing.tab_id != tab_id
                    or existing.request_fingerprint != fingerprint
                ):
                    raise IdempotencyConflictError(
                        "Esta chave de idempotência já foi utilizada com dados diferentes."
                    )
                tab = uow.tabs.get_by_id(tab_id)
                assert tab is not None
                return tab, existing, False

            if uow.cash_movements.list_by_batch_key(clean_key, for_update=True):
                raise IdempotencyConflictError(
                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                )

            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Não é permitido devolver crédito de uma comanda já encerrada.")
            if tab.pending_refund_total > ZERO:
                raise TabIncompleteOperationsError(
                    "Há uma devolução pendente de lançamento cancelado nesta comanda. Confirme-a antes de devolver saldo de crédito."
                )

            now = self._clock.now()
            tab_shift = uow.shifts.get_by_id(tab.shift_id, for_update=True)
            if tab_shift is not None and tab_shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido devolver crédito em um turno já fechado.")
            shift = uow.shifts.get_open_shift(for_update=True)
            if shift is None:
                shift = uow.shifts.create_open_shift(opened_by=actor.id, now=now)

            if tab.credit_balance <= ZERO:
                raise InsufficientCreditError(
                    f"A comanda #{tab.number} não possui saldo de crédito disponível para devolução."
                )
            if parsed_amount > tab.credit_balance:
                raise InsufficientCreditError(
                    f"O valor solicitado para devolução (R$ {parsed_amount:.2f}) excede o crédito disponível "
                    f"na comanda #{tab.number} (R$ {tab.credit_balance:.2f})."
                )

            mov = uow.cash_movements.add(
                shift_id=shift.id,
                tab_id=tab.id,
                sale_id=None,
                kind=CashMovementKind.CREDIT_REFUND,
                method=method_enum,
                direction=CashDirection.OUT,
                amount=parsed_amount,
                fee_percent=ZERO,
                fee_mode="none",
                fee_amount=ZERO,
                gross_amount=parsed_amount,
                cash_tendered=None,
                change_given=None,
                reverses_id=None,
                operator_id=actor.id,
                created_at=now,
                idempotency_key=clean_key,
                batch_key=clean_key,
                request_fingerprint=fingerprint,
                note=clean_note or f"Devolução de saldo de crédito comanda #{tab.number} ({method_enum.value})",
            )

            updated_tab = uow.tabs.get_by_id(tab.id)
            assert updated_tab is not None

            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_credit_refunded",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                reason=clean_note,
                details=(
                    f"number={tab.number} refund_id={mov.id} method={method_enum.value} "
                    f"amount={parsed_amount} credit_balance_after={updated_tab.credit_balance}"
                ),
            )
            return updated_tab, mov, True

    def void_credit_deposit(
        self,
        actor: User,
        tab_id: int,
        movement_id: int,
        reason: str,
        customer_refund_completed: bool = False,
        customer_refund_method: Optional[Union[str, PaymentMethod]] = None,
        customer_refund_notes: Optional[str] = None,
        refund_method: Optional[Union[str, PaymentMethod]] = None,
        notes: Optional[str] = None,
    ) -> Tuple[Tab, Optional[CashMovement]]:
        """Cancela administrativamente um depósito de crédito antecipado lançado por engano.

        - Exige perfil `admin`, motivo obrigatório, comanda aberta e turno aberto.
        - Não permite cancelar um depósito cujo valor já tenha sido devolvido ao cliente via `credit_refund`.
        - Separa o cancelamento contábil do depósito da saída física de caixa:
          - Se `customer_refund_completed = False` (padrão seguro), cancela contabilmente o depósito
            (`customer_refund_status = 'pending'`, reduzindo imediatamente `credit_deposited`),
            registra a obrigação pendente (`pending_refund_total`) e NÃO insere movimento `direction = 'out'`
            em `cash_movements` antes da devolução efetiva ao cliente.
          - Se `customer_refund_completed = True`, marca `customer_refund_status = 'completed'` e insere
            o movimento `kind = 'reversal'`, `direction = 'out'` em `cash_movements`.
        - Preserva a política de taxa de cartão do Bloco 3 (`fee_amount = 0.00` na reversão).
        """
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem cancelar depósitos de crédito.")
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        if isinstance(movement_id, bool) or not isinstance(movement_id, int) or movement_id < 1:
            raise InvalidInputError("Identificador do depósito inválido.")

        if not isinstance(reason, str):
            raise InvalidInputError("O motivo do cancelamento do depósito é obrigatório.")
        clean_reason = " ".join(reason.strip().split())
        if len(clean_reason) < 3 or len(clean_reason) > 255:
            raise InvalidInputError("Informe um motivo de cancelamento entre 3 e 255 caracteres.")

        raw_method = customer_refund_method if customer_refund_method is not None else refund_method
        resolved_method: Optional[PaymentMethod] = None
        if raw_method is not None:
            try:
                resolved_method = (
                    raw_method
                    if isinstance(raw_method, PaymentMethod)
                    else PaymentMethod(str(raw_method))
                )
            except ValueError as exc:
                raise InvalidInputError("Meio de devolução inválido.") from exc

        raw_notes = customer_refund_notes if customer_refund_notes is not None else notes
        clean_notes = _validate_notes(raw_notes)

        with self._uow() as uow:
            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Não é permitido cancelar depósitos de uma comanda já encerrada.")
            if tab.pending_refund_total > ZERO:
                raise TabIncompleteOperationsError(
                    "Confirme a devolução pendente anterior nesta comanda antes de cancelar um depósito."
                )

            deposit_mov = next(
                (
                    m
                    for m in tab.credit_deposits
                    if m.id == movement_id
                    and m.kind == CashMovementKind.CREDIT_DEPOSIT
                    and m.direction == CashDirection.IN
                ),
                None,
            )
            if deposit_mov is None:
                raise InvalidInputError("Depósito de crédito não encontrado nesta comanda.")

            already_reversed = deposit_mov.voided_at is not None or any(
                m.kind == CashMovementKind.REVERSAL and m.reverses_id == deposit_mov.id
                for m in tab.credit_deposits
            )
            if already_reversed:
                raise SaleAlreadyVoidedError("Este depósito de crédito já foi cancelado.")

            shift = uow.shifts.get_by_id(deposit_mov.shift_id, for_update=True)
            if shift is None or shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido cancelar depósitos pertencentes a um turno já fechado.")

            if tab.credit_deposited - deposit_mov.amount < tab.credit_refunded:
                raise InvalidPaymentError(
                    "Não é possível cancelar este depósito pois parte ou a totalidade do crédito já foi devolvida ao cliente."
                )

            now = self._clock.now()
            is_completed = bool(customer_refund_completed)
            refund_status = (
                CustomerRefundStatus.COMPLETED if is_completed else CustomerRefundStatus.PENDING
            )
            out_method = resolved_method or deposit_mov.method

            uow.cash_movements.mark_credit_deposit_voided(
                movement_id=deposit_mov.id,
                voided_by=actor.id,
                voided_at=now,
                reason=clean_reason,
                customer_refund_status=refund_status,
                customer_refund_amount=deposit_mov.amount,
                customer_refund_method=out_method if is_completed else None,
                customer_refund_notes=clean_notes if is_completed else None,
            )

            rev_mov: Optional[CashMovement] = None
            if is_completed:
                rev_key = f"void:tab:{tab.id}:credit:{deposit_mov.id}"[:64]
                note_text = f"Cancelamento depósito #{deposit_mov.id} comanda #{tab.number}: {clean_reason}"
                if clean_notes:
                    note_text = f"{note_text} ({clean_notes})"

                rev_mov = uow.cash_movements.add(
                    shift_id=deposit_mov.shift_id,
                    tab_id=tab.id,
                    sale_id=None,
                    kind=CashMovementKind.REVERSAL,
                    method=out_method,
                    direction=CashDirection.OUT,
                    amount=deposit_mov.amount,
                    fee_percent=ZERO,
                    fee_mode="none",
                    fee_amount=ZERO,
                    gross_amount=deposit_mov.amount,
                    cash_tendered=None,
                    change_given=None,
                    reverses_id=deposit_mov.id,
                    operator_id=actor.id,
                    created_at=now,
                    idempotency_key=rev_key,
                    note=note_text[:255],
                )

            updated_tab = uow.tabs.get_by_id(tab.id)
            assert updated_tab is not None

            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_credit_deposit_voided",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                reason=clean_reason,
                details=(
                    f"number={tab.number} deposit_id={deposit_mov.id} refund_status={refund_status.value} "
                    f"reversal_id={rev_mov.id if rev_mov else None} amount={deposit_mov.amount} "
                    f"credit_balance_after={updated_tab.credit_balance} balance_due_after={updated_tab.balance_due}"
                ),
            )
            return updated_tab, rev_mov

    def confirm_credit_deposit_refund(
        self,
        actor: User,
        tab_id: int,
        movement_id: int,
        method: Union[str, PaymentMethod],
        notes: Optional[str] = None,
    ) -> Tuple[Tab, CashMovement]:
        """Confirma que a devolução financeira pendente de um depósito de crédito cancelado foi realizada."""
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem confirmar devoluções ao cliente.")
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        if isinstance(movement_id, bool) or not isinstance(movement_id, int) or movement_id < 1:
            raise InvalidInputError("Identificador do depósito inválido.")

        try:
            method_enum = method if isinstance(method, PaymentMethod) else PaymentMethod(str(method))
        except ValueError as exc:
            raise InvalidInputError("Meio de devolução inválido.") from exc

        clean_notes = _validate_notes(notes)

        with self._uow() as uow:
            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")

            deposit_mov = next(
                (
                    m
                    for m in tab.credit_deposits
                    if m.id == movement_id
                    and m.kind == CashMovementKind.CREDIT_DEPOSIT
                    and m.direction == CashDirection.IN
                ),
                None,
            )
            if deposit_mov is None:
                raise InvalidInputError("Depósito de crédito não encontrado nesta comanda.")
            if deposit_mov.voided_at is None:
                raise InvalidInputError("Apenas depósitos de crédito cancelados permitem confirmação de devolução.")
            if deposit_mov.customer_refund_status == CustomerRefundStatus.COMPLETED:
                raise InvalidInputError("A devolução deste depósito já foi registrada.")

            shift = uow.shifts.get_by_id(deposit_mov.shift_id, for_update=True)
            if shift is None or shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido registrar devolução em um turno já fechado.")

            now = self._clock.now()
            uow.cash_movements.mark_credit_deposit_refunded(
                movement_id=deposit_mov.id,
                refunded_by=actor.id,
                refunded_at=now,
                method=method_enum,
                notes=clean_notes,
            )

            rev_key = f"void:tab:{tab.id}:credit:{deposit_mov.id}"[:64]
            note_text = f"Devolução depósito cancelado #{deposit_mov.id} comanda #{tab.number}: {deposit_mov.void_reason}"
            if clean_notes:
                note_text = f"{note_text} ({clean_notes})"

            rev_mov = uow.cash_movements.add(
                shift_id=deposit_mov.shift_id,
                tab_id=tab.id,
                sale_id=None,
                kind=CashMovementKind.REVERSAL,
                method=method_enum,
                direction=CashDirection.OUT,
                amount=deposit_mov.amount,
                fee_percent=ZERO,
                fee_mode="none",
                fee_amount=ZERO,
                gross_amount=deposit_mov.amount,
                cash_tendered=None,
                change_given=None,
                reverses_id=deposit_mov.id,
                operator_id=actor.id,
                created_at=now,
                idempotency_key=rev_key,
                note=note_text[:255],
            )

            updated_tab = uow.tabs.get_by_id(tab.id)
            assert updated_tab is not None

            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_credit_deposit_refund_confirmed",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                reason=clean_notes,
                details=(
                    f"number={tab.number} deposit_id={deposit_mov.id} reversal_id={rev_mov.id} "
                    f"method={method_enum.value} amount={deposit_mov.amount}"
                ),
            )
            return updated_tab, rev_mov

    def close_tab(
        self,
        actor: User,
        tab_id: int,
        notes: Optional[str] = None,
    ) -> Tab:
        """Encerra uma comanda quando o saldo pendente é zero e não há operações financeiras pendentes."""
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 1:
            raise InvalidInputError("Identificador de comanda inválido.")
        clean_notes = _validate_notes(notes)

        with self._uow() as uow:
            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Esta comanda já está encerrada.")
            if tab.pending_refund_total > ZERO:
                raise TabIncompleteOperationsError(
                    f"Não é possível encerrar a comanda #{tab.number}: há devolução pendente ao cliente de R$ {tab.pending_refund_total:.2f}."
                )
            if tab.balance_due > ZERO:
                raise TabBalancePendingError(
                    f"Não é possível encerrar a comanda #{tab.number}: ainda há saldo pendente de R$ {tab.balance_due:.2f}."
                )
            if tab.credit_balance > ZERO:
                raise TabCreditBalancePendingError(
                    f"Não é possível encerrar a comanda #{tab.number}: ainda há saldo de crédito disponível de R$ {tab.credit_balance:.2f} a devolver ao cliente."
                )
            if tab.net_principal_paid > tab.subtotal_consumed:
                raise TabIncompleteOperationsError(
                    f"Não é possível encerrar a comanda #{tab.number}: há operações financeiras pendentes de conciliação."
                )

            now = self._clock.now()
            uow.tabs.close_tab(
                tab_id=tab.id,
                closed_by=actor.id,
                closed_at=now,
                notes=clean_notes,
            )

            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_closed",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                details=(
                    f"number={tab.number} consumed={tab.subtotal_consumed} "
                    f"credit_applied={tab.credit_applied} credit_refunded={tab.credit_refunded} "
                    f"principal_paid={tab.principal_paid} card_fee={tab.card_fee_paid}"
                ),
            )

            closed = uow.tabs.get_by_id(tab.id)
            assert closed is not None
            return closed

    def void_tab_item(
        self,
        actor: User,
        tab_id: int,
        sale_id: int,
        reason: str,
        customer_refund_completed: bool = False,
        customer_refund_method: Optional[Union[str, PaymentMethod]] = None,
        customer_refund_notes: Optional[str] = None,
    ) -> Tuple[Tab, Sale]:
        """Cancela contabilmente um lançamento de consumo de uma comanda aberta.

        Regras financeiras (§10):
        - Exige perfil `admin`, motivo obrigatório e turno aberto.
        - Não permite alterar comanda já encerrada.
        - Quando não há excesso de pagamentos sobre o consumo ativo restante (`net_principal_paid <= consumed_after`),
          o lançamento é cancelado com `customer_refund_status = 'not_applicable'` e `customer_refund_amount = 0.00`.
        - Quando há pagamento associado que passa a exceder o consumo ativo restante (`net_principal_paid > consumed_after`),
          NÃO gera devolução fictícia automática nem presume devolução automática de taxa de cartão pela maquininha:
          se `customer_refund_completed = False` (padrão), registra obrigação de devolução pendente
          (`customer_refund_status = 'pending'`) pelo principal efetivamente excedente, mantendo o dinheiro no
          livro-razão até a confirmação da devolução efetiva.
        """
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem cancelar lançamentos de comanda.")

        if not isinstance(reason, str):
            raise InvalidInputError("O motivo do cancelamento é obrigatório.")
        clean_reason = " ".join(reason.strip().split())
        if len(clean_reason) < 3 or len(clean_reason) > 255:
            raise InvalidInputError("Informe um motivo de cancelamento entre 3 e 255 caracteres.")

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
            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Não é permitido cancelar lançamentos de uma comanda já encerrada.")

            sale = uow.sales.get_by_id(sale_id, for_update=True)
            if sale is None or sale.tab_id != tab.id or sale.kind != SaleKind.TAB_ITEM:
                raise SaleNotFoundError("Lançamento de consumo não encontrado nesta comanda.")
            if sale.status == SaleStatus.VOIDED:
                raise SaleAlreadyVoidedError("Este lançamento já foi cancelado.")

            shift = uow.shifts.get_by_id(sale.shift_id, for_update=True)
            if shift is None or shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido cancelar lançamentos pertencentes a um turno já fechado.")

            if tab.pending_refund_total > ZERO:
                raise TabIncompleteOperationsError(
                    "Confirme a devolução pendente anterior nesta comanda antes de cancelar outro lançamento pago."
                )

            now = self._clock.now()
            consumed_after = tab.subtotal_consumed - sale.subtotal
            credit_pool = max(ZERO, tab.credit_deposited - tab.credit_refunded)
            target_credit_applied = min(consumed_after, credit_pool)
            ordinary_needed_after = max(ZERO, consumed_after - target_credit_applied)
            active_ordinary = max(ZERO, tab.net_principal_paid - tab.pending_refund_total)
            excess_principal = max(ZERO, active_ordinary - ordinary_needed_after)
            credit_restored = max(ZERO, tab.credit_applied - target_credit_applied)

            if excess_principal <= ZERO:
                refund_status = CustomerRefundStatus.NOT_APPLICABLE
                refund_amount = ZERO
                reversal_plan = []
            else:
                reversal_plan = plan_tab_excess_reversals(tab.payments, excess_principal)
                refund_amount = sum((gross for _, _, _, gross in reversal_plan), ZERO)
                if customer_refund_completed:
                    refund_status = CustomerRefundStatus.COMPLETED
                    if refund_method_enum is None and len(reversal_plan) == 1:
                        refund_method_enum = reversal_plan[0][0].method
                else:
                    refund_status = CustomerRefundStatus.PENDING

            uow.sales.mark_voided(
                sale_id=sale.id,
                voided_by=actor.id,
                voided_at=now,
                reason=clean_reason,
                customer_refund_completed=(refund_status == CustomerRefundStatus.COMPLETED),
                customer_refund_method=refund_method_enum,
                customer_refund_notes=clean_notes,
                customer_refund_status=refund_status,
                customer_refund_amount=refund_amount,
            )

            if refund_status == CustomerRefundStatus.COMPLETED and reversal_plan:
                self._record_tab_reversals(
                    uow=uow,
                    tab=tab,
                    sale=sale,
                    reversal_plan=reversal_plan,
                    actor=actor,
                    now=now,
                    note=f"Devolução cancelamento lançamento #{sale.id} comanda #{tab.number}: {clean_reason}"[:255],
                )

            details_str = (
                f"number={tab.number} sale_id={sale.id} subtotal={sale.subtotal} "
                f"credit_restored={credit_restored} "
                f"customer_refund_status={refund_status.value} refund_amount={refund_amount}"
            )
            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_item_voided",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                reason=clean_reason,
                details=details_str,
            )
            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_item_voided",
                entity="sale",
                entity_id=sale.id,
                created_at=now,
                reason=clean_reason,
                details=details_str,
            )

            updated_tab = uow.tabs.get_by_id(tab.id)
            updated_sale = uow.sales.get_by_id(sale.id)
            assert updated_tab is not None and updated_sale is not None
            return updated_tab, updated_sale

    @staticmethod
    def _record_tab_reversals(
        uow: UnitOfWork,
        tab: Tab,
        sale: Sale,
        reversal_plan: List[Tuple[CashMovement, Decimal, Decimal, Decimal]],
        actor: User,
        now,
        note: str,
    ) -> None:
        for orig_mov, take_principal, take_fee, take_gross in reversal_plan:
            rev_key = f"void:tab:{tab.id}:sale:{sale.id}:mov:{orig_mov.id}"[:64]
            uow.cash_movements.add(
                shift_id=sale.shift_id,
                tab_id=tab.id,
                sale_id=sale.id,
                kind=CashMovementKind.REVERSAL,
                method=orig_mov.method,
                direction=CashDirection.OUT,
                amount=take_principal,
                fee_percent=orig_mov.fee_percent if (orig_mov.method == PaymentMethod.CARD and take_fee > ZERO) else ZERO,
                fee_mode=orig_mov.fee_mode if (orig_mov.method == PaymentMethod.CARD and take_fee > ZERO) else "none",
                fee_amount=take_fee,
                gross_amount=take_gross,
                cash_tendered=None,
                change_given=None,
                reverses_id=orig_mov.id,
                operator_id=actor.id,
                created_at=now,
                idempotency_key=rev_key,
                note=note,
            )

    def confirm_tab_customer_refund(
        self,
        actor: User,
        tab_id: int,
        sale_id: int,
        method: Union[str, PaymentMethod],
        notes: Optional[str] = None,
    ) -> Tuple[Tab, Sale]:
        """Confirma que a devolução financeira efetiva ao cliente referente a um lançamento
        cancelado de comanda foi concluída, registrando os movimentos `direction = 'out'`."""
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem registrar devolução ao cliente.")

        try:
            method_enum = method if isinstance(method, PaymentMethod) else PaymentMethod(str(method))
        except ValueError as exc:
            raise InvalidInputError("Meio de devolução inválido.") from exc

        clean_notes = _validate_notes(notes)

        with self._uow() as uow:
            tab = uow.tabs.get_by_id(tab_id, for_update=True)
            if tab is None:
                raise TabNotFoundError("Comanda não encontrada.")
            if tab.status != TabStatus.OPEN:
                raise TabClosedError("Não é permitido alterar registros de uma comanda já encerrada.")

            sale = uow.sales.get_by_id(sale_id, for_update=True)
            if sale is None or sale.tab_id != tab.id or sale.kind != SaleKind.TAB_ITEM:
                raise SaleNotFoundError("Lançamento de consumo não encontrado nesta comanda.")
            if sale.status != SaleStatus.VOIDED:
                raise InvalidInputError("Apenas lançamentos cancelados podem ter devolução ao cliente confirmada.")
            if sale.customer_refund_status == CustomerRefundStatus.COMPLETED:
                raise InvalidInputError("A devolução ao cliente deste lançamento já foi registrada como concluída.")
            if sale.customer_refund_status == CustomerRefundStatus.NOT_APPLICABLE:
                raise InvalidInputError("Este lançamento não possui devolução financeira pendente ao cliente.")

            shift = uow.shifts.get_by_id(sale.shift_id, for_update=True)
            if shift is None or shift.status != ShiftStatus.OPEN:
                raise ShiftClosedError("Não é permitido alterar registros de um turno já fechado.")

            now = self._clock.now()
            excess_principal = sale.customer_refund_amount
            reversal_plan = plan_tab_excess_reversals(tab.payments, excess_principal)

            self._record_tab_reversals(
                uow=uow,
                tab=tab,
                sale=sale,
                reversal_plan=reversal_plan,
                actor=actor,
                now=now,
                note=f"Devolução efetiva comanda #{tab.number} lançamento #{sale.id} ({method_enum.value})"[:255],
            )

            uow.sales.mark_customer_refunded(
                sale_id=sale.id,
                refunded_by=actor.id,
                refunded_at=now,
                method=method_enum,
                notes=clean_notes,
            )

            details_str = f"number={tab.number} sale_id={sale.id} method={method_enum.value} amount={sale.customer_refund_amount}"
            uow.audit_logs.add(
                operator_id=actor.id,
                action="tab_customer_refund_completed",
                entity="tab",
                entity_id=tab.id,
                created_at=now,
                details=details_str,
            )
            uow.audit_logs.add(
                operator_id=actor.id,
                action="customer_refund_completed",
                entity="sale",
                entity_id=sale.id,
                created_at=now,
                details=details_str,
            )

            updated_tab = uow.tabs.get_by_id(tab.id)
            updated_sale = uow.sales.get_by_id(sale.id)
            assert updated_tab is not None and updated_sale is not None
            return updated_tab, updated_sale
