"""Casos de uso de turno de caixa: abertura explícita, sangria, suprimento,
conferência por método, bloqueio por pendências, encerramento atômico e relatório histórico."""
import hashlib
import json
import time
from decimal import Decimal
from typing import Callable, List, Optional, Tuple, Union

import pymysql

from ..domain.errors import (
    IdempotencyConflictError,
    InvalidInputError,
    ShiftAlreadyOpenError,
    ShiftCloseBlockedError,
    ShiftClosedError,
    ShiftNotFoundError,
)
from ..domain.finance import ZERO, parse_money
from ..domain.models import (
    CashDirection,
    CashMovement,
    CashMovementKind,
    PaymentMethod,
    Shift,
    ShiftStatus,
    TabStatus,
    User,
)
from ..domain.ports import Clock, UnitOfWork
from .sales import _validate_idempotency_key

_DUPLICATE_ENTRY = 1062
_LOCK_DEADLOCK = 1213
_LOCK_WAIT_TIMEOUT = 1205


def _validate_optional_notes(notes: Optional[str], max_len: int = 255) -> Optional[str]:
    if notes is None:
        return None
    if not isinstance(notes, str):
        raise InvalidInputError("Observação inválida.")
    cleaned = " ".join(notes.strip().split())
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        raise InvalidInputError(f"A observação deve ter no máximo {max_len} caracteres.")
    return cleaned


def _validate_mandatory_reason(reason: Optional[str], field_label: str = "motivo") -> str:
    if not isinstance(reason, str):
        raise InvalidInputError(f"O {field_label} é obrigatório.")
    cleaned = " ".join(reason.strip().split())
    if len(cleaned) < 3 or len(cleaned) > 255:
        raise InvalidInputError(f"Informe um {field_label} entre 3 e 255 caracteres.")
    return cleaned


def _diff_status(diff: Optional[Decimal]) -> str:
    if diff is None:
        return "pending"
    if diff > ZERO:
        return "surplus"
    if diff < ZERO:
        return "shortage"
    return "exact"


def serialize_shift(shift: Shift) -> dict:
    return {
        "id": shift.id,
        "status": shift.status.value,
        "opened_by": shift.opened_by,
        "opened_by_name": shift.opened_by_name,
        "opened_at": shift.opened_at.isoformat(),
        "opening_cash": f"{shift.opening_cash:.2f}",
        "closed_by": shift.closed_by,
        "closed_by_name": shift.closed_by_name,
        "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
        "expected_cash": f"{shift.expected_cash:.2f}" if shift.expected_cash is not None else None,
        "counted_cash": f"{shift.counted_cash:.2f}" if shift.counted_cash is not None else None,
        "cash_difference": f"{shift.cash_difference:.2f}" if shift.cash_difference is not None else None,
        "expected_pix": f"{shift.expected_pix:.2f}" if shift.expected_pix is not None else None,
        "counted_pix": f"{shift.counted_pix:.2f}" if shift.counted_pix is not None else None,
        "pix_difference": f"{shift.pix_difference:.2f}" if shift.pix_difference is not None else None,
        "expected_card": f"{shift.expected_card:.2f}" if shift.expected_card is not None else None,
        "counted_card": f"{shift.counted_card:.2f}" if shift.counted_card is not None else None,
        "card_difference": f"{shift.card_difference:.2f}" if shift.card_difference is not None else None,
        "discrepancy_justification": shift.discrepancy_justification,
        "notes": shift.notes,
    }


def serialize_drawer_movement(mov: Union[CashMovement, dict]) -> dict:
    if isinstance(mov, dict):
        return mov
    return {
        "id": mov.id,
        "shift_id": mov.shift_id,
        "kind": mov.kind.value,
        "method": mov.method.value,
        "direction": mov.direction.value,
        "amount": f"{mov.amount:.2f}",
        "operator_id": mov.operator_id,
        "created_at": mov.created_at.isoformat(),
        "reason": mov.note or "",
        "note": mov.note,
    }


class ShiftService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock):
        self._uow = uow_factory
        self._clock = clock

    def open_shift(
        self,
        actor: User,
        opening_cash: Union[str, int, float, Decimal] = "0.00",
        idempotency_key: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Tuple[Shift, bool]:
        """Abre explicitamente um turno de caixa com o fundo inicial informado.

        - Registra operador, data/hora e valor inicial (`DECIMAL(10,2)` >= 0.00).
        - Impede dois turnos abertos simultaneamente (validação transacional + constraint única no banco).
        - Garante idempotência quando `idempotency_key` é informada.
        - Não contabiliza o fundo inicial como venda ou receita de cliente.
        """
        opening_cash_dec = parse_money(
            opening_cash,
            field_name="fundo inicial em dinheiro",
            allow_zero=True,
        )
        clean_notes = _validate_optional_notes(notes)
        clean_key = _validate_idempotency_key(idempotency_key) if idempotency_key is not None else None

        fingerprint: Optional[str] = None
        if clean_key is not None:
            canonical = json.dumps(
                {
                    "op": "shift_open",
                    "opening_cash": f"{opening_cash_dec:.2f}",
                    "notes": clean_notes or "",
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                with self._uow() as uow:
                    if clean_key is not None:
                        existing_by_key = uow.shifts.get_by_open_idempotency_key(clean_key, for_update=True)
                        if existing_by_key is not None:
                            if existing_by_key.open_request_fingerprint != fingerprint:
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada na abertura de turno com dados diferentes."
                                )
                            return existing_by_key, False

                    current_open = uow.shifts.get_open_shift(for_update=True)
                    if current_open is not None:
                        raise ShiftAlreadyOpenError(
                            f"Já existe um turno de caixa aberto (Turno #{current_open.id}). "
                            "Encerre o turno atual antes de abrir um novo."
                        )

                    now = self._clock.now()
                    created = uow.shifts.create_open_shift(
                        opened_by=actor.id,
                        now=now,
                        opening_cash=opening_cash_dec,
                        notes=clean_notes,
                        open_idempotency_key=clean_key,
                        open_request_fingerprint=fingerprint,
                    )

                    uow.audit_logs.add(
                        operator_id=actor.id,
                        action="shift_opened",
                        entity="shift",
                        entity_id=created.id,
                        created_at=now,
                        details=f"opening_cash={opening_cash_dec:.2f} notes={clean_notes or ''}",
                    )
                    return created, True
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                    with self._uow() as uow:
                        if clean_key is not None:
                            existing_by_key = uow.shifts.get_by_open_idempotency_key(clean_key)
                            if existing_by_key is not None:
                                if existing_by_key.open_request_fingerprint != fingerprint:
                                    raise IdempotencyConflictError(
                                        "Esta chave de idempotência já foi utilizada na abertura de turno com dados diferentes."
                                    ) from exc
                                return existing_by_key, False
                        current_open = uow.shifts.get_open_shift()
                        if current_open is not None:
                            raise ShiftAlreadyOpenError(
                                f"Já existe um turno de caixa aberto (Turno #{current_open.id}). "
                                "Encerre o turno atual antes de abrir um novo."
                            ) from exc
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir a abertura do turno após novas tentativas.")

    def record_cash_withdrawal(
        self,
        actor: User,
        amount: Union[str, int, float, Decimal],
        reason: str,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[CashMovement, bool]:
        """Registra uma sangria (`kind = 'cash_withdrawal'`, `direction = 'out'`, `method = 'cash'`)."""
        return self._record_drawer_movement(
            actor=actor,
            kind=CashMovementKind.CASH_WITHDRAWAL,
            direction=CashDirection.OUT,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key,
            field_label="valor da sangria",
            audit_action="cash_withdrawal_recorded",
        )

    def record_cash_supply(
        self,
        actor: User,
        amount: Union[str, int, float, Decimal],
        reason: str,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[CashMovement, bool]:
        """Registra um suprimento (`kind = 'cash_supply'`, `direction = 'in'`, `method = 'cash'`)."""
        return self._record_drawer_movement(
            actor=actor,
            kind=CashMovementKind.CASH_SUPPLY,
            direction=CashDirection.IN,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key,
            field_label="valor do suprimento",
            audit_action="cash_supply_recorded",
        )

    def _record_drawer_movement(
        self,
        actor: User,
        kind: CashMovementKind,
        direction: CashDirection,
        amount: Union[str, int, float, Decimal],
        reason: str,
        idempotency_key: Optional[str],
        field_label: str,
        audit_action: str,
    ) -> Tuple[CashMovement, bool]:
        parsed_amount = parse_money(amount, field_name=field_label, allow_zero=False)
        clean_reason = _validate_mandatory_reason(reason, field_label="motivo")
        clean_key = (
            _validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else None
        )

        canonical = json.dumps(
            {
                "op": kind.value,
                "amount": f"{parsed_amount:.2f}",
                "reason": clean_reason,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                with self._uow() as uow:
                    if clean_key is not None:
                        existing = uow.cash_movements.get_by_idempotency_key(clean_key, for_update=True)
                        if existing is not None:
                            if existing.kind != kind or existing.request_fingerprint != fingerprint:
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                                )
                            return existing, False

                    shift = uow.shifts.get_open_shift(for_update=True)
                    if shift is None or shift.status != ShiftStatus.OPEN:
                        raise ShiftClosedError(
                            "Não há turno de caixa aberto. Abra o caixa antes de registrar sangria ou suprimento."
                        )

                    now = self._clock.now()
                    generated_key = clean_key or f"drawer:{kind.value}:{shift.id}:{int(now.timestamp() * 1000000)}"[:64]

                    mov = uow.cash_movements.add(
                        shift_id=shift.id,
                        sale_id=None,
                        tab_id=None,
                        kind=kind,
                        method=PaymentMethod.CASH,
                        direction=direction,
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
                        idempotency_key=generated_key,
                        batch_key=clean_key,
                        request_fingerprint=fingerprint if clean_key is not None else None,
                        note=clean_reason,
                    )

                    uow.audit_logs.add(
                        operator_id=actor.id,
                        action=audit_action,
                        entity="cash_movement",
                        entity_id=mov.id,
                        created_at=now,
                        reason=clean_reason,
                        details=f"shift_id={shift.id} kind={kind.value} amount={parsed_amount:.2f}",
                    )
                    return mov, True
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY and clean_key is not None:
                    with self._uow() as uow:
                        existing = uow.cash_movements.get_by_idempotency_key(clean_key)
                        if existing is not None:
                            if existing.kind != kind or existing.request_fingerprint != fingerprint:
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada com dados diferentes."
                                ) from exc
                            return existing, False
                        raise IdempotencyConflictError(
                            "Esta chave de idempotência já foi utilizada em outra operação."
                        ) from exc
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível registrar a movimentação de gaveta após novas tentativas.")

    def _build_shift_analysis(
        self,
        uow: UnitOfWork,
        shift: Shift,
        counted_cash: Optional[Decimal] = None,
        counted_pix: Optional[Decimal] = None,
        counted_card: Optional[Decimal] = None,
        discrepancy_justification: Optional[str] = None,
    ) -> dict:
        summary = uow.cash_movements.get_financial_summary(shift_id=shift.id)
        drawer_movements = uow.cash_movements.list_drawer_movements(shift_id=shift.id)
        pending_refunds = uow.cash_movements.list_pending_refunds(shift_id=shift.id)
        shift_tabs = uow.tabs.list_tabs(shift_id=shift.id, limit=500)

        open_tabs = [t for t in shift_tabs if t.status == TabStatus.OPEN]
        closed_tabs = [t for t in shift_tabs if t.status == TabStatus.CLOSED]

        open_tabs_count = len(open_tabs)
        unpaid_consumption_total = sum((t.balance_due for t in open_tabs), ZERO)
        unrefunded_credit_total = sum((t.credit_balance for t in open_tabs), ZERO)
        pending_refunds_total = sum((Decimal(item["refund_amount"]) for item in pending_refunds), ZERO)

        incomplete_tabs = [
            t
            for t in open_tabs
            if t.pending_refund_total > ZERO or t.net_principal_paid > t.subtotal_consumed
        ]
        incomplete_operations_count = len(pending_refunds) + sum(
            1 for t in open_tabs if t.net_principal_paid > t.subtotal_consumed and t.pending_refund_total == ZERO
        )

        open_tabs_serialized = [
            {
                "id": t.id,
                "number": t.number,
                "customer_name": t.customer_name,
                "subtotal_consumed": f"{t.subtotal_consumed:.2f}",
                "credit_deposited": f"{t.credit_deposited:.2f}",
                "credit_applied": f"{t.credit_applied:.2f}",
                "credit_refunded": f"{t.credit_refunded:.2f}",
                "credit_balance": f"{t.credit_balance:.2f}",
                "principal_paid": f"{t.principal_paid:.2f}",
                "balance_due": f"{t.balance_due:.2f}",
                "pending_refund_total": f"{t.pending_refund_total:.2f}",
            }
            for t in open_tabs
        ]
        tabs_with_credit = [
            item for item in open_tabs_serialized if Decimal(item["credit_balance"]) > ZERO
        ]

        blockers = []
        if open_tabs_count > 0:
            tab_numbers = ", ".join(f"#{t.number}" for t in open_tabs)
            blockers.append({
                "code": "open_tabs",
                "message": (
                    f"Existem {open_tabs_count} comanda(s) aberta(s) ({tab_numbers}). "
                    f"Consumo pendente de pagamento: R$ {unpaid_consumption_total:.2f}."
                ),
                "count": open_tabs_count,
                "amount": f"{unpaid_consumption_total:.2f}",
            })
        if unrefunded_credit_total > ZERO:
            credit_tab_numbers = ", ".join(
                f"#{t.number} (R$ {t.credit_balance:.2f})" for t in open_tabs if t.credit_balance > ZERO
            )
            blockers.append({
                "code": "unrefunded_credit",
                "message": (
                    f"Há saldo de crédito antecipado não utilizado/não devolvido no total de "
                    f"R$ {unrefunded_credit_total:.2f} ({credit_tab_numbers})."
                ),
                "count": len(tabs_with_credit),
                "amount": f"{unrefunded_credit_total:.2f}",
            })
        if len(pending_refunds) > 0:
            blockers.append({
                "code": "pending_refunds",
                "message": (
                    f"Existem {len(pending_refunds)} obrigação(ões) de devolução pendente(s) ao cliente "
                    f"no total de R$ {pending_refunds_total:.2f}."
                ),
                "count": len(pending_refunds),
                "amount": f"{pending_refunds_total:.2f}",
            })

        pendencies = {
            "open_tabs_count": open_tabs_count,
            "closed_tabs_count": len(closed_tabs),
            "unpaid_consumption_total": f"{unpaid_consumption_total:.2f}",
            "unrefunded_credit_total": f"{unrefunded_credit_total:.2f}",
            "pending_refunds_count": len(pending_refunds),
            "pending_refunds_total": f"{pending_refunds_total:.2f}",
            "incomplete_operations_count": incomplete_operations_count,
            "open_tabs": open_tabs_serialized,
            "tabs_with_credit_balance": tabs_with_credit,
            "pending_refunds": pending_refunds,
            "blockers": blockers,
            "can_close": len(blockers) == 0,
        }

        expected_cash = Decimal(summary["expected_cash"])
        expected_pix = Decimal(summary["expected_pix"])
        expected_card = Decimal(summary["expected_card"])

        eff_counted_cash = counted_cash if counted_cash is not None else shift.counted_cash
        eff_counted_pix = counted_pix if counted_pix is not None else shift.counted_pix
        eff_counted_card = counted_card if counted_card is not None else shift.counted_card
        eff_justification = (
            discrepancy_justification
            if discrepancy_justification is not None
            else shift.discrepancy_justification
        )

        cash_diff = (eff_counted_cash - expected_cash) if eff_counted_cash is not None else None
        pix_diff = (eff_counted_pix - expected_pix) if eff_counted_pix is not None else None
        card_diff = (eff_counted_card - expected_card) if eff_counted_card is not None else None

        has_discrepancy = any(
            d is not None and d != ZERO for d in (cash_diff, pix_diff, card_diff)
        )

        by_method = summary["by_method"]
        conference = {
            "cash": {
                "opening_cash": summary["opening_cash"],
                "inflows": by_method["cash"]["gross_in"],
                "outflows": by_method["cash"]["gross_out"],
                "net_Transactional": by_method["cash"]["net_gross"],
                "supplies": summary["cash_supplies_total"],
                "supplies_count": summary["cash_supplies_count"],
                "withdrawals": summary["cash_withdrawals_total"],
                "withdrawals_count": summary["cash_withdrawals_count"],
                "expected_amount": f"{expected_cash:.2f}",
                "counted_amount": f"{eff_counted_cash:.2f}" if eff_counted_cash is not None else None,
                "difference": f"{cash_diff:.2f}" if cash_diff is not None else None,
                "status": _diff_status(cash_diff),
            },
            "pix": {
                "inflows": by_method["pix"]["gross_in"],
                "outflows": by_method["pix"]["gross_out"],
                "net_registered": f"{expected_pix:.2f}",
                "expected_amount": f"{expected_pix:.2f}",
                "counted_amount": f"{eff_counted_pix:.2f}" if eff_counted_pix is not None else None,
                "difference": f"{pix_diff:.2f}" if pix_diff is not None else None,
                "status": _diff_status(pix_diff),
            },
            "card": {
                "principal_in": by_method["card"]["principal_in"],
                "fee_in": by_method["card"]["fee_in"],
                "gross_in": by_method["card"]["gross_in"],
                "principal_out": by_method["card"]["principal_out"],
                "fee_out": by_method["card"]["fee_out"],
                "gross_out": by_method["card"]["gross_out"],
                "net_principal_registered": by_method["card"]["net_principal"],
                "net_fee_registered": by_method["card"]["net_fee"],
                "net_gross_registered": f"{expected_card:.2f}",
                "expected_amount": f"{expected_card:.2f}",
                "counted_amount": f"{eff_counted_card:.2f}" if eff_counted_card is not None else None,
                "difference": f"{card_diff:.2f}" if card_diff is not None else None,
                "status": _diff_status(card_diff),
                "settlement_note": (
                    "Valor bruto cobrado nas transações registradas no PDV (principal + taxa repassada). "
                    "O sistema não possui integração bancária e não presume descontos da adquirente nem liquidação imediata."
                ),
            },
            "has_discrepancy": has_discrepancy,
            "requires_justification": has_discrepancy,
            "discrepancy_justification": eff_justification,
        }

        return {
            "shift": serialize_shift(shift),
            "summary": summary,
            "conference": conference,
            "pendencies": pendencies,
            "drawer_movements": [serialize_drawer_movement(m) for m in drawer_movements],
        }

    def get_current_shift_state(self, history_limit: int = 20) -> dict:
        """Retorna o estado atual do caixa (aberto ou fechado), indicadores do turno aberto
        e histórico recente de turnos encerrados."""
        clean_limit = max(1, min(int(history_limit), 100))
        with self._uow() as uow:
            open_shift = uow.shifts.get_open_shift()
            history_shifts = uow.shifts.list_shifts(status=ShiftStatus.CLOSED, limit=clean_limit)

            current_analysis = (
                self._build_shift_analysis(uow, open_shift)
                if open_shift is not None
                else None
            )

            return {
                "is_open": open_shift is not None,
                "current_shift": current_analysis["shift"] if current_analysis else None,
                "summary": current_analysis["summary"] if current_analysis else None,
                "conference": current_analysis["conference"] if current_analysis else None,
                "pendencies": current_analysis["pendencies"] if current_analysis else None,
                "drawer_movements": current_analysis["drawer_movements"] if current_analysis else [],
                "closed_shifts": [serialize_shift(s) for s in history_shifts],
            }

    def list_shifts(
        self,
        status: Optional[Union[str, ShiftStatus]] = None,
        limit: int = 50,
    ) -> List[Shift]:
        status_enum: Optional[ShiftStatus] = None
        if status is not None and str(status).strip() and str(status).strip().lower() != "all":
            try:
                status_enum = (
                    status
                    if isinstance(status, ShiftStatus)
                    else ShiftStatus(str(status).strip().lower())
                )
            except ValueError as exc:
                raise InvalidInputError("Filtro de status de turno inválido.") from exc
        clean_limit = max(1, min(int(limit), 200))
        with self._uow() as uow:
            return uow.shifts.list_shifts(status=status_enum, limit=clean_limit)

    def get_shift_report(self, shift_id: Optional[int] = None) -> dict:
        """Consulta o relatório completo de um turno (aberto ou encerrado).
        Se o turno estiver encerrado e possuir `closing_snapshot`, retorna o snapshot imutável
        preservado no fechamento (atualizando metadados do turno se necessário)."""
        with self._uow() as uow:
            if shift_id is not None:
                if isinstance(shift_id, bool) or not isinstance(shift_id, int) or shift_id < 1:
                    raise InvalidInputError("Identificador de turno inválido.")
                shift = uow.shifts.get_by_id(shift_id)
                if shift is None:
                    raise ShiftNotFoundError("Turno de caixa não encontrado.")
            else:
                shift = uow.shifts.get_open_shift()
                if shift is None:
                    recent = uow.shifts.list_shifts(limit=1)
                    if not recent:
                        raise ShiftNotFoundError("Nenhum turno de caixa encontrado.")
                    shift = recent[0]

            if shift.status == ShiftStatus.CLOSED and shift.closing_snapshot:
                try:
                    snapshot = json.loads(shift.closing_snapshot)
                    snapshot["shift"] = serialize_shift(shift)
                    return snapshot
                except json.JSONDecodeError:
                    pass

            return self._build_shift_analysis(uow, shift)

    def preview_closing(
        self,
        counted_cash: Optional[Union[str, int, float, Decimal]] = None,
        counted_pix: Optional[Union[str, int, float, Decimal]] = None,
        counted_card: Optional[Union[str, int, float, Decimal]] = None,
        discrepancy_justification: Optional[str] = None,
        shift_id: Optional[int] = None,
    ) -> dict:
        """Gera a prévia completa de conferência e fechamento do turno aberto sem encerrá-lo."""
        cash_dec = (
            parse_money(counted_cash, field_name="valor contado em dinheiro", allow_zero=True)
            if counted_cash is not None and str(counted_cash).strip() != ""
            else None
        )
        pix_dec = (
            parse_money(counted_pix, field_name="valor conferido em Pix", allow_zero=True)
            if counted_pix is not None and str(counted_pix).strip() != ""
            else None
        )
        card_dec = (
            parse_money(counted_card, field_name="valor conferido em cartão", allow_zero=True)
            if counted_card is not None and str(counted_card).strip() != ""
            else None
        )
        clean_justification = _validate_optional_notes(discrepancy_justification, max_len=500)

        with self._uow() as uow:
            if shift_id is not None:
                if isinstance(shift_id, bool) or not isinstance(shift_id, int) or shift_id < 1:
                    raise InvalidInputError("Identificador de turno inválido.")
                shift = uow.shifts.get_by_id(shift_id)
                if shift is None:
                    raise ShiftNotFoundError("Turno de caixa não encontrado.")
            else:
                shift = uow.shifts.get_open_shift()
                if shift is None:
                    raise ShiftClosedError("Não há turno de caixa aberto no momento.")

            analysis = self._build_shift_analysis(
                uow=uow,
                shift=shift,
                counted_cash=cash_dec,
                counted_pix=pix_dec,
                counted_card=card_dec,
                discrepancy_justification=clean_justification,
            )

            validation_errors: List[str] = []
            if shift.status != ShiftStatus.OPEN:
                validation_errors.append("Este turno já está encerrado.")
            for blocker in analysis["pendencies"]["blockers"]:
                validation_errors.append(blocker["message"])
            if cash_dec is None:
                validation_errors.append("Informe o valor contado em dinheiro na gaveta.")
            if analysis["conference"]["has_discrepancy"] and (
                not clean_justification or len(clean_justification) < 3
            ):
                validation_errors.append(
                    "Há diferença entre o valor conferido e o esperado. Informe uma justificativa (mínimo de 3 caracteres)."
                )

            analysis["ready_to_close"] = len(validation_errors) == 0
            analysis["validation_errors"] = validation_errors
            return analysis

    def close_shift(
        self,
        actor: User,
        counted_cash: Union[str, int, float, Decimal],
        counted_pix: Optional[Union[str, int, float, Decimal]] = None,
        counted_card: Optional[Union[str, int, float, Decimal]] = None,
        discrepancy_justification: Optional[str] = None,
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        shift_id: Optional[int] = None,
    ) -> Tuple[Shift, dict, bool]:
        """Encerra o turno de caixa de forma atômica, exigindo conferência, ausência de pendências
        e justificativa quando houver divergência, persistindo snapshot imutável do fechamento."""
        if counted_cash is None or (isinstance(counted_cash, str) and not counted_cash.strip()):
            raise InvalidInputError("Informe o valor contado em dinheiro para encerrar o turno.")

        counted_cash_dec = parse_money(
            counted_cash,
            field_name="valor contado em dinheiro",
            allow_zero=True,
        )
        counted_pix_dec = (
            parse_money(counted_pix, field_name="valor conferido em Pix", allow_zero=True)
            if counted_pix is not None and str(counted_pix).strip() != ""
            else None
        )
        counted_card_dec = (
            parse_money(counted_card, field_name="valor conferido em cartão", allow_zero=True)
            if counted_card is not None and str(counted_card).strip() != ""
            else None
        )
        clean_justification = _validate_optional_notes(discrepancy_justification, max_len=500)
        clean_notes = _validate_optional_notes(notes, max_len=255)
        clean_key = (
            _validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else None
        )

        fingerprint: Optional[str] = None
        if clean_key is not None:
            canonical = json.dumps(
                {
                    "op": "shift_close",
                    "shift_id": shift_id,
                    "counted_cash": f"{counted_cash_dec:.2f}",
                    "counted_pix": f"{counted_pix_dec:.2f}" if counted_pix_dec is not None else None,
                    "counted_card": f"{counted_card_dec:.2f}" if counted_card_dec is not None else None,
                    "discrepancy_justification": clean_justification or "",
                    "notes": clean_notes or "",
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                with self._uow() as uow:
                    if clean_key is not None:
                        existing_closed = uow.shifts.get_by_close_idempotency_key(clean_key, for_update=True)
                        if existing_closed is not None:
                            if existing_closed.close_request_fingerprint != fingerprint:
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada no fechamento com dados diferentes."
                                )
                            snapshot = (
                                json.loads(existing_closed.closing_snapshot)
                                if existing_closed.closing_snapshot
                                else self._build_shift_analysis(uow, existing_closed)
                            )
                            snapshot["shift"] = serialize_shift(existing_closed)
                            return existing_closed, snapshot, False

                    if shift_id is not None:
                        if isinstance(shift_id, bool) or not isinstance(shift_id, int) or shift_id < 1:
                            raise InvalidInputError("Identificador de turno inválido.")
                        shift = uow.shifts.get_by_id(shift_id, for_update=True)
                        if shift is None:
                            raise ShiftNotFoundError("Turno de caixa não encontrado.")
                        if shift.status == ShiftStatus.CLOSED:
                            raise ShiftClosedError(
                                f"O Turno #{shift.id} já está encerrado e não pode ser fechado novamente."
                            )
                    else:
                        shift = uow.shifts.get_open_shift(for_update=True)
                        if shift is None:
                            raise ShiftClosedError("Não há turno de caixa aberto para encerrar.")

                    expected_summary = uow.cash_movements.get_financial_summary(shift_id=shift.id)
                    expected_cash = Decimal(expected_summary["expected_cash"])
                    expected_pix = Decimal(expected_summary["expected_pix"])
                    expected_card = Decimal(expected_summary["expected_card"])

                    final_counted_pix = counted_pix_dec if counted_pix_dec is not None else expected_pix
                    final_counted_card = counted_card_dec if counted_card_dec is not None else expected_card

                    analysis = self._build_shift_analysis(
                        uow=uow,
                        shift=shift,
                        counted_cash=counted_cash_dec,
                        counted_pix=final_counted_pix,
                        counted_card=final_counted_card,
                        discrepancy_justification=clean_justification,
                    )

                    blockers = analysis["pendencies"]["blockers"]
                    if blockers:
                        blocker_messages = " ".join(b["message"] for b in blockers)
                        raise ShiftCloseBlockedError(
                            f"Não é possível encerrar o turno enquanto houver pendências: {blocker_messages}",
                            details={"pendencies": analysis["pendencies"]},
                        )

                    cash_diff = counted_cash_dec - expected_cash
                    pix_diff = final_counted_pix - expected_pix
                    card_diff = final_counted_card - expected_card

                    has_discrepancy = (cash_diff != ZERO) or (pix_diff != ZERO) or (card_diff != ZERO)
                    if has_discrepancy:
                        if not clean_justification or len(clean_justification) < 3:
                            raise InvalidInputError(
                                "Há divergência entre os valores esperados e conferidos. "
                                "Informe uma justificativa (mínimo de 3 caracteres) para encerrar o turno."
                            )

                    now = self._clock.now()
                    merged_notes = clean_notes if clean_notes is not None else shift.notes

                    snapshot_payload = {
                        "shift": {
                            **serialize_shift(shift),
                            "status": ShiftStatus.CLOSED.value,
                            "closed_by": actor.id,
                            "closed_by_name": actor.name,
                            "closed_at": now.isoformat(),
                            "expected_cash": f"{expected_cash:.2f}",
                            "counted_cash": f"{counted_cash_dec:.2f}",
                            "cash_difference": f"{cash_diff:.2f}",
                            "expected_pix": f"{expected_pix:.2f}",
                            "counted_pix": f"{final_counted_pix:.2f}",
                            "pix_difference": f"{pix_diff:.2f}",
                            "expected_card": f"{expected_card:.2f}",
                            "counted_card": f"{final_counted_card:.2f}",
                            "card_difference": f"{card_diff:.2f}",
                            "discrepancy_justification": clean_justification,
                            "notes": merged_notes,
                        },
                        "summary": analysis["summary"],
                        "conference": analysis["conference"],
                        "pendencies": analysis["pendencies"],
                        "drawer_movements": analysis["drawer_movements"],
                    }
                    snapshot_json = json.dumps(snapshot_payload, ensure_ascii=False)

                    closed_shift = uow.shifts.close_shift(
                        shift_id=shift.id,
                        closed_by=actor.id,
                        now=now,
                        expected_cash=expected_cash,
                        counted_cash=counted_cash_dec,
                        cash_difference=cash_diff,
                        expected_pix=expected_pix,
                        counted_pix=final_counted_pix,
                        pix_difference=pix_diff,
                        expected_card=expected_card,
                        counted_card=final_counted_card,
                        card_difference=card_diff,
                        discrepancy_justification=clean_justification,
                        notes=merged_notes,
                        closing_snapshot=snapshot_json,
                        close_idempotency_key=clean_key,
                        close_request_fingerprint=fingerprint,
                    )

                    uow.audit_logs.add(
                        operator_id=actor.id,
                        action="shift_closed",
                        entity="shift",
                        entity_id=shift.id,
                        created_at=now,
                        reason=clean_justification,
                        details=(
                            f"expected_cash={expected_cash:.2f} counted_cash={counted_cash_dec:.2f} "
                            f"cash_diff={cash_diff:.2f} pix_diff={pix_diff:.2f} card_diff={card_diff:.2f}"
                        ),
                    )

                    snapshot_payload["shift"] = serialize_shift(closed_shift)
                    return closed_shift, snapshot_payload, True
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY and clean_key is not None:
                    with self._uow() as uow:
                        existing_closed = uow.shifts.get_by_close_idempotency_key(clean_key)
                        if existing_closed is not None:
                            if existing_closed.close_request_fingerprint != fingerprint:
                                raise IdempotencyConflictError(
                                    "Esta chave de idempotência já foi utilizada no fechamento com dados diferentes."
                                ) from exc
                            snapshot = (
                                json.loads(existing_closed.closing_snapshot)
                                if existing_closed.closing_snapshot
                                else self._build_shift_analysis(uow, existing_closed)
                            )
                            snapshot["shift"] = serialize_shift(existing_closed)
                            return existing_closed, snapshot, False
                raise
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] in (_LOCK_DEADLOCK, _LOCK_WAIT_TIMEOUT) and attempt < max_attempts:
                    time.sleep(0.02 * attempt)
                    continue
                raise
        raise RuntimeError("Não foi possível concluir o fechamento do turno após novas tentativas.")

    def reopen_shift(self, actor: User, shift_id: int) -> None:
        """Bloqueia qualquer tentativa de reabertura de turno encerrado no MVP."""
        raise ShiftClosedError("Turnos encerrados são imutáveis e não podem ser reabertos.")
