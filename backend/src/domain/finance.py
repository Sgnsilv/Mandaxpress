"""Regras e cálculos financeiros puros do domínio (Decimal exato, sem float)."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import List, Optional, Sequence, Tuple, Union

from .errors import InvalidInputError, InvalidPaymentError
from .models import (
    CardFeeMode,
    CashDirection,
    CashMovement,
    CashMovementKind,
    PaymentLinePreview,
    PaymentMethod,
)

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
MAX_AMOUNT = Decimal("999999.99")


def parse_money(value: Union[str, int, Decimal], field_name: str = "valor",
                allow_zero: bool = True, max_places: int = 2) -> Decimal:
    """Converte string, int ou Decimal em Decimal quantizado em 2 casas.

    Rejeita explicitamente float, boolean, NaN, infinito, valores negativos e
    números com mais casas decimais do que o permitido.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise InvalidInputError(f"O campo '{field_name}' deve ser informado como texto decimal ou Decimal exato (float não é permitido).")
    if value is None:
        raise InvalidInputError(f"O campo '{field_name}' é obrigatório.")
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        if not cleaned:
            raise InvalidInputError(f"O campo '{field_name}' não pode ser vazio.")
        raw = cleaned
    elif isinstance(value, (int, Decimal)):
        raw = str(value)
    else:
        raise InvalidInputError(f"Tipo inválido para '{field_name}'.")

    try:
        dec = Decimal(raw)
    except InvalidOperation as exc:
        raise InvalidInputError(f"Valor monetário inválido em '{field_name}'.") from exc

    if not dec.is_finite():
        raise InvalidInputError(f"Valor inválido em '{field_name}'.")

    # Verifica se há mais de max_places casas decimais informadas
    exponent = dec.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > max_places:
        raise InvalidInputError(f"O campo '{field_name}' deve ter no máximo {max_places} casas decimais.")

    quantized = dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if quantized < ZERO:
        raise InvalidInputError(f"O campo '{field_name}' não pode ser negativo.")
    if not allow_zero and quantized <= ZERO:
        raise InvalidInputError(f"O campo '{field_name}' deve ser maior que zero.")
    if quantized > MAX_AMOUNT:
        raise InvalidInputError(f"O campo '{field_name}' excede o limite permitido.")
    return quantized


def parse_percentage(value: Union[str, int, Decimal], field_name: str = "percentual") -> Decimal:
    """Valida um percentual de taxa em [0.00, 99.99]."""
    pct = parse_money(value, field_name=field_name, allow_zero=True, max_places=2)
    if pct >= HUNDRED:
        raise InvalidInputError(f"O campo '{field_name}' deve ser menor que 100%.")
    return pct


def round_money(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def calculate_card_fee(
    principal_amount: Decimal,
    fee_percent: Decimal,
    fee_mode: CardFeeMode = CardFeeMode.SIMPLE_ADDITION,
) -> Decimal:
    """Calcula a taxa do cartão sobre a parcela de principal paga no cartão.

    - `SIMPLE_ADDITION`: acréscimo simples sobre a parcela:
        fee = round(principal * (fee_percent / 100), 2)
        gross = principal + fee
    - `PASS_THROUGH`: repasse de taxa descontada sobre o valor bruto cobrado
      (para que o valor líquido após desconto de `fee_percent` sobre o bruto seja `principal`):
        gross = round(principal / (1 - fee_percent / 100), 2)
        fee = gross - principal
    """
    principal = parse_money(principal_amount, "principal", allow_zero=False)
    pct = parse_percentage(fee_percent, "taxa_cartao")
    if pct == ZERO:
        return ZERO

    rate = pct / HUNDRED
    if fee_mode == CardFeeMode.SIMPLE_ADDITION:
        return round_money(principal * rate)
    if fee_mode == CardFeeMode.PASS_THROUGH:
        gross = round_money(principal / (Decimal("1") - rate))
        return gross - principal
    raise InvalidInputError(f"Modo de cálculo de taxa desconhecido: {fee_mode}")


def build_payment_line_preview(
    method: PaymentMethod,
    principal_amount: Decimal,
    card_fee_percent: Decimal,
    card_fee_mode: CardFeeMode,
    cash_tendered: Optional[Decimal] = None,
) -> PaymentLinePreview:
    """Calcula taxa, bruto cobrado e troco para uma parcela de pagamento."""
    amount = parse_money(principal_amount, "valor do pagamento", allow_zero=False)

    if method == PaymentMethod.CARD:
        fee_pct = parse_percentage(card_fee_percent, "taxa de cartão")
        fee_amt = calculate_card_fee(amount, fee_pct, card_fee_mode)
        fee_mode_str = card_fee_mode.value
        gross = amount + fee_amt
        if cash_tendered is not None:
            raise InvalidPaymentError("Valor entregue em dinheiro só pode ser informado para pagamentos em dinheiro.")
        return PaymentLinePreview(
            method=method,
            amount=amount,
            fee_percent=fee_pct,
            fee_mode=fee_mode_str,
            fee_amount=fee_amt,
            gross_amount=gross,
            cash_tendered=None,
            change_given=None,
        )

    if method == PaymentMethod.PIX:
        if cash_tendered is not None:
            raise InvalidPaymentError("Valor entregue em dinheiro só pode ser informado para pagamentos em dinheiro.")
        return PaymentLinePreview(
            method=method,
            amount=amount,
            fee_percent=ZERO,
            fee_mode="none",
            fee_amount=ZERO,
            gross_amount=amount,
            cash_tendered=None,
            change_given=None,
        )

    if method == PaymentMethod.CASH:
        tendered = amount if cash_tendered is None else parse_money(cash_tendered, "valor entregue", allow_zero=False)
        if tendered < amount:
            raise InvalidPaymentError("O valor entregue em dinheiro é menor que a parcela em dinheiro.")
        change = tendered - amount
        return PaymentLinePreview(
            method=method,
            amount=amount,
            fee_percent=ZERO,
            fee_mode="none",
            fee_amount=ZERO,
            gross_amount=amount,
            cash_tendered=tendered,
            change_given=change,
        )

    raise InvalidInputError(f"Forma de pagamento inválida: {method}")


def plan_tab_excess_reversals(
    payments: Sequence[CashMovement],
    excess_principal: Decimal,
) -> List[Tuple[CashMovement, Decimal, Decimal, Decimal]]:
    """Aloca o excesso de principal pago em uma comanda quando o consumo ativo
    passa a ser menor que o principal líquido recebido.

    Regras financeiras para pagamentos mistos e taxas de cartão:
    1. Prioriza a reversão sobre pagamentos sem taxa de operadora (`cash` e `pix`, em ordem LIFO)
       antes de alcançar parcelas pagas em `card` (também em ordem LIFO). Assim, em pagamentos
       divididos ou parciais mistos, o resultado independe da ordem em que o operador lançou
       as linhas do pagamento dividido e preserva a transação de cartão sempre que possível.
    2. Não presume devolução automática nem proporcional da taxa de cartão pela maquininha:
       o cancelamento de consumo na comanda gera obrigação de devolução restrita ao principal
       excedente (`fee_to_reverse = 0.00`, `gross_to_reverse = principal_to_reverse`), mantendo
       a taxa originalmente cobrada na transação de cartão registrada separadamente no livro-razão.

    Retorna lista de `(original_movement, principal_to_reverse, fee_to_reverse, gross_to_reverse)`.
    """
    if excess_principal <= ZERO:
        return []

    originals = [
        m for m in payments
        if m.kind == CashMovementKind.SALE_PAYMENT and m.direction == CashDirection.IN
    ]
    reversals = [
        m for m in payments
        if m.kind == CashMovementKind.REVERSAL and m.direction == CashDirection.OUT
    ]

    reversed_principal_by_id = {}
    for r in reversals:
        if r.reverses_id is not None:
            reversed_principal_by_id[r.reverses_id] = (
                reversed_principal_by_id.get(r.reverses_id, ZERO) + r.amount
            )

    # 1º: Pagamentos sem taxa (dinheiro/Pix) em LIFO; 2º: Pagamentos em cartão em LIFO.
    non_card_lifo = [m for m in reversed(originals) if m.method != PaymentMethod.CARD]
    card_lifo = [m for m in reversed(originals) if m.method == PaymentMethod.CARD]
    ordered_candidates = non_card_lifo + card_lifo

    remaining = excess_principal
    plan: List[Tuple[CashMovement, Decimal, Decimal, Decimal]] = []

    for mov in ordered_candidates:
        if remaining <= ZERO:
            break
        avail_principal = mov.amount - reversed_principal_by_id.get(mov.id, ZERO)
        if avail_principal <= ZERO:
            continue

        take_principal = min(remaining, avail_principal)
        # Não presume devolução automática ou proporcional de taxa pela maquininha:
        take_fee = ZERO
        take_gross = take_principal
        plan.append((mov, take_principal, take_fee, take_gross))
        remaining -= take_principal

    return plan

