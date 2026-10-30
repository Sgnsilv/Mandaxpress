"""Testes automatizados do Bloco 5 (26 cenários obrigatórios §13):
abertura explícita de caixa, bloqueio sem turno aberto, sangria e suprimento,
saldo esperado em dinheiro, conferência por método, divergências e justificativa,
bloqueios por pendências, fechamento atômico, snapshot imutável, concorrência e regressão."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import pytest

from conftest import logged_in
from src.domain.errors import (
    IdempotencyConflictError,
    InvalidInputError,
    ShiftAlreadyOpenError,
    ShiftCloseBlockedError,
    ShiftClosedError,
)
from src.domain.models import (
    CashDirection,
    CashMovementKind,
    PaymentMethod,
    ShiftStatus,
)
from src.use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput


def test_01_open_shift_with_initial_cash(client, operator):
    """1. Abertura de caixa com fundo inicial válido, operador, horário e idempotência."""
    op_headers = logged_in(client, "caixa")

    state_before = client.get("/api/shifts/current", headers=op_headers).get_json()
    assert state_before["is_open"] is False
    assert state_before["current_shift"] is None

    resp = client.post(
        "/api/shifts/open",
        json={
            "opening_cash": "150.00",
            "idempotency_key": "open-shift-001",
            "notes": "Turno noturno Festival do Açaí",
        },
        headers=op_headers,
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["idempotent_replay"] is False
    shift = data["shift"]
    assert shift["status"] == "open"
    assert shift["opened_by"] == operator.id
    assert shift["opened_by_name"] == operator.name
    assert shift["opening_cash"] == "150.00"
    assert shift["notes"] == "Turno noturno Festival do Açaí"
    assert data["state"]["is_open"] is True
    assert data["state"]["summary"]["opening_cash"] == "150.00"
    assert data["state"]["summary"]["expected_cash"] == "150.00"

    # Reenvio idempotente com a mesma chave e mesmos dados
    replay = client.post(
        "/api/shifts/open",
        json={
            "opening_cash": "150.00",
            "idempotency_key": "open-shift-001",
            "notes": "Turno noturno Festival do Açaí",
        },
        headers=op_headers,
    )
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    assert replay.get_json()["shift"]["id"] == shift["id"]

    # Validação de valor negativo
    neg_resp = client.post(
        "/api/shifts/open",
        json={"opening_cash": "-10.00"},
        headers=op_headers,
    )
    assert neg_resp.status_code == 400


def test_02_block_second_simultaneous_open_shift(client, admin, operator):
    """2. Bloqueio de segunda abertura simultânea de turno no MVP."""
    op_headers = logged_in(client, "caixa")
    r1 = client.post("/api/shifts/open", json={"opening_cash": "100.00"}, headers=op_headers)
    assert r1.status_code == 201

    r2 = client.post("/api/shifts/open", json={"opening_cash": "50.00"}, headers=op_headers)
    assert r2.status_code == 409
    assert r2.get_json()["error"]["code"] == "shift_already_open"

    with pytest.raises(ShiftAlreadyOpenError):
        client.application.extensions["mx_container"].shifts.open_shift(
            actor=admin,
            opening_cash="200.00",
        )


def test_03_block_operations_without_open_shift(client, admin, operator):
    """3. Bloqueio de venda rápida, abertura de comanda, sangria e suprimento sem turno aberto."""
    container = client.application.extensions["mx_container"]
    product = container.products.create_product(admin, "Açaí 300ml", "16.00")

    op_headers = logged_in(client, "caixa")

    # Venda rápida bloqueada sem turno aberto
    sale_resp = client.post(
        "/api/sales/quick",
        json={
            "idempotency_key": "no-shift-sale-001",
            "items": [{"product_id": product.id, "quantity": 1}],
            "payments": [{"method": "cash", "amount": "16.00", "cash_tendered": "20.00"}],
        },
        headers=op_headers,
    )
    assert sale_resp.status_code == 409
    assert sale_resp.get_json()["error"]["code"] == "shift_closed"

    # Abertura de comanda bloqueada sem turno aberto
    tab_resp = client.post(
        "/api/tabs",
        json={"number": 10, "customer_name": "Cliente Sem Turno"},
        headers=op_headers,
    )
    assert tab_resp.status_code == 409
    assert tab_resp.get_json()["error"]["code"] == "shift_closed"

    # Sangria e suprimento bloqueados sem turno aberto
    w_resp = client.post(
        "/api/shifts/withdrawals",
        json={"amount": "20.00", "reason": "Troco externo"},
        headers=op_headers,
    )
    assert w_resp.status_code == 409
    assert w_resp.get_json()["error"]["code"] == "shift_closed"

    s_resp = client.post(
        "/api/shifts/supplies",
        json={"amount": "20.00", "reason": "Reforço de moedas"},
        headers=op_headers,
    )
    assert s_resp.status_code == 409
    assert s_resp.get_json()["error"]["code"] == "shift_closed"


def test_04_opening_cash_not_counted_as_sale_or_revenue(client, operator):
    """4. Fundo inicial não é contabilizado como venda de produtos nem receita de cliente."""
    op_headers = logged_in(client, "caixa")
    client.post("/api/shifts/open", json={"opening_cash": "150.00"}, headers=op_headers)

    state = client.get("/api/shifts/current", headers=op_headers).get_json()
    summary = state["summary"]
    assert summary["opening_cash"] == "150.00"
    assert summary["expected_cash"] == "150.00"
    assert summary["sales_count"] == 0
    assert summary["quick_sales_subtotal"] == "0.00"
    assert summary["tabs_active_consumed"] == "0.00"
    assert summary["total_products_sold"] == "0.00"
    assert summary["net_principal"] == "0.00"
    assert summary["net_gross"] == "0.00"
    assert summary["by_method"]["cash"]["principal_in"] == "0.00"
    assert summary["by_method"]["cash"]["gross_in"] == "0.00"


def test_05_change_given_accounted_correctly_without_double_counting(client, admin, operator):
    """5. Troco em dinheiro contabilizado corretamente: saldo esperado soma apenas o valor líquido da venda."""
    container = client.application.extensions["mx_container"]
    product = container.products.create_product(admin, "Açaí 500ml", "22.00")
    container.shifts.open_shift(actor=operator, opening_cash="100.00")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-change-check-005",
        items=[QuickSaleItemInput(product_id=product.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="22.00", cash_tendered="50.00")],
    )
    assert sale.payments[0].amount == Decimal("22.00")
    assert sale.payments[0].cash_tendered == Decimal("50.00")
    assert sale.payments[0].change_given == Decimal("28.00")

    state = container.shifts.get_current_shift_state()
    assert state["summary"]["opening_cash"] == "100.00"
    assert state["summary"]["by_method"]["cash"]["gross_in"] == "22.00"
    assert state["summary"]["by_method"]["cash"]["change_given_total"] == "28.00"
    # 100.00 fundo + 22.00 líquido (50 recebidos - 28 troco) = 122.00
    assert state["summary"]["expected_cash"] == "122.00"
    assert state["conference"]["cash"]["expected_amount"] == "122.00"


def test_06_advance_credit_deposit_not_counted_as_sale(client, operator):
    """6. Depósito antecipado de crédito entra no caixa mas não é contado como venda de produtos."""
    container = client.application.extensions["mx_container"]
    container.shifts.open_shift(actor=operator, opening_cash="100.00")
    tab = container.tabs.open_tab(actor=operator, number=20, customer_name="Cliente Crédito")

    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="dep-not-sale-006",
        method="cash",
        amount="60.00",
        cash_tendered="60.00",
    )

    state = container.shifts.get_current_shift_state()
    summary = state["summary"]
    assert summary["total_products_sold"] == "0.00"
    assert summary["credit_deposits_total"] == "60.00"
    assert summary["tabs_credit_balance_total"] == "60.00"
    assert summary["by_method"]["cash"]["gross_in"] == "60.00"
    assert summary["expected_cash"] == "160.00"


def test_07_credit_applied_does_not_create_duplicate_inflow(client, admin, operator):
    """7. Consumo abatido por crédito aplicado registra venda de produtos sem duplicar entrada financeira."""
    container = client.application.extensions["mx_container"]
    product = container.products.create_product(admin, "Barca Açaí", "45.00")
    container.shifts.open_shift(actor=operator, opening_cash="100.00")
    tab = container.tabs.open_tab(actor=operator, number=21, customer_name="Cliente Aplicação")

    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="dep-apply-007",
        method="pix",
        amount="50.00",
    )
    updated_tab, _, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="item-apply-007",
        items=[QuickSaleItemInput(product_id=product.id, quantity=1)],
    )
    assert updated_tab.credit_applied == Decimal("45.00")
    assert updated_tab.credit_balance == Decimal("5.00")
    assert updated_tab.balance_due == Decimal("0.00")

    state = container.shifts.get_current_shift_state()
    summary = state["summary"]
    assert summary["total_products_sold"] == "45.00"
    assert summary["tabs_credit_applied_total"] == "45.00"
    # Entrada financeira total permanece R$ 50,00 (Pix), sem nova entrada de R$ 45,00
    assert summary["by_method"]["pix"]["gross_in"] == "50.00"
    assert summary["expected_pix"] == "50.00"
    assert summary["expected_cash"] == "100.00"


def test_08_completed_refund_reduces_expected_balance(client, admin, operator):
    """8. Devolução efetiva ao cliente reduz imediatamente o saldo esperado do método."""
    container = client.application.extensions["mx_container"]
    product = container.products.create_product(admin, "Açaí 400ml", "18.00")
    container.shifts.open_shift(actor=operator, opening_cash="100.00")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-refund-008",
        items=[QuickSaleItemInput(product_id=product.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="18.00")],
    )
    assert container.shifts.get_current_shift_state()["summary"]["expected_cash"] == "118.00"

    container.sales.void_quick_sale(
        actor=admin,
        sale_id=sale.id,
        reason="Pedido trocado com devolução imediata",
        customer_refund_completed=True,
        customer_refund_method="cash",
    )

    state = container.shifts.get_current_shift_state()
    assert state["summary"]["by_method"]["cash"]["gross_in"] == "18.00"
    assert state["summary"]["by_method"]["cash"]["gross_out"] == "18.00"
    assert state["summary"]["expected_cash"] == "100.00"
    assert state["pendencies"]["pending_refunds_count"] == 0


def test_09_pending_refund_does_not_create_fictitious_outflow(client, admin, operator):
    """9. Cancelamento contábil com devolução pendente não gera saída física fictícia antes da devolução."""
    container = client.application.extensions["mx_container"]
    product = container.products.create_product(admin, "Açaí 400ml", "18.00")
    container.shifts.open_shift(actor=operator, opening_cash="100.00")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-pending-refund-009",
        items=[QuickSaleItemInput(product_id=product.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="18.00")],
    )
    container.sales.void_quick_sale(
        actor=admin,
        sale_id=sale.id,
        reason="Cancelado aguardando cliente voltar ao caixa",
        customer_refund_completed=False,
    )

    state = container.shifts.get_current_shift_state()
    # Venda foi cancelada contabilmente, mas os R$ 18,00 continuam fisicamente na gaveta até a devolução
    assert state["summary"]["total_products_sold"] == "0.00"
    assert state["summary"]["by_method"]["cash"]["gross_in"] == "18.00"
    assert state["summary"]["by_method"]["cash"]["gross_out"] == "0.00"
    assert state["summary"]["expected_cash"] == "118.00"
    assert state["pendencies"]["pending_refunds_count"] == 1
    assert state["pendencies"]["pending_refunds_total"] == "18.00"


def test_10_credit_refund_by_different_method_than_deposit(client, operator):
    """10. Devolução de crédito por método diferente do depósito impacta os saldos esperados de cada método."""
    container = client.application.extensions["mx_container"]
    container.shifts.open_shift(actor=operator, opening_cash="150.00")
    tab = container.tabs.open_tab(actor=operator, number=30, customer_name="Depósito Pix / Troco Dinheiro")

    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="dep-pix-010",
        method="pix",
        amount="50.00",
    )
    container.tabs.refund_credit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="ref-cash-010",
        method="cash",
        amount="50.00",
        note="Devolvido em dinheiro no caixa",
    )
    container.tabs.close_tab(actor=operator, tab_id=tab.id)

    state = container.shifts.get_current_shift_state()
    # Pix recebeu 50.00 e teve 0.00 de saída em Pix -> esperado Pix = 50.00
    assert state["conference"]["pix"]["inflows"] == "50.00"
    assert state["conference"]["pix"]["outflows"] == "0.00"
    assert state["conference"]["pix"]["expected_amount"] == "50.00"
    # Dinheiro tinha 150.00 de fundo e saiu 50.00 de devolução -> esperado Dinheiro = 100.00
    assert state["conference"]["cash"]["opening_cash"] == "150.00"
    assert state["conference"]["cash"]["outflows"] == "50.00"
    assert state["conference"]["cash"]["expected_amount"] == "100.00"


def test_11_cash_withdrawal_sangria(client, operator):
    """11. Sangria exige valor positivo e motivo, reduz saldo esperado em dinheiro e não altera vendas."""
    op_headers = logged_in(client, "caixa")
    client.post("/api/shifts/open", json={"opening_cash": "200.00"}, headers=op_headers)

    # Sangria inválida (valor zero ou motivo curto)
    bad_val = client.post(
        "/api/shifts/withdrawals",
        json={"amount": "0.00", "reason": "Retirada"},
        headers=op_headers,
    )
    assert bad_val.status_code == 400

    bad_reason = client.post(
        "/api/shifts/withdrawals",
        json={"amount": "50.00", "reason": "ab"},
        headers=op_headers,
    )
    assert bad_reason.status_code == 400

    resp = client.post(
        "/api/shifts/withdrawals",
        json={
            "amount": "80.00",
            "reason": "Retirada para cofre da organização",
            "idempotency_key": "sangria-key-011",
        },
        headers=op_headers,
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["movement"]["kind"] == "cash_withdrawal"
    assert body["movement"]["direction"] == "out"
    assert body["movement"]["amount"] == "80.00"
    assert body["state"]["summary"]["cash_withdrawals_total"] == "80.00"
    assert body["state"]["summary"]["expected_cash"] == "120.00"
    # Sangria não é devolução de cliente nem reduz receita
    assert body["state"]["summary"]["by_method"]["cash"]["gross_out"] == "0.00"
    assert body["state"]["summary"]["total_products_sold"] == "0.00"


def test_12_cash_supply_suprimento(client, operator):
    """12. Suprimento exige valor positivo e motivo, aumenta saldo esperado em dinheiro e não conta como venda."""
    op_headers = logged_in(client, "caixa")
    client.post("/api/shifts/open", json={"opening_cash": "100.00"}, headers=op_headers)

    resp = client.post(
        "/api/shifts/supplies",
        json={
            "amount": "45.00",
            "reason": "Adição de notas de R$ 5 e R$ 2 para troco",
            "idempotency_key": "suprimento-key-012",
        },
        headers=op_headers,
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["movement"]["kind"] == "cash_supply"
    assert body["movement"]["direction"] == "in"
    assert body["movement"]["amount"] == "45.00"
    assert body["state"]["summary"]["cash_supplies_total"] == "45.00"
    assert body["state"]["summary"]["expected_cash"] == "145.00"
    # Suprimento não é venda nem depósito de cliente
    assert body["state"]["summary"]["by_method"]["cash"]["gross_in"] == "0.00"
    assert body["state"]["summary"]["total_products_sold"] == "0.00"


def test_13_expected_cash_formula_exact_specification_example(client, admin, operator):
    """13. Cálculo exato do saldo esperado em dinheiro conforme exemplo da especificação (§5):
    - Fundo inicial: R$ 150,00
    - Vendas recebidas em dinheiro: R$ 80,00
    - Depósitos de crédito em dinheiro: R$ 50,00
    - Devoluções em dinheiro: R$ 18,00
    - Sangria: R$ 100,00
    => Dinheiro esperado: R$ 162,00.
    """
    container = client.application.extensions["mx_container"]
    p_80 = container.products.create_product(admin, "Combo Família Açaí", "80.00")
    p_32 = container.products.create_product(admin, "Açaí Especial 700ml", "32.00")

    # 1. Fundo inicial: R$ 150,00
    container.shifts.open_shift(actor=operator, opening_cash="150.00")

    # 2. Venda recebida em dinheiro: R$ 80,00 (cliente entregou R$ 100,00 e recebeu R$ 20,00 de troco)
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="spec-sale-cash-80",
        items=[QuickSaleItemInput(product_id=p_80.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="80.00", cash_tendered="100.00")],
    )

    # 3. Depósito de crédito em dinheiro: R$ 50,00 (consumiu R$ 32,00 e devolveu os R$ 18,00 restantes em dinheiro)
    tab = container.tabs.open_tab(actor=operator, number=50, customer_name="Cliente Exemplo §5")
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="spec-dep-cash-50",
        method="cash",
        amount="50.00",
        cash_tendered="50.00",
    )
    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="spec-cons-32",
        items=[QuickSaleItemInput(product_id=p_32.id, quantity=1)],
    )
    # 4. Devolução em dinheiro: R$ 18,00
    container.tabs.refund_credit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="spec-refund-cash-18",
        method="cash",
        amount="18.00",
    )
    container.tabs.close_tab(actor=operator, tab_id=tab.id)

    # 5. Sangria: R$ 100,00
    container.shifts.record_cash_withdrawal(
        actor=operator,
        amount="100.00",
        reason="Sangria de segurança para o cofre",
        idempotency_key="spec-sangria-100",
    )

    state = container.shifts.get_current_shift_state()
    cash_conf = state["conference"]["cash"]
    assert cash_conf["opening_cash"] == "150.00"
    assert cash_conf["inflows"] == "130.00"      # 80.00 venda + 50.00 depósito
    assert cash_conf["outflows"] == "18.00"      # 18.00 devolução
    assert cash_conf["supplies"] == "0.00"
    assert cash_conf["withdrawals"] == "100.00"  # 100.00 sangria
    assert cash_conf["expected_amount"] == "162.00"
    assert state["summary"]["expected_cash"] == "162.00"


def test_14_exact_conference_requires_no_justification(client, admin, operator):
    """14. Conferência exata: diferença 0.00 em todos os métodos, sem exigir justificativa."""
    container = client.application.extensions["mx_container"]
    prod = container.products.create_product(admin, "Açaí 300ml", "15.00")
    container.shifts.open_shift(actor=operator, opening_cash="100.00")
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-exact-014",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=2)],
        payments=[
            QuickSalePaymentInput(method="cash", amount="15.00"),
            QuickSalePaymentInput(method="pix", amount="15.00"),
        ],
    )

    preview = container.shifts.preview_closing(
        counted_cash="115.00",
        counted_pix="15.00",
        counted_card="0.00",
    )
    assert preview["conference"]["cash"]["difference"] == "0.00"
    assert preview["conference"]["cash"]["status"] == "exact"
    assert preview["conference"]["pix"]["difference"] == "0.00"
    assert preview["conference"]["pix"]["status"] == "exact"
    assert preview["conference"]["has_discrepancy"] is False
    assert preview["ready_to_close"] is True

    closed_shift, report, _ = container.shifts.close_shift(
        actor=operator,
        counted_cash="115.00",
        counted_pix="15.00",
        counted_card="0.00",
    )
    assert closed_shift.status == ShiftStatus.CLOSED
    assert closed_shift.cash_difference == Decimal("0.00")
    assert closed_shift.pix_difference == Decimal("0.00")
    assert closed_shift.card_difference == Decimal("0.00")
    assert report["conference"]["has_discrepancy"] is False


def test_15_conference_with_surplus_sobra(client, operator):
    """15. Conferência com sobra (diferença positiva) preserva esperado, conferido e diferença sem criar movimento fictício."""
    container = client.application.extensions["mx_container"]
    container.shifts.open_shift(actor=operator, opening_cash="100.00")

    preview = container.shifts.preview_closing(
        counted_cash="105.50",
        discrepancy_justification="Moedas de troco deixadas por clientes na gaveta",
    )
    assert preview["conference"]["cash"]["expected_amount"] == "100.00"
    assert preview["conference"]["cash"]["counted_amount"] == "105.50"
    assert preview["conference"]["cash"]["difference"] == "5.50"
    assert preview["conference"]["cash"]["status"] == "surplus"
    assert preview["conference"]["has_discrepancy"] is True

    closed_shift, report, _ = container.shifts.close_shift(
        actor=operator,
        counted_cash="105.50",
        discrepancy_justification="Moedas de troco deixadas por clientes na gaveta",
    )
    assert closed_shift.expected_cash == Decimal("100.00")
    assert closed_shift.counted_cash == Decimal("105.50")
    assert closed_shift.cash_difference == Decimal("5.50")
    assert report["conference"]["cash"]["status"] == "surplus"
    # Nenhum movimento financeiro fictício criado para forçar o saldo a fechar
    assert report["summary"]["expected_cash"] == "100.00"


def test_16_conference_with_shortage_falta(client, operator):
    """16. Conferência com falta (diferença negativa) registra falta e justificativa."""
    container = client.application.extensions["mx_container"]
    container.shifts.open_shift(actor=operator, opening_cash="150.00")

    closed_shift, report, _ = container.shifts.close_shift(
        actor=operator,
        counted_cash="142.00",
        discrepancy_justification="Diferença de R$ 8,00 no troco em horário de pico",
    )
    assert closed_shift.expected_cash == Decimal("150.00")
    assert closed_shift.counted_cash == Decimal("142.00")
    assert closed_shift.cash_difference == Decimal("-8.00")
    assert report["conference"]["cash"]["difference"] == "-8.00"
    assert report["conference"]["cash"]["status"] == "shortage"
    assert report["conference"]["discrepancy_justification"] == "Diferença de R$ 8,00 no troco em horário de pico"


def test_17_mandatory_justification_when_discrepancy_exists(client, operator):
    """17. Justificativa obrigatória quando houver divergência em qualquer método (Dinheiro, Pix ou Cartão)."""
    op_headers = logged_in(client, "caixa")
    client.post("/api/shifts/open", json={"opening_cash": "100.00"}, headers=op_headers)

    # Tentativa de fechar com diferença em dinheiro sem justificativa
    resp_no_just = client.post(
        "/api/shifts/close",
        json={"counted_cash": "95.00"},
        headers=op_headers,
    )
    assert resp_no_just.status_code == 400
    assert "justificativa" in resp_no_just.get_json()["error"]["message"].lower()

    # Tentativa com diferença em Pix sem justificativa
    resp_pix_diff = client.post(
        "/api/shifts/close",
        json={"counted_cash": "100.00", "counted_pix": "10.00"},
        headers=op_headers,
    )
    assert resp_pix_diff.status_code == 400

    # Com justificativa válida, permite fechar
    resp_ok = client.post(
        "/api/shifts/close",
        json={
            "counted_cash": "95.00",
            "discrepancy_justification": "Falta de R$ 5,00 verificada na contagem física",
        },
        headers=op_headers,
    )
    assert resp_ok.status_code == 200
    assert resp_ok.get_json()["shift"]["status"] == "closed"


def test_18_block_closing_with_open_tab(client, admin, operator):
    """18. Bloqueio de fechamento enquanto houver comanda aberta (com ou sem consumo pendente)."""
    container = client.application.extensions["mx_container"]
    prod = container.products.create_product(admin, "Açaí 300ml", "15.00")
    container.shifts.open_shift(actor=operator, opening_cash="100.00")

    tab = container.tabs.open_tab(actor=operator, number=101, customer_name="Mesa 101")
    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab-101-item",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=2)],
    )

    state = container.shifts.get_current_shift_state()
    assert state["pendencies"]["open_tabs_count"] == 1
    assert state["pendencies"]["unpaid_consumption_total"] == "30.00"
    assert state["pendencies"]["can_close"] is False

    with pytest.raises(ShiftCloseBlockedError) as exc_info:
        container.shifts.close_shift(actor=operator, counted_cash="100.00")
    assert exc_info.value.details["pendencies"]["open_tabs_count"] == 1
    assert exc_info.value.details["pendencies"]["open_tabs"][0]["number"] == 101


def test_19_block_closing_with_unrefunded_advance_credit(client, operator):
    """19. Bloqueio de fechamento enquanto houver saldo de crédito antecipado não utilizado/não devolvido."""
    container = client.application.extensions["mx_container"]
    container.shifts.open_shift(actor=operator, opening_cash="100.00")

    tab = container.tabs.open_tab(actor=operator, number=102, customer_name="Cliente Crédito Sobrando")
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab-102-credit",
        method="cash",
        amount="40.00",
    )

    state = container.shifts.get_current_shift_state()
    assert state["pendencies"]["unrefunded_credit_total"] == "40.00"
    assert any(b["code"] == "unrefunded_credit" for b in state["pendencies"]["blockers"])

    with pytest.raises(ShiftCloseBlockedError) as exc_info:
        container.shifts.close_shift(actor=operator, counted_cash="140.00")
    codes = [b["code"] for b in exc_info.value.details["pendencies"]["blockers"]]
    assert "unrefunded_credit" in codes

    # Após devolver o crédito e fechar a comanda, o fechamento do turno é liberado
    container.tabs.refund_credit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab-102-refund",
        method="cash",
        amount="40.00",
    )
    container.tabs.close_tab(actor=operator, tab_id=tab.id)

    closed_shift, _, _ = container.shifts.close_shift(actor=operator, counted_cash="100.00")
    assert closed_shift.status == ShiftStatus.CLOSED


def test_20_block_closing_with_pending_customer_refund(client, admin, operator):
    """20. Bloqueio de fechamento enquanto houver devolução pendente ao cliente (venda rápida ou comanda)."""
    container = client.application.extensions["mx_container"]
    prod = container.products.create_product(admin, "Açaí 500ml", "20.00")
    container.shifts.open_shift(actor=operator, opening_cash="100.00")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-block-refund-020",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="pix", amount="20.00")],
    )
    container.sales.void_quick_sale(
        actor=admin,
        sale_id=sale.id,
        reason="Cancelada aguardando devolução do Pix",
        customer_refund_completed=False,
    )

    with pytest.raises(ShiftCloseBlockedError) as exc_info:
        container.shifts.close_shift(actor=operator, counted_cash="100.00", counted_pix="20.00")
    codes = [b["code"] for b in exc_info.value.details["pendencies"]["blockers"]]
    assert "pending_refunds" in codes
    assert exc_info.value.details["pendencies"]["pending_refunds_count"] == 1

    # Confirma a devolução pendente e então encerra o turno com sucesso
    container.sales.confirm_customer_refund(
        actor=admin,
        sale_id=sale.id,
        method="pix",
        notes="Pix devolvido ao cliente",
    )
    closed_shift, _, _ = container.shifts.close_shift(
        actor=operator,
        counted_cash="100.00",
        counted_pix="0.00",
    )
    assert closed_shift.status == ShiftStatus.CLOSED


def test_21_successful_shift_closing_and_card_fee_reconciliation(client, admin, operator):
    """21. Fechamento bem-sucedido com conciliação de Dinheiro, Pix e Cartão (principal + taxa repassada)."""
    container = client.application.extensions["mx_container"]
    container.settings.update_settings(admin, "3.50", "simple_addition")
    prod = container.products.create_product(admin, "Açaí 500ml", "20.00")
    container.shifts.open_shift(actor=operator, opening_cash="150.00")

    # Venda em cartão de R$ 20,00 com taxa de 3.50% repassada ao cliente -> R$ 20,70 cobrados na maquininha
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-card-021",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="card", amount="20.00")],
    )

    op_headers = logged_in(client, "caixa")
    close_resp = client.post(
        "/api/shifts/close",
        json={
            "counted_cash": "150.00",
            "counted_pix": "0.00",
            "counted_card": "20.70",
            "notes": "Turno encerrado sem intercorrências",
            "idempotency_key": "close-shift-021",
        },
        headers=op_headers,
    )
    assert close_resp.status_code == 200
    body = close_resp.get_json()
    assert body["shift"]["status"] == "closed"
    assert body["shift"]["expected_cash"] == "150.00"
    assert body["shift"]["counted_cash"] == "150.00"
    assert body["shift"]["expected_card"] == "20.70"
    assert body["shift"]["counted_card"] == "20.70"
    assert body["shift"]["card_difference"] == "0.00"
    card_conf = body["report"]["conference"]["card"]
    assert card_conf["principal_in"] == "20.00"
    assert card_conf["fee_in"] == "0.70"
    assert card_conf["gross_in"] == "20.70"
    assert "adquirente" in card_conf["settlement_note"].lower()


def test_22_prevent_closing_twice_and_prevent_reopening(client, operator):
    """22. Impede fechar duas vezes o mesmo turno (exceto replay idempotente exato) e impede reabrir turno encerrado."""
    op_headers = logged_in(client, "caixa")
    open_resp = client.post("/api/shifts/open", json={"opening_cash": "100.00"}, headers=op_headers)
    shift_id = open_resp.get_json()["shift"]["id"]

    first_close = client.post(
        f"/api/shifts/{shift_id}/close",
        json={"counted_cash": "100.00", "idempotency_key": "close-once-022"},
        headers=op_headers,
    )
    assert first_close.status_code == 200
    assert first_close.get_json()["idempotent_replay"] is False

    # Replay idempotente com a mesma chave retorna o fechamento já realizado
    replay_close = client.post(
        f"/api/shifts/{shift_id}/close",
        json={"counted_cash": "100.00", "idempotency_key": "close-once-022"},
        headers=op_headers,
    )
    assert replay_close.status_code == 200
    assert replay_close.get_json()["idempotent_replay"] is True

    # Nova tentativa de fechar o mesmo turno falha com 409 shift_closed
    second_close = client.post(
        f"/api/shifts/{shift_id}/close",
        json={"counted_cash": "100.00", "idempotency_key": "close-twice-022"},
        headers=op_headers,
    )
    assert second_close.status_code == 409
    assert second_close.get_json()["error"]["code"] == "shift_closed"

    # Tentativa de reabrir turno encerrado falha com 409 shift_closed
    reopen_resp = client.post(f"/api/shifts/{shift_id}/reopen", headers=op_headers)
    assert reopen_resp.status_code == 409
    assert reopen_resp.get_json()["error"]["code"] == "shift_closed"


def test_23_block_new_operations_and_voids_after_shift_closed(client, admin, operator):
    """23. Bloqueia novas vendas, sangrias, suprimentos e estornos de vendas pertencentes a turno já encerrado."""
    container = client.application.extensions["mx_container"]
    prod = container.products.create_product(admin, "Açaí 300ml", "15.00")
    shift, _ = container.shifts.open_shift(actor=operator, opening_cash="100.00")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-before-close-023",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="15.00")],
    )

    container.shifts.close_shift(actor=operator, counted_cash="115.00")

    # Não permite nova venda com o turno fechado
    with pytest.raises(ShiftClosedError):
        container.sales.confirm_quick_sale(
            actor=operator,
            idempotency_key="sale-after-close-023",
            items=[QuickSaleItemInput(product_id=prod.id, quantity=1)],
            payments=[QuickSalePaymentInput(method="cash", amount="15.00")],
        )

    # Não permite sangria ou suprimento com o turno fechado
    with pytest.raises(ShiftClosedError):
        container.shifts.record_cash_withdrawal(actor=operator, amount="10.00", reason="Sangria pós fechamento")
    with pytest.raises(ShiftClosedError):
        container.shifts.record_cash_supply(actor=operator, amount="10.00", reason="Suprimento pós fechamento")

    # Mesmo abrindo um segundo turno, não permite estornar venda do turno #1 já encerrado
    container.shifts.open_shift(actor=operator, opening_cash="80.00")
    with pytest.raises(ShiftClosedError):
        container.sales.void_quick_sale(
            actor=admin,
            sale_id=sale.id,
            reason="Tentativa de estornar venda de turno anterior fechado",
            customer_refund_completed=True,
        )


def test_24_historical_report_preservation(client, admin, operator):
    """24. Preservação do relatório histórico imutável de fechamentos anteriores."""
    container = client.application.extensions["mx_container"]
    prod = container.products.create_product(admin, "Açaí 500ml", "20.00")

    # Turno 1
    shift1, _ = container.shifts.open_shift(actor=operator, opening_cash="150.00", notes="Turno 1")
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="hist-t1-sale",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=2)],
        payments=[QuickSalePaymentInput(method="cash", amount="40.00")],
    )
    container.shifts.record_cash_withdrawal(actor=operator, amount="30.00", reason="Sangria Turno 1")
    container.shifts.close_shift(
        actor=operator,
        counted_cash="158.00",
        discrepancy_justification="Falta de R$ 2,00 no troco de moedas",
        notes="Fechamento Turno 1",
    )

    # Turno 2 com novos movimentos não deve alterar o relatório histórico do Turno 1
    shift2, _ = container.shifts.open_shift(actor=operator, opening_cash="200.00", notes="Turno 2")
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="hist-t2-sale",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=3)],
        payments=[QuickSalePaymentInput(method="pix", amount="60.00")],
    )

    op_headers = logged_in(client, "caixa")
    r1 = client.get(f"/api/shifts/{shift1.id}/report", headers=op_headers)
    assert r1.status_code == 200
    rep1 = r1.get_json()["report"]
    assert rep1["shift"]["id"] == shift1.id
    assert rep1["shift"]["status"] == "closed"
    assert rep1["shift"]["opening_cash"] == "150.00"
    assert rep1["shift"]["expected_cash"] == "160.00"
    assert rep1["shift"]["counted_cash"] == "158.00"
    assert rep1["shift"]["cash_difference"] == "-2.00"
    assert rep1["shift"]["discrepancy_justification"] == "Falta de R$ 2,00 no troco de moedas"
    assert rep1["summary"]["total_products_sold"] == "40.00"
    assert len(rep1["drawer_movements"]) == 1
    assert rep1["drawer_movements"][0]["amount"] == "30.00"

    # Lista de turnos encerrados retorna Turno 1
    closed_list = client.get("/api/shifts?status=closed", headers=op_headers).get_json()["shifts"]
    assert len(closed_list) == 1
    assert closed_list[0]["id"] == shift1.id


def test_25_concurrency_and_atomic_rollback(client, admin, operator):
    """25. Concorrência na abertura/fechamento de turno e rollback atômico em caso de falha."""
    container = client.application.extensions["mx_container"]

    # 25a. Abertura concorrente por múltiplas threads: apenas 1 turno pode ser criado
    def _try_open(idx: int):
        try:
            s, _ = container.shifts.open_shift(
                actor=operator,
                opening_cash=f"{100 + idx}.00",
            )
            return ("ok", s.id)
        except ShiftAlreadyOpenError:
            return ("blocked", None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_try_open, range(4)))

    successes = [r for r in results if r[0] == "ok"]
    blocked = [r for r in results if r[0] == "blocked"]
    assert len(successes) == 1
    assert len(blocked) == 3

    # 25b. Rollback atômico se ocorrer erro durante o fechamento: turno permanece 'open'
    with patch("src.adapters.db.repositories.MariaDbAuditLogRepository.add", side_effect=RuntimeError("Falha simulada")):
        with pytest.raises(RuntimeError, match="Falha simulada"):
            container.shifts.close_shift(actor=operator, counted_cash= container.shifts.get_current_shift_state()["summary"]["expected_cash"])

    state_after_fail = container.shifts.get_current_shift_state()
    assert state_after_fail["is_open"] is True
    assert state_after_fail["current_shift"]["status"] == "open"
    assert state_after_fail["current_shift"]["closed_at"] is None


def test_26_full_shift_lifecycle_regression(client, admin, operator):
    """26. Ciclo completo do turno integrando abertura, venda rápida, comanda, depósito antecipado,
    aplicação de crédito, devolução de excedente, sangria, suprimento, conferência e fechamento."""
    container = client.application.extensions["mx_container"]
    container.settings.update_settings(admin, "3.50", "simple_addition")
    acai_300 = container.products.create_product(admin, "Açaí 300ml", "15.00")
    acai_500 = container.products.create_product(admin, "Açaí 500ml", "22.00")

    # 1. Abre turno com R$ 200,00
    shift, _ = container.shifts.open_shift(actor=operator, opening_cash="200.00", notes="Turno Completo Festival")

    # 2. Venda rápida em dinheiro (2x R$ 15 = R$ 30, cliente entregou R$ 50, troco R$ 20)
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="reg-quick-cash",
        items=[QuickSaleItemInput(product_id=acai_300.id, quantity=2)],
        payments=[QuickSalePaymentInput(method="cash", amount="30.00", cash_tendered="50.00")],
    )

    # 3. Venda rápida em cartão (1x R$ 22 + 3.5% taxa R$ 0.77 = R$ 22.77)
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="reg-quick-card",
        items=[QuickSaleItemInput(product_id=acai_500.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="card", amount="22.00")],
    )

    # 4. Comanda #1: depósito antecipado de R$ 50 em Pix, consumo de R$ 37 (1x 15 + 1x 22), devolução de R$ 13 em dinheiro
    tab = container.tabs.open_tab(actor=operator, number=1, customer_name="Cliente Comanda #1")
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="reg-tab-dep-pix",
        method="pix",
        amount="50.00",
    )
    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="reg-tab-items",
        items=[
            QuickSaleItemInput(product_id=acai_300.id, quantity=1),
            QuickSaleItemInput(product_id=acai_500.id, quantity=1),
        ],
    )
    container.tabs.refund_credit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="reg-tab-refund-cash",
        method="cash",
        amount="13.00",
    )
    container.tabs.close_tab(actor=operator, tab_id=tab.id)

    # 5. Suprimento de R$ 25,00 e Sangria de R$ 60,00
    container.shifts.record_cash_supply(actor=operator, amount="25.00", reason="Suprimento de moedas")
    container.shifts.record_cash_withdrawal(actor=operator, amount="60.00", reason="Sangria parcial")

    # Saldo esperado em dinheiro:
    # 200.00 (fundo) + 30.00 (venda dinheiro) - 13.00 (devolução crédito dinheiro) + 25.00 (suprimento) - 60.00 (sangria) = 182.00
    # Saldo esperado em Pix: 50.00
    # Saldo esperado em Cartão (bruto): 22.77
    # Total produtos vendidos: 30.00 (quick cash) + 22.00 (quick card) + 37.00 (tab) = 89.00
    closed_shift, report, _ = container.shifts.close_shift(
        actor=operator,
        counted_cash="182.00",
        counted_pix="50.00",
        counted_card="22.77",
        idempotency_key="reg-close-shift",
    )

    assert closed_shift.status == ShiftStatus.CLOSED
    assert closed_shift.expected_cash == Decimal("182.00")
    assert closed_shift.counted_cash == Decimal("182.00")
    assert closed_shift.cash_difference == Decimal("0.00")
    assert closed_shift.expected_pix == Decimal("50.00")
    assert closed_shift.expected_card == Decimal("22.77")
    assert report["summary"]["total_products_sold"] == "89.00"
    assert report["summary"]["credit_deposits_total"] == "50.00"
    assert report["summary"]["credit_refunds_total"] == "13.00"
    assert report["summary"]["tabs_credit_applied_total"] == "37.00"
    assert report["summary"]["cash_supplies_total"] == "25.00"
    assert report["summary"]["cash_withdrawals_total"] == "60.00"
