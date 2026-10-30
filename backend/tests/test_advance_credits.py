"""Testes automatizados do Bloco 4 (25 cenários obrigatórios §13):
crédito antecipado vinculado à comanda, aplicação automática ao consumo,
consumo excedente com pagamento complementar, devolução parcial/integral por
qualquer método, estorno administrativo de depósito, cancelamento de consumo
pago com crédito e reconciliação financeira completa."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from conftest import logged_in
from src.domain.errors import (
    IdempotencyConflictError,
    InsufficientCreditError,
    InvalidPaymentError,
    PermissionDeniedError,
    ShiftClosedError,
    TabBalancePendingError,
    TabClosedError,
    TabCreditBalancePendingError,
    TabIncompleteOperationsError,
)
from src.domain.models import (
    CashDirection,
    CashMovementKind,
    CustomerRefundStatus,
    PaymentMethod,
    SaleStatus,
    TabStatus,
)
from src.use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput


def test_01_open_tab_and_pix_credit_deposit(client, operator):
    """1. Abertura de comanda e depósito por Pix (passivo de crédito, não venda)."""
    op_headers = logged_in(client, "caixa")

    open_resp = client.post(
        "/api/tabs",
        json={"number": 15, "customer_name": "Cliente Pix"},
        headers=op_headers,
    )
    assert open_resp.status_code == 201
    tab_id = open_resp.get_json()["tab"]["id"]

    dep_resp = client.post(
        f"/api/tabs/{tab_id}/credits",
        json={
            "idempotency_key": "b4-pix-dep-001",
            "method": "pix",
            "amount": "50.00",
            "note": "Depósito antecipado Pix",
        },
        headers=op_headers,
    )
    assert dep_resp.status_code == 201
    body = dep_resp.get_json()
    assert body["idempotent_replay"] is False
    tab = body["tab"]
    assert tab["credit_deposited"] == "50.00"
    assert tab["credit_applied"] == "0.00"
    assert tab["credit_refunded"] == "0.00"
    assert tab["credit_balance"] == "50.00"
    assert tab["subtotal_consumed"] == "0.00"
    assert tab["balance_due"] == "0.00"

    mov = body["credit_deposits"][0]
    assert mov["kind"] == "credit_deposit"
    assert mov["direction"] == "in"
    assert mov["method"] == "pix"
    assert mov["amount"] == "50.00"
    assert mov["fee_amount"] == "0.00"
    assert mov["gross_amount"] == "50.00"

    summary = client.get("/api/sales/summary").get_json()["summary"]
    assert summary["active_sales_subtotal"] == "0.00"
    assert summary["tabs_credit_deposited_total"] == "50.00"
    assert summary["tabs_credit_balance_total"] == "50.00"


def test_02_cash_credit_deposit_with_change(client, operator):
    """2. Depósito em dinheiro com troco."""
    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 16}, headers=op_headers).get_json()["tab"]["id"]

    dep_resp = client.post(
        f"/api/tabs/{tab_id}/credits",
        json={
            "idempotency_key": "b4-cash-dep-002",
            "method": "cash",
            "amount": "35.00",
            "cash_tendered": "50.00",
        },
        headers=op_headers,
    )
    assert dep_resp.status_code == 201
    body = dep_resp.get_json()
    assert body["tab"]["credit_deposited"] == "35.00"
    assert body["tab"]["credit_balance"] == "35.00"

    mov = body["credit_deposits"][0]
    assert mov["method"] == "cash"
    assert mov["amount"] == "35.00"
    assert mov["gross_amount"] == "35.00"
    assert mov["cash_tendered"] == "50.00"
    assert mov["change_given"] == "15.00"


def test_03_card_credit_deposit_with_repassed_fee(client, admin, operator):
    """3. Depósito em cartão com taxa repassada ao cliente."""
    admin_headers = logged_in(client, "admin")
    client.put(
        "/api/settings/payment",
        json={"card_fee_percent": "3.00", "card_fee_mode": "simple_addition"},
        headers=admin_headers,
    )

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 17}, headers=op_headers).get_json()["tab"]["id"]

    dep_resp = client.post(
        f"/api/tabs/{tab_id}/credits",
        json={
            "idempotency_key": "b4-card-dep-003",
            "method": "card",
            "amount": "50.00",
        },
        headers=op_headers,
    )
    assert dep_resp.status_code == 201
    mov = dep_resp.get_json()["credit_deposits"][0]
    assert mov["kind"] == "credit_deposit"
    assert mov["method"] == "card"
    assert mov["amount"] == "50.00"
    assert mov["fee_percent"] == "3.00"
    assert mov["fee_mode"] == "simple_addition"
    assert mov["fee_amount"] == "1.50"
    assert mov["gross_amount"] == "51.50"


def test_04_card_fee_does_not_increase_available_credit_and_preserves_history(client, admin, operator):
    """4. Garantia de que a taxa do cartão não aumenta o crédito disponível e preserva valores históricos."""
    admin_headers = logged_in(client, "admin")
    client.put(
        "/api/settings/payment",
        json={"card_fee_percent": "3.00", "card_fee_mode": "simple_addition"},
        headers=admin_headers,
    )

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 170}, headers=op_headers).get_json()["tab"]["id"]
    dep_resp = client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-card-dep-004", "method": "card", "amount": "50.00"},
        headers=op_headers,
    )
    tab = dep_resp.get_json()["tab"]
    assert tab["credit_deposited"] == "50.00"
    assert tab["credit_balance"] == "50.00"
    assert tab["credit_card_fee_paid"] == "1.50"
    assert tab["total_charged_paid"] == "51.50"

    # Alterar configuração de taxa futura não recalcula depósito histórico
    admin_headers = logged_in(client, "admin")
    client.put(
        "/api/settings/payment",
        json={"card_fee_percent": "5.00", "card_fee_mode": "pass_through"},
        headers=admin_headers,
    )
    fetched = client.get(f"/api/tabs/{tab_id}").get_json()["tab"]
    assert fetched["credit_deposited"] == "50.00"
    assert fetched["credit_balance"] == "50.00"
    assert fetched["credit_card_fee_paid"] == "1.50"
    assert fetched["credit_deposits"][0]["fee_percent"] == "3.00"


def test_05_consumption_less_than_credit_reduces_credit_balance(container, admin, operator):
    """5. Consumo inferior ao crédito com redução correta do saldo de crédito."""
    p = container.products.create_product(admin, "Açaí 300ml", "15.00")
    tab = container.tabs.open_tab(operator, number=18)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c05-dep-50",
        payments=[QuickSalePaymentInput(method="pix", amount="50.00")],
    )

    tab, launch, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c05-cons-15",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )
    assert launch.subtotal == Decimal("15.00")
    assert tab.subtotal_consumed == Decimal("15.00")
    assert tab.credit_applied == Decimal("15.00")
    assert tab.credit_balance == Decimal("35.00")
    assert tab.balance_due == Decimal("0.00")


def test_06_multiple_consumptions_using_accumulated_credit(container, admin, operator):
    """6. Múltiplos consumos usando crédito acumulado (Exemplo: Depósito 50, consumos 15 e 20 -> restante 15)."""
    p15 = container.products.create_product(admin, "Açaí 300ml", "15.00")
    p20 = container.products.create_product(admin, "Açaí 500ml", "20.00")

    tab = container.tabs.open_tab(operator, number=181)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c06-dep-50",
        payments=[QuickSalePaymentInput(method="pix", amount="50.00")],
    )
    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c06-cons-15",
        items=[QuickSaleItemInput(product_id=p15.id, quantity=1)],
    )
    tab, _, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c06-cons-20",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )
    assert tab.subtotal_consumed == Decimal("35.00")
    assert tab.credit_applied == Decimal("35.00")
    assert tab.credit_balance == Decimal("15.00")
    assert tab.balance_due == Decimal("0.00")


def test_07_consuming_credit_does_not_create_new_cash_inflow(container, admin, operator):
    """7. Garantia de que consumir crédito não cria nova entrada financeira."""
    p20 = container.products.create_product(admin, "Açaí 500ml", "20.00")
    tab = container.tabs.open_tab(operator, number=182)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c07-dep-50",
        payments=[QuickSalePaymentInput(method="pix", amount="50.00")],
    )
    tab, _, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c07-cons-20",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )

    assert len(tab.payments) == 0
    assert len(tab.credit_deposits) == 1
    summary = container.sales.get_financial_summary()
    assert summary["by_method"]["pix"]["in_principal"] == "50.00"
    assert summary["by_method"]["pix"]["in_gross"] == "50.00"
    assert summary["principal_received_total"] == "0.00"
    assert summary["tabs_credit_deposited_total"] == "50.00"
    assert summary["tabs_credit_applied_total"] == "20.00"
    assert summary["tabs_credit_balance_total"] == "30.00"
    assert summary["active_sales_subtotal"] == "20.00"


def test_08_consumption_exceeding_credit_calculates_balance_due(container, admin, operator):
    """8. Consumo superior ao crédito com cálculo correto de saldo a pagar (Crédito 30, Consumo 42 -> a pagar 12)."""
    p42 = container.products.create_product(admin, "Combo Festival", "42.00")
    tab = container.tabs.open_tab(operator, number=19)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c08-dep-30",
        payments=[QuickSalePaymentInput(method="pix", amount="30.00")],
    )
    tab, _, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c08-cons-42",
        items=[QuickSaleItemInput(product_id=p42.id, quantity=1)],
    )
    assert tab.subtotal_consumed == Decimal("42.00")
    assert tab.credit_deposited == Decimal("30.00")
    assert tab.credit_applied == Decimal("30.00")
    assert tab.credit_balance == Decimal("0.00")
    assert tab.balance_due == Decimal("12.00")
    assert tab.can_close is False
    assert "balance_due_pending" in tab.close_blockers


def test_09_complementary_payment_of_excess_balance_single_methods(container, admin, operator):
    """9. Pagamento complementar do saldo excedente em Pix, cartão ou dinheiro."""
    container.settings.update_settings(admin, card_fee_percent="3.00", card_fee_mode="simple_addition")
    p42 = container.products.create_product(admin, "Combo Festival", "42.00")

    for idx, (method, tendered) in enumerate([("pix", None), ("card", None), ("cash", "20.00")], start=1):
        tab = container.tabs.open_tab(operator, number=190 + idx)
        container.tabs.record_credit_deposit(
            actor=operator,
            tab_id=tab.id,
            idempotency_key=f"b4-c09-dep-{idx}",
            payments=[QuickSalePaymentInput(method="pix", amount="30.00")],
        )
        container.tabs.add_items(
            actor=operator,
            tab_id=tab.id,
            idempotency_key=f"b4-c09-cons-{idx}",
            items=[QuickSaleItemInput(product_id=p42.id, quantity=1)],
        )
        tab, movs, _ = container.tabs.record_payment(
            actor=operator,
            tab_id=tab.id,
            idempotency_key=f"b4-c09-pay-{idx}",
            payments=[QuickSalePaymentInput(method=method, amount="12.00", cash_tendered=tendered)],
        )
        assert len(movs) == 1
        assert tab.credit_applied == Decimal("30.00")
        assert tab.principal_paid == Decimal("12.00")
        assert tab.balance_due == Decimal("0.00")
        assert tab.can_close is True


def test_10_complementary_split_payment_of_excess_balance(container, admin, operator):
    """10. Pagamento complementar dividido (Pix + Cartão com taxa + Dinheiro com troco)."""
    container.settings.update_settings(admin, card_fee_percent="3.00", card_fee_mode="simple_addition")
    p42 = container.products.create_product(admin, "Combo Festival", "42.00")

    tab = container.tabs.open_tab(operator, number=199)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c10-dep-30",
        payments=[QuickSalePaymentInput(method="pix", amount="30.00")],
    )
    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c10-cons-42",
        items=[QuickSaleItemInput(product_id=p42.id, quantity=1)],
    )
    tab, comp_movements, _ = container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c10-split-pay",
        payments=[
            QuickSalePaymentInput(method="pix", amount="5.00"),
            QuickSalePaymentInput(method="card", amount="4.00"),
            QuickSalePaymentInput(method="cash", amount="3.00", cash_tendered="5.00"),
        ],
    )
    assert len(comp_movements) == 3
    assert tab.credit_applied == Decimal("30.00")
    assert tab.principal_paid == Decimal("12.00")
    assert tab.settled_subtotal == Decimal("42.00")
    assert tab.balance_due == Decimal("0.00")
    assert tab.sale_card_fee_paid == Decimal("0.12")
    assert tab.can_close is True


def test_11_new_credit_deposit_after_partial_consumption(container, admin, operator):
    """11. Novo depósito após consumo parcial."""
    p = container.products.create_product(admin, "Açaí 400ml", "18.00")

    tab = container.tabs.open_tab(operator, number=20)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-dep-initial-20",
        payments=[QuickSalePaymentInput(method="pix", amount="20.00")],
    )
    tab, _, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-cons-first-18",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )
    assert tab.credit_deposited == Decimal("20.00")
    assert tab.credit_applied == Decimal("18.00")
    assert tab.credit_balance == Decimal("2.00")

    tab, _, _ = container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-dep-second-25",
        payments=[QuickSalePaymentInput(method="cash", amount="25.00", cash_tendered="25.00")],
    )
    assert tab.credit_deposited == Decimal("45.00")
    assert tab.credit_applied == Decimal("18.00")
    assert tab.credit_balance == Decimal("27.00")
    assert tab.balance_due == Decimal("0.00")


def test_12_partial_credit_refund(client, admin, operator):
    """12. Devolução parcial de crédito."""
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí Tradicional", "price": "32.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 120}, headers=op_headers).get_json()["tab"]["id"]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c12-dep-50", "method": "pix", "amount": "50.00"},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/items",
        json={"idempotency_key": "b4-c12-cons-32", "items": [{"product_id": p["id"], "quantity": 1}]},
        headers=op_headers,
    )

    ref1 = client.post(
        f"/api/tabs/{tab_id}/credit-refunds",
        json={
            "idempotency_key": "b4-c12-ref-8",
            "method": "pix",
            "amount": "8.00",
            "note": "Devolução parcial",
        },
        headers=op_headers,
    )
    assert ref1.status_code == 201
    tab = ref1.get_json()["tab"]
    assert tab["credit_deposited"] == "50.00"
    assert tab["credit_applied"] == "32.00"
    assert tab["credit_refunded"] == "8.00"
    assert tab["credit_balance"] == "10.00"
    assert tab["can_close"] is False


def test_13_full_credit_refund_of_remaining_balance(client, admin, operator):
    """13. Devolução integral do crédito restante."""
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí Tradicional", "price": "32.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 130}, headers=op_headers).get_json()["tab"]["id"]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c13-dep-50", "method": "pix", "amount": "50.00"},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/items",
        json={"idempotency_key": "b4-c13-cons-32", "items": [{"product_id": p["id"], "quantity": 1}]},
        headers=op_headers,
    )
    ref = client.post(
        f"/api/tabs/{tab_id}/credit-refunds",
        json={"idempotency_key": "b4-c13-ref-18", "method": "pix", "amount": "18.00"},
        headers=op_headers,
    )
    assert ref.status_code == 201
    tab = ref.get_json()["tab"]
    assert tab["credit_refunded"] == "18.00"
    assert tab["credit_balance"] == "0.00"
    assert tab["balance_due"] == "0.00"
    assert tab["can_close"] is True


def test_14_credit_refund_by_different_method_than_deposit(client, admin, operator):
    """14. Devolução por método diferente do método do depósito (Exemplo principal §2: Comanda 15)."""
    admin_headers = logged_in(client, "admin")
    p1 = client.post(
        "/api/products",
        json={"name": "Açaí Tradicional", "price": "20.00"},
        headers=admin_headers,
    ).get_json()["product"]
    p2 = client.post(
        "/api/products",
        json={"name": "Adicional Creme", "price": "12.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab_id = client.post(
        "/api/tabs",
        json={"number": 15, "customer_name": "Exemplo Principal"},
        headers=op_headers,
    ).get_json()["tab"]["id"]

    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "ex15-dep-50", "method": "pix", "amount": "50.00"},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/items",
        json={"idempotency_key": "ex15-cons-20", "items": [{"product_id": p1["id"], "quantity": 1}]},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/items",
        json={"idempotency_key": "ex15-cons-12", "items": [{"product_id": p2["id"], "quantity": 1}]},
        headers=op_headers,
    )
    ref = client.post(
        f"/api/tabs/{tab_id}/credit-refunds",
        json={"idempotency_key": "ex15-ref-18-cash", "method": "cash", "amount": "18.00"},
        headers=op_headers,
    )
    assert ref.status_code == 201
    close_resp = client.post(f"/api/tabs/{tab_id}/close", json={}, headers=op_headers)
    assert close_resp.status_code == 200

    summary = client.get("/api/sales/summary").get_json()["summary"]
    assert summary["active_sales_subtotal"] == "32.00"
    assert summary["by_method"]["pix"]["in_gross"] == "50.00"
    assert summary["by_method"]["pix"]["out_gross"] == "0.00"
    assert summary["by_method"]["pix"]["net_gross"] == "50.00"
    assert summary["by_method"]["cash"]["in_gross"] == "0.00"
    assert summary["by_method"]["cash"]["out_gross"] == "18.00"
    assert summary["by_method"]["cash"]["net_gross"] == "-18.00"
    assert summary["net_received_total"] == "32.00"
    assert summary["tabs_credit_balance_total"] == "0.00"
    assert summary["tabs_pending_balance_total"] == "0.00"


def test_15_block_credit_refund_exceeding_available_credit(client, operator):
    """15. Bloqueio de devolução superior ao crédito disponível."""
    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 150}, headers=op_headers).get_json()["tab"]["id"]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c15-dep-18", "method": "pix", "amount": "18.00"},
        headers=op_headers,
    )
    over_ref = client.post(
        f"/api/tabs/{tab_id}/credit-refunds",
        json={"idempotency_key": "b4-c15-ref-over", "method": "cash", "amount": "18.01"},
        headers=op_headers,
    )
    assert over_ref.status_code == 409
    assert over_ref.get_json()["error"]["code"] == "insufficient_credit"


def test_16_block_closing_tab_with_positive_credit_balance(client, operator):
    """16. Bloqueio de encerramento com crédito positivo."""
    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 160}, headers=op_headers).get_json()["tab"]["id"]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c16-dep-20", "method": "pix", "amount": "20.00"},
        headers=op_headers,
    )
    close_credit = client.post(f"/api/tabs/{tab_id}/close", json={}, headers=op_headers)
    assert close_credit.status_code == 409
    assert close_credit.get_json()["error"]["code"] == "tab_credit_balance_pending"


def test_17_block_closing_tab_with_positive_balance_due(client, admin, operator):
    """17. Bloqueio de encerramento com saldo devedor positivo."""
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 500ml", "price": "25.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 171}, headers=op_headers).get_json()["tab"]["id"]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c17-dep-20", "method": "pix", "amount": "20.00"},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/items",
        json={"idempotency_key": "b4-c17-cons-25", "items": [{"product_id": p["id"], "quantity": 1}]},
        headers=op_headers,
    )
    close_debt = client.post(f"/api/tabs/{tab_id}/close", json={}, headers=op_headers)
    assert close_debt.status_code == 409
    assert close_debt.get_json()["error"]["code"] == "tab_balance_pending"


def test_18_closing_allowed_only_when_both_balances_are_zero(client, admin, operator):
    """18. Encerramento permitido somente com saldos zerados."""
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 500ml", "price": "25.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 180}, headers=op_headers).get_json()["tab"]["id"]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c18-dep-20", "method": "pix", "amount": "20.00"},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/items",
        json={"idempotency_key": "b4-c18-cons-25", "items": [{"product_id": p["id"], "quantity": 1}]},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/payments",
        json={"idempotency_key": "b4-c18-pay-5", "payments": [{"method": "cash", "amount": "5.00"}]},
        headers=op_headers,
    )
    close_ok = client.post(f"/api/tabs/{tab_id}/close", json={"notes": "Quitada"}, headers=op_headers)
    assert close_ok.status_code == 200
    assert close_ok.get_json()["tab"]["status"] == "closed"


def test_19_block_deposit_consumption_and_refund_on_closed_tab(client, admin, operator):
    """19. Bloqueio de depósito, consumo e devolução em comanda encerrada."""
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 500ml", "price": "25.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 190}, headers=op_headers).get_json()["tab"]["id"]
    client.post(f"/api/tabs/{tab_id}/close", json={}, headers=op_headers)

    dep_closed = client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c19-closed-dep", "method": "pix", "amount": "10.00"},
        headers=op_headers,
    )
    assert dep_closed.status_code == 409
    assert dep_closed.get_json()["error"]["code"] == "tab_closed"

    cons_closed = client.post(
        f"/api/tabs/{tab_id}/items",
        json={"idempotency_key": "b4-c19-closed-cons", "items": [{"product_id": p["id"], "quantity": 1}]},
        headers=op_headers,
    )
    assert cons_closed.status_code == 409
    assert cons_closed.get_json()["error"]["code"] == "tab_closed"

    ref_closed = client.post(
        f"/api/tabs/{tab_id}/credit-refunds",
        json={"idempotency_key": "b4-c19-closed-ref", "method": "cash", "amount": "5.00"},
        headers=op_headers,
    )
    assert ref_closed.status_code == 409
    assert ref_closed.get_json()["error"]["code"] == "tab_closed"


def test_20_idempotency_of_credit_deposit(client, operator):
    """20. Idempotência de depósito."""
    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 200}, headers=op_headers).get_json()["tab"]["id"]

    dep_payload = {
        "idempotency_key": "b4-c20-idem-dep-key",
        "payments": [
            {"method": "pix", "amount": "20.00"},
            {"method": "cash", "amount": "10.00", "cash_tendered": "10.00"},
        ],
        "note": "Depósito dividido",
    }
    first_dep = client.post(f"/api/tabs/{tab_id}/credits", json=dep_payload, headers=op_headers)
    assert first_dep.status_code == 201
    assert first_dep.get_json()["idempotent_replay"] is False
    first_ids = [m["id"] for m in first_dep.get_json()["credit_deposits"]]

    replay_dep = client.post(f"/api/tabs/{tab_id}/credits", json=dep_payload, headers=op_headers)
    assert replay_dep.status_code == 200
    assert replay_dep.get_json()["idempotent_replay"] is True
    assert [m["id"] for m in replay_dep.get_json()["credit_deposits"]] == first_ids
    assert replay_dep.get_json()["tab"]["credit_deposited"] == "30.00"
    assert replay_dep.get_json()["tab"]["credit_balance"] == "30.00"


def test_21_idempotency_of_credit_refund(client, operator):
    """21. Idempotência de devolução."""
    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 210}, headers=op_headers).get_json()["tab"]["id"]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c21-dep-30", "method": "pix", "amount": "30.00"},
        headers=op_headers,
    )

    ref_payload = {
        "idempotency_key": "b4-c21-idem-ref-key",
        "method": "pix",
        "amount": "12.00",
        "note": "Devolução Pix",
    }
    first_ref = client.post(f"/api/tabs/{tab_id}/credit-refunds", json=ref_payload, headers=op_headers)
    assert first_ref.status_code == 201
    assert first_ref.get_json()["idempotent_replay"] is False
    ref_id = first_ref.get_json()["credit_refund"]["id"]

    replay_ref = client.post(f"/api/tabs/{tab_id}/credit-refunds", json=ref_payload, headers=op_headers)
    assert replay_ref.status_code == 200
    assert replay_ref.get_json()["idempotent_replay"] is True
    assert replay_ref.get_json()["credit_refund"]["id"] == ref_id
    assert replay_ref.get_json()["tab"]["credit_refunded"] == "12.00"
    assert replay_ref.get_json()["tab"]["credit_balance"] == "18.00"


def test_22_idempotency_conflict_with_different_payload(client, operator):
    """22. Conflito de idempotência com payload diferente (em depósito e em devolução)."""
    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 220}, headers=op_headers).get_json()["tab"]["id"]

    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c22-dep-key", "method": "pix", "amount": "30.00"},
        headers=op_headers,
    )
    conflict_dep = client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-c22-dep-key", "method": "pix", "amount": "25.00"},
        headers=op_headers,
    )
    assert conflict_dep.status_code == 409
    assert conflict_dep.get_json()["error"]["code"] == "idempotency_conflict"

    client.post(
        f"/api/tabs/{tab_id}/credit-refunds",
        json={"idempotency_key": "b4-c22-ref-key", "method": "pix", "amount": "10.00"},
        headers=op_headers,
    )
    conflict_ref = client.post(
        f"/api/tabs/{tab_id}/credit-refunds",
        json={"idempotency_key": "b4-c22-ref-key", "method": "cash", "amount": "10.00"},
        headers=op_headers,
    )
    assert conflict_ref.status_code == 409
    assert conflict_ref.get_json()["error"]["code"] == "idempotency_conflict"


def test_23_administrative_void_of_credit_deposit(client, admin, operator):
    """23. Cancelamento/estorno administrativo de depósito:
    - Separa cancelamento contábil (customer_refund_completed=False -> obrigação pendente, sem saída física de caixa)
      da devolução financeira efetiva ao cliente (confirm_tab_credit_refund / customer_refund_completed=True).
    - Preserva trilha de auditoria e nunca reduz o crédito depositado duas vezes."""
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 300ml", "price": "20.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab_id = client.post("/api/tabs", json={"number": 23}, headers=op_headers).get_json()["tab"]["id"]

    dep1 = client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-void-dep-1", "method": "pix", "amount": "30.00"},
        headers=op_headers,
    ).get_json()["credit_deposits"][0]
    client.post(
        f"/api/tabs/{tab_id}/credits",
        json={"idempotency_key": "b4-void-dep-2", "method": "cash", "amount": "20.00"},
        headers=op_headers,
    )
    client.post(
        f"/api/tabs/{tab_id}/items",
        json={
            "idempotency_key": "b4-void-cons-35",
            "items": [
                {"product_id": p["id"], "quantity": 1},
                {"custom_description": "Extra", "custom_amount": "15.00", "quantity": 1},
            ],
        },
        headers=op_headers,
    )

    forbidden = client.post(
        f"/api/tabs/{tab_id}/credits/{dep1['id']}/void",
        json={"reason": "Erro de digitação"},
        headers=op_headers,
    )
    assert forbidden.status_code == 403

    # 1. Cancelamento contábil sem devolução física imediata (customer_refund_completed=False por padrão)
    admin_headers = logged_in(client, "admin")
    void_resp = client.post(
        f"/api/tabs/{tab_id}/credits/{dep1['id']}/void",
        json={"reason": "Depósito lançado em duplicidade"},
        headers=admin_headers,
    )
    assert void_resp.status_code == 200
    body = void_resp.get_json()
    assert body["reversal"] is None

    tab = body["tab"]
    assert tab["credit_deposited"] == "20.00"
    assert tab["credit_applied"] == "20.00"
    assert tab["credit_balance"] == "0.00"
    assert tab["balance_due"] == "15.00"
    assert tab["pending_refund_total"] == "30.00"
    assert tab["completed_refund_total"] == "0.00"
    assert tab["can_close"] is False

    # O caixa físico (net_gross / out_gross) NÃO deve ser reduzido antes da devolução efetiva!
    summary_pending = client.get("/api/sales/summary", headers=admin_headers).get_json()["summary"]
    assert summary_pending["tabs_credit_deposited_total"] == "20.00"
    assert summary_pending["pending_customer_refunds_count"] == 1
    assert summary_pending["pending_customer_refunds_total"] == "30.00"
    assert summary_pending["completed_customer_refunds_total"] == "0.00"
    assert summary_pending["by_method"]["pix"]["in_gross"] == "30.00"
    assert summary_pending["by_method"]["pix"]["out_gross"] == "0.00"
    assert summary_pending["by_method"]["pix"]["net_gross"] == "30.00"
    assert summary_pending["by_method"]["cash"]["in_gross"] == "20.00"
    assert summary_pending["by_method"]["cash"]["out_gross"] == "0.00"
    assert summary_pending["by_method"]["cash"]["net_gross"] == "20.00"
    assert summary_pending["net_received_total"] == "50.00"

    dup_void = client.post(
        f"/api/tabs/{tab_id}/credits/{dep1['id']}/void",
        json={"reason": "Repetido"},
        headers=admin_headers,
    )
    assert dup_void.status_code == 409

    # 2. Confirmação posterior da devolução física ao cliente (em dinheiro)
    confirm_resp = client.post(
        f"/api/tabs/{tab_id}/credits/{dep1['id']}/customer-refund",
        json={"method": "cash", "notes": "Devolvido em espécie no caixa"},
        headers=admin_headers,
    )
    assert confirm_resp.status_code == 200
    confirm_body = confirm_resp.get_json()
    reversal = confirm_body["reversal"]
    assert reversal["kind"] == "reversal"
    assert reversal["direction"] == "out"
    assert reversal["method"] == "cash"
    assert reversal["reverses_id"] == dep1["id"]
    assert reversal["amount"] == "30.00"

    tab_after = confirm_body["tab"]
    # O crédito depositado permanece R$ 20,00 (sem dupla dedução!) e a pendência vai a zero
    assert tab_after["credit_deposited"] == "20.00"
    assert tab_after["credit_applied"] == "20.00"
    assert tab_after["credit_balance"] == "0.00"
    assert tab_after["balance_due"] == "15.00"
    assert tab_after["pending_refund_total"] == "0.00"
    assert tab_after["completed_refund_total"] == "30.00"

    summary_completed = client.get("/api/sales/summary", headers=admin_headers).get_json()["summary"]
    assert summary_completed["pending_customer_refunds_count"] == 0
    assert summary_completed["pending_customer_refunds_total"] == "0.00"
    assert summary_completed["completed_customer_refunds_total"] == "30.00"
    assert summary_completed["by_method"]["pix"]["net_gross"] == "30.00"
    assert summary_completed["by_method"]["cash"]["in_gross"] == "20.00"
    assert summary_completed["by_method"]["cash"]["out_gross"] == "30.00"
    assert summary_completed["by_method"]["cash"]["net_gross"] == "-10.00"
    assert summary_completed["net_received_total"] == "20.00"

    # 3. Bloqueio de estorno após devolução parcial de crédito
    op_headers = logged_in(client, "caixa")
    tab2_id = client.post("/api/tabs", json={"number": 24}, headers=op_headers).get_json()["tab"]["id"]
    dep_t2 = client.post(
        f"/api/tabs/{tab2_id}/credits",
        json={"idempotency_key": "b4-void-t2-dep", "method": "pix", "amount": "20.00"},
        headers=op_headers,
    ).get_json()["credit_deposits"][0]
    client.post(
        f"/api/tabs/{tab2_id}/credit-refunds",
        json={"idempotency_key": "b4-void-t2-ref", "method": "cash", "amount": "10.00"},
        headers=op_headers,
    )
    admin_headers = logged_in(client, "admin")
    blocked_void = client.post(
        f"/api/tabs/{tab2_id}/credits/{dep_t2['id']}/void",
        json={"reason": "Tentativa após devolução parcial"},
        headers=admin_headers,
    )
    assert blocked_void.status_code == 400
    assert blocked_void.get_json()["error"]["code"] == "invalid_payment"


def test_24_void_consumption_paid_with_credit_restores_credit_without_fictitious_movements(
    container, admin, operator
):
    """24. Cancelamento de consumo pago com crédito, restaurando o crédito sem entrada/saída fictícia.
    Testa também consumo parcialmente pago com crédito e parcialmente pago com pagamento complementar."""
    p1 = container.products.create_product(admin, "Açaí 300ml", "20.00")
    p2 = container.products.create_product(admin, "Açaí 500ml", "25.00")

    # Caso 24A: consumo 100% coberto por crédito
    tab = container.tabs.open_tab(operator, number=25)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c24-dep-50",
        payments=[QuickSalePaymentInput(method="pix", amount="50.00")],
    )
    tab, launch1, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c24-cons-20",
        items=[QuickSaleItemInput(product_id=p1.id, quantity=1)],
    )
    assert tab.credit_applied == Decimal("20.00")
    assert tab.credit_balance == Decimal("30.00")

    tab, voided1 = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab.id,
        sale_id=launch1.id,
        reason="Lançamento incorreto",
    )
    assert voided1.status == SaleStatus.VOIDED
    assert voided1.customer_refund_status == CustomerRefundStatus.NOT_APPLICABLE
    assert voided1.customer_refund_amount == Decimal("0.00")
    assert tab.subtotal_consumed == Decimal("0.00")
    assert tab.credit_applied == Decimal("0.00")
    assert tab.credit_balance == Decimal("50.00")
    assert len(tab.payments) == 0
    assert len(tab.credit_deposits) == 1
    assert len(tab.credit_refunds) == 0

    # Caso 24B: consumo (R$ 20,00 + R$ 25,00 = R$ 45,00) parcialmente pago com crédito (R$ 30,00)
    # e parcialmente pago com pagamento ordinário (R$ 15,00 em dinheiro).
    tab2 = container.tabs.open_tab(operator, number=26)
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab2.id,
        idempotency_key="b4-c24b-dep-30",
        payments=[QuickSalePaymentInput(method="pix", amount="30.00")],
    )
    _, l_a, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab2.id,
        idempotency_key="b4-c24b-cons-20",
        items=[QuickSaleItemInput(product_id=p1.id, quantity=1)],
    )
    _, l_b, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab2.id,
        idempotency_key="b4-c24b-cons-25",
        items=[QuickSaleItemInput(product_id=p2.id, quantity=1)],
    )
    tab2, _, _ = container.tabs.record_payment(
        actor=operator,
        tab_id=tab2.id,
        idempotency_key="b4-c24b-pay-15",
        payments=[QuickSalePaymentInput(method="cash", amount="15.00", cash_tendered="15.00")],
    )
    assert tab2.subtotal_consumed == Decimal("45.00")
    assert tab2.credit_applied == Decimal("30.00")
    assert tab2.principal_paid == Decimal("15.00")
    assert tab2.balance_due == Decimal("0.00")

    # Cancela o lançamento de R$ 25,00 -> consumo cai para R$ 20,00.
    # Dos R$ 25,00 cancelados: R$ 10,00 apenas recompõem o crédito antecipado e R$ 15,00 geram devolução financeira.
    tab2, voided_b = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab2.id,
        sale_id=l_b.id,
        reason="Cancelou item de 25",
        customer_refund_completed=True,
        customer_refund_method="cash",
    )
    assert voided_b.customer_refund_status == CustomerRefundStatus.COMPLETED
    assert voided_b.customer_refund_amount == Decimal("15.00")
    assert tab2.subtotal_consumed == Decimal("20.00")
    assert tab2.principal_paid == Decimal("0.00")
    assert tab2.credit_applied == Decimal("20.00")
    assert tab2.credit_balance == Decimal("10.00")
    assert tab2.balance_due == Decimal("0.00")

    # Cancela também o item de R$ 20,00 -> consumo vai a R$ 0,00 e o crédito volta a R$ 30,00!
    tab2, voided_a = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab2.id,
        sale_id=l_a.id,
        reason="Cancelou item de 20",
    )
    assert voided_a.customer_refund_status == CustomerRefundStatus.NOT_APPLICABLE
    assert voided_a.customer_refund_amount == Decimal("0.00")
    assert tab2.subtotal_consumed == Decimal("0.00")
    assert tab2.principal_paid == Decimal("0.00")
    assert tab2.credit_applied == Decimal("0.00")
    assert tab2.credit_balance == Decimal("30.00")


def test_25_complete_financial_reconciliation_and_concurrent_credit_refund_protection(
    container, admin, operator
):
    """25. Reconciliação completa:
    - vendas de produtos;
    - entradas por método;
    - saídas por método;
    - líquido por método;
    - saldo de crédito pendente;
    - saldo devedor pendente.
    Inclui também proteção contra corrida concorrente em devoluções de crédito."""
    container.settings.update_settings(admin, card_fee_percent="4.00", card_fee_mode="simple_addition")
    p_acai = container.products.create_product(admin, "Açaí 500ml", "25.00")
    p_agua = container.products.create_product(admin, "Água Mineral", "6.00")

    # 1. Venda rápida: R$ 25,00 no Pix
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="b4-rec-qs-1",
        items=[QuickSaleItemInput(product_id=p_acai.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="pix", amount="25.00")],
    )

    # 2. Comanda #30: Depósito cartão R$ 50,00 (+ R$ 2,00 taxa = R$ 52,00 bruto),
    # consome R$ 31,00 (25 + 6), devolve R$ 10,00 em dinheiro, mantém R$ 9,00 de crédito pendente.
    tab30 = container.tabs.open_tab(operator, number=30, customer_name="Comanda Crédito")
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab30.id,
        idempotency_key="b4-rec-t30-dep",
        payments=[QuickSalePaymentInput(method="card", amount="50.00")],
    )
    container.tabs.add_items(
        actor=operator,
        tab_id=tab30.id,
        idempotency_key="b4-rec-t30-cons",
        items=[
            QuickSaleItemInput(product_id=p_acai.id, quantity=1),
            QuickSaleItemInput(product_id=p_agua.id, quantity=1),
        ],
    )

    def attempt_refund(idx: int):
        return container.tabs.refund_credit(
            actor=operator,
            tab_id=tab30.id,
            idempotency_key=f"b4-rec-t30-ref-race-{idx}",
            method="cash",
            amount="10.00",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt_refund, 1), pool.submit(attempt_refund, 2)]
        outcomes = []
        for f in futures:
            try:
                outcomes.append(("ok", f.result()))
            except Exception as exc:
                outcomes.append(("err", exc))

    oks = [o for o in outcomes if o[0] == "ok"]
    errs = [o for o in outcomes if o[0] == "err"]
    assert len(oks) == 1
    assert len(errs) == 1
    assert isinstance(errs[0][1], InsufficientCreditError)

    # 3. Comanda #31: Depósito Pix R$ 15,00, consumo R$ 25,00, pagamento parcial dinheiro R$ 4,00 ->
    # crédito aplicado R$ 15,00, principal pago R$ 4,00, saldo devedor R$ 6,00.
    tab31 = container.tabs.open_tab(operator, number=31, customer_name="Comanda Devedora")
    container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab31.id,
        idempotency_key="b4-rec-t31-dep",
        payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
    )
    container.tabs.add_items(
        actor=operator,
        tab_id=tab31.id,
        idempotency_key="b4-rec-t31-cons",
        items=[QuickSaleItemInput(product_id=p_acai.id, quantity=1)],
    )
    container.tabs.record_payment(
        actor=operator,
        tab_id=tab31.id,
        idempotency_key="b4-rec-t31-pay",
        payments=[QuickSalePaymentInput(method="cash", amount="4.00", cash_tendered="5.00")],
    )

    summary = container.sales.get_financial_summary()

    assert summary["quick_sales_subtotal"] == "25.00"
    assert summary["tabs_consumed_subtotal"] == "56.00"
    assert summary["active_sales_subtotal"] == "81.00"

    assert summary["principal_received_total"] == "29.00"
    assert summary["tabs_credit_applied_total"] == "46.00"
    assert summary["tabs_pending_balance_total"] == "6.00"
    assert (
        Decimal(summary["active_sales_subtotal"])
        == Decimal(summary["principal_received_total"])
        + Decimal(summary["tabs_credit_applied_total"])
        + Decimal(summary["tabs_pending_balance_total"])
    )

    assert summary["tabs_credit_deposited_total"] == "65.00"
    assert summary["tabs_credit_refunded_total"] == "10.00"
    assert summary["tabs_credit_balance_total"] == "9.00"
    assert (
        Decimal(summary["tabs_credit_deposited_total"])
        == Decimal(summary["tabs_credit_applied_total"])
        + Decimal(summary["tabs_credit_refunded_total"])
        + Decimal(summary["tabs_credit_balance_total"])
    )

    assert summary["by_method"]["pix"]["in_principal"] == "40.00"
    assert summary["by_method"]["pix"]["out_principal"] == "0.00"
    assert summary["by_method"]["pix"]["net_gross"] == "40.00"

    assert summary["by_method"]["card"]["in_principal"] == "50.00"
    assert summary["by_method"]["card"]["in_fee"] == "2.00"
    assert summary["by_method"]["card"]["in_gross"] == "52.00"
    assert summary["by_method"]["card"]["out_gross"] == "0.00"
    assert summary["by_method"]["card"]["net_gross"] == "52.00"

    assert summary["by_method"]["cash"]["in_principal"] == "4.00"
    assert summary["by_method"]["cash"]["out_principal"] == "10.00"
    assert summary["by_method"]["cash"]["net_gross"] == "-6.00"
    assert summary["by_method"]["cash"]["change_given_total"] == "1.00"

    assert summary["net_received_total"] == "86.00"


def test_26_complex_lifecycle_deposit_excess_consumption_complementary_payment_partial_void_and_new_deposit(
    container, admin, operator
):
    """26. Ciclo completo multi-etapa (Verificação Direcionada 1 e 3):
    - Depósito antecipado (Pix R$ 30,00);
    - Consumo superior ao crédito (R$ 25,00 + R$ 30,00 = R$ 55,00);
    - Pagamento complementar (Cartão R$ 25,00 principal + 4% taxa = R$ 26,00 bruto);
    - Cancelamento parcial de consumo (cancela lançamento de R$ 30,00 -> devolve R$ 25,00 do pagamento
      complementar em dinheiro e restaura R$ 5,00 de crédito sem movimento fictício);
    - Novo depósito de crédito (Dinheiro R$ 15,00 -> crédito disponível sobe para R$ 20,00);
    - Devolução parcial/integral de crédito por método diferente (Pix R$ 20,00);
    - Verifica que crédito, pagamentos e devoluções jamais são contabilizados duas vezes."""
    container.settings.update_settings(admin, card_fee_percent="4.00", card_fee_mode="simple_addition")
    p25 = container.products.create_product(admin, "Açaí 500ml", "25.00")
    p30 = container.products.create_product(admin, "Barca Açaí", "30.00")

    # 1. Abre comanda #40 e deposita R$ 30,00 via Pix
    tab = container.tabs.open_tab(operator, number=40, customer_name="Ciclo Completo")
    tab, _, _ = container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c26-dep1-pix-30",
        payments=[QuickSalePaymentInput(method="pix", amount="30.00")],
    )
    assert tab.credit_deposited == Decimal("30.00")
    assert tab.credit_balance == Decimal("30.00")
    assert tab.balance_due == Decimal("0.00")

    # 2. Consome R$ 25,00 + R$ 30,00 = R$ 55,00 (excede o crédito em R$ 25,00)
    tab, launch_25, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c26-cons1-25",
        items=[QuickSaleItemInput(product_id=p25.id, quantity=1)],
    )
    tab, launch_30, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c26-cons2-30",
        items=[QuickSaleItemInput(product_id=p30.id, quantity=1)],
    )
    assert tab.subtotal_consumed == Decimal("55.00")
    assert tab.credit_applied == Decimal("30.00")
    assert tab.credit_balance == Decimal("0.00")
    assert tab.balance_due == Decimal("25.00")

    # 3. Pagamento complementar de R$ 25,00 no cartão (+ R$ 1,00 taxa = R$ 26,00 bruto)
    tab, _, _ = container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c26-pay-card-25",
        payments=[QuickSalePaymentInput(method="card", amount="25.00")],
    )
    assert tab.principal_paid == Decimal("25.00")
    assert tab.sale_card_fee_paid == Decimal("1.00")
    assert tab.settled_subtotal == Decimal("55.00")
    assert tab.balance_due == Decimal("0.00")

    # 4. Cancelamento parcial de consumo: cancela launch_30 (R$ 30,00), devolvendo o excesso de
    # pagamento complementar (R$ 25,00) em dinheiro e recompondo R$ 5,00 de crédito antecipado
    tab, voided_30 = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab.id,
        sale_id=launch_30.id,
        reason="Cliente desistiu da barca antes do preparo",
        customer_refund_completed=True,
        customer_refund_method="cash",
    )
    assert voided_30.status == SaleStatus.VOIDED
    assert voided_30.customer_refund_status == CustomerRefundStatus.COMPLETED
    assert voided_30.customer_refund_amount == Decimal("25.00")

    assert tab.subtotal_consumed == Decimal("25.00")
    assert tab.voided_subtotal == Decimal("30.00")
    assert tab.principal_paid == Decimal("0.00")
    assert tab.net_principal_paid == Decimal("0.00")
    assert tab.credit_deposited == Decimal("30.00")
    assert tab.credit_applied == Decimal("25.00")
    assert tab.credit_refunded == Decimal("0.00")
    assert tab.credit_balance == Decimal("5.00")
    assert tab.balance_due == Decimal("0.00")
    assert tab.completed_refund_total == Decimal("25.00")

    # 5. Novo depósito antecipado de R$ 15,00 em dinheiro
    tab, _, _ = container.tabs.record_credit_deposit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c26-dep2-cash-15",
        payments=[QuickSalePaymentInput(method="cash", amount="15.00", cash_tendered="20.00")],
    )
    assert tab.credit_deposited == Decimal("45.00")
    assert tab.credit_applied == Decimal("25.00")
    assert tab.credit_refunded == Decimal("0.00")
    assert tab.credit_balance == Decimal("20.00")
    assert tab.balance_due == Decimal("0.00")
    assert tab.can_close is False

    # Verifica resumo financeiro intermediário (com R$ 20,00 de crédito disponível na comanda)
    s_mid = container.sales.get_financial_summary()
    assert s_mid["active_sales_subtotal"] == "25.00"
    assert s_mid["principal_received_total"] == "0.00"
    assert s_mid["tabs_credit_deposited_total"] == "45.00"
    assert s_mid["tabs_credit_applied_total"] == "25.00"
    assert s_mid["tabs_credit_refunded_total"] == "0.00"
    assert s_mid["tabs_credit_balance_total"] == "20.00"
    assert s_mid["tabs_pending_balance_total"] == "0.00"
    assert s_mid["completed_customer_refunds_total"] == "25.00"

    # 6. Devolve o saldo restante de crédito (R$ 20,00) via Pix e encerra a comanda
    tab, _, _ = container.tabs.refund_credit(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="b4-c26-ref-pix-20",
        method="pix",
        amount="20.00",
    )
    assert tab.credit_deposited == Decimal("45.00")
    assert tab.credit_applied == Decimal("25.00")
    assert tab.credit_refunded == Decimal("20.00")
    assert tab.credit_balance == Decimal("0.00")
    assert tab.balance_due == Decimal("0.00")
    assert tab.can_close is True

    tab = container.tabs.close_tab(actor=operator, tab_id=tab.id)
    assert tab.status == TabStatus.CLOSED

    # 7. Reconciliação final: nenhuma dupla contagem entre crédito, pagamentos e devoluções
    s_final = container.sales.get_financial_summary()
    assert s_final["active_sales_subtotal"] == "25.00"
    assert s_final["principal_received_total"] == "0.00"
    assert s_final["tabs_credit_deposited_total"] == "45.00"
    assert s_final["tabs_credit_applied_total"] == "25.00"
    assert s_final["tabs_credit_refunded_total"] == "20.00"
    assert s_final["tabs_credit_balance_total"] == "0.00"
    assert s_final["tabs_pending_balance_total"] == "0.00"
    assert s_final["pending_customer_refunds_total"] == "0.00"
    assert s_final["completed_customer_refunds_total"] == "25.00"

    # Pix: +30.00 (depósito 1) - 20.00 (devolução de crédito) = +10.00 líquido
    assert s_final["by_method"]["pix"]["in_principal"] == "30.00"
    assert s_final["by_method"]["pix"]["out_principal"] == "20.00"
    assert s_final["by_method"]["pix"]["net_gross"] == "10.00"

    # Cartão: +25.00 principal + 1.00 taxa = +26.00 bruto; o estorno do pagamento complementar
    # reverte R$ 25,00 de principal sem reverter a taxa cobrada (net_fee = 1.00, net_gross = 1.00)
    assert s_final["by_method"]["card"]["in_principal"] == "25.00"
    assert s_final["by_method"]["card"]["in_fee"] == "1.00"
    assert s_final["by_method"]["card"]["in_gross"] == "26.00"
    assert s_final["by_method"]["card"]["out_principal"] == "25.00"
    assert s_final["by_method"]["card"]["out_fee"] == "0.00"
    assert s_final["by_method"]["card"]["out_gross"] == "25.00"
    assert s_final["by_method"]["card"]["net_gross"] == "1.00"

    # Dinheiro: +15.00 (depósito 2, com R$ 20,00 entregue e R$ 5,00 de troco)
    assert s_final["by_method"]["cash"]["in_principal"] == "15.00"
    assert s_final["by_method"]["cash"]["out_principal"] == "0.00"
    assert s_final["by_method"]["cash"]["net_gross"] == "15.00"
    assert s_final["by_method"]["cash"]["change_given_total"] == "5.00"

    # Líquido total em caixa: 10.00 (Pix) + 1.00 (Cartão) + 15.00 (Dinheiro) = 26.00
    # (= R$ 25,00 consumo ativo + R$ 1,00 taxa de cartão retida/repassada)
    assert s_final["net_received_total"] == "26.00"

