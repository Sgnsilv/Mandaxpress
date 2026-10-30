"""Testes automatizados do Bloco 3: comandas abertas, lançamentos incrementais,
pagamentos parciais/divididos, taxa de cartão, troco, idempotência, concorrência,
rollback, cancelamento auditável e integração financeira."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import pytest

from conftest import logged_in
from src.domain.errors import (
    IdempotencyConflictError,
    InvalidInputError,
    InvalidPaymentError,
    PermissionDeniedError,
    SaleAlreadyVoidedError,
    ShiftClosedError,
    TabAlreadyOpenError,
    TabBalancePendingError,
    TabClosedError,
    TabIncompleteOperationsError,
)
from src.domain.models import (
    CashDirection,
    CashMovementKind,
    CustomerRefundStatus,
    PaymentMethod,
    SaleItemKind,
    SaleKind,
    SaleStatus,
    TabStatus,
)
from src.use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput


@pytest.fixture(autouse=True)
def _open_default_shift(container, operator):
    container.shifts.open_shift(actor=operator, opening_cash="0.00")


def test_open_tab_with_and_without_customer_name(client, operator):
    op_headers = logged_in(client, "caixa")

    # 1. Abertura com número e nome opcional do cliente
    resp1 = client.post(
        "/api/tabs",
        json={"number": 12, "customer_name": "Maria Oliveira", "notes": "Mesa próxima ao palco"},
        headers=op_headers,
    )
    assert resp1.status_code == 201
    tab1 = resp1.get_json()["tab"]
    assert tab1["number"] == 12
    assert tab1["customer_name"] == "Maria Oliveira"
    assert tab1["notes"] == "Mesa próxima ao palco"
    assert tab1["status"] == "open"
    assert tab1["opened_by"] == operator.id
    assert tab1["opened_at"] is not None
    assert tab1["closed_by"] is None
    assert tab1["closed_at"] is None
    assert tab1["subtotal_consumed"] == "0.00"
    assert tab1["principal_paid"] == "0.00"
    assert tab1["balance_due"] == "0.00"
    assert tab1["can_close"] is True

    # 2. Abertura apenas com número (sem nome de cliente)
    resp2 = client.post(
        "/api/tabs",
        json={"number": "25"},
        headers=op_headers,
    )
    assert resp2.status_code == 201
    tab2 = resp2.get_json()["tab"]
    assert tab2["number"] == 25
    assert tab2["customer_name"] is None
    assert tab2["status"] == "open"
    assert tab2["id"] != tab1["id"]

    # 3. Consulta e busca de comandas abertas
    list_resp = client.get("/api/tabs?status=open").get_json()
    assert len(list_resp["tabs"]) == 2
    assert [t["number"] for t in list_resp["tabs"]] == [12, 25]

    search_resp = client.get("/api/tabs?search=12").get_json()
    assert len(search_resp["tabs"]) == 1
    assert search_resp["tabs"][0]["id"] == tab1["id"]


def test_duplicate_open_tab_number_is_rejected(client, operator):
    op_headers = logged_in(client, "caixa")

    first = client.post("/api/tabs", json={"number": 10}, headers=op_headers)
    assert first.status_code == 201

    duplicate = client.post("/api/tabs", json={"number": 10, "customer_name": "Outro"}, headers=op_headers)
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"]["code"] == "tab_already_open"


def test_reuse_tab_number_after_closing_preserves_history_and_distinct_ids(container, admin, operator, clock):
    p = container.products.create_product(admin, "Açaí 300ml", "18.00")

    # Primeira comanda #15
    tab1 = container.tabs.open_tab(operator, number=15, customer_name="Cliente Manhã")
    container.tabs.add_items(
        actor=operator,
        tab_id=tab1.id,
        idempotency_key="tab15-first-launch-001",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )
    container.tabs.record_payment(
        actor=operator,
        tab_id=tab1.id,
        idempotency_key="tab15-first-pay-001",
        payments=[QuickSalePaymentInput(method="pix", amount="18.00")],
    )
    clock.advance(minutes=20)
    closed_tab1 = container.tabs.close_tab(operator, tab_id=tab1.id, notes="Primeira comanda 15 encerrada")
    assert closed_tab1.status == TabStatus.CLOSED
    assert closed_tab1.closed_by == operator.id
    assert closed_tab1.closed_at is not None

    # Reutiliza o número 15 para um novo cliente
    clock.advance(minutes=10)
    tab2 = container.tabs.open_tab(operator, number=15, customer_name="Cliente Tarde")
    assert tab2.id != closed_tab1.id
    assert tab2.number == 15
    assert tab2.status == TabStatus.OPEN
    assert tab2.subtotal_consumed == Decimal("0.00")
    assert tab2.balance_due == Decimal("0.00")

    # Consulta por número retorna a comanda aberta atual e preserva todo o histórico da anterior
    open_now, history = container.tabs.get_tabs_by_number(15, include_closed=True)
    assert open_now is not None
    assert open_now.id == tab2.id
    assert len(history) == 2
    by_id = {t.id: t for t in history}
    assert by_id[closed_tab1.id].customer_name == "Cliente Manhã"
    assert by_id[closed_tab1.id].subtotal_consumed == Decimal("18.00")
    assert by_id[closed_tab1.id].principal_paid == Decimal("18.00")
    assert by_id[closed_tab1.id].status == TabStatus.CLOSED
    assert by_id[tab2.id].customer_name == "Cliente Tarde"
    assert by_id[tab2.id].status == TabStatus.OPEN


def test_multiple_incremental_launches_accumulate_consumption(container, admin, operator, clock):
    """Exemplo exato da especificação (§4):
    Comanda 12:
    - Primeiro lançamento: R$ 20,00.
    - Segundo lançamento: R$ 15,00.
    - Terceiro lançamento: R$ 10,00.
    Total consumido: R$ 45,00.
    """
    p20 = container.products.create_product(admin, "Açaí 500ml", "20.00")
    p15 = container.products.create_product(admin, "Açaí 300ml", "15.00")
    p10 = container.products.create_product(admin, "Água de Coco", "10.00")

    tab = container.tabs.open_tab(operator, number=12, customer_name="João")

    tab, l1, created1 = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab12-launch-1",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )
    assert created1 is True
    assert l1.subtotal == Decimal("20.00")
    assert tab.subtotal_consumed == Decimal("20.00")
    assert tab.balance_due == Decimal("20.00")

    clock.advance(minutes=15)
    tab, l2, created2 = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab12-launch-2",
        items=[QuickSaleItemInput(product_id=p15.id, quantity=1)],
    )
    assert created2 is True
    assert l2.subtotal == Decimal("15.00")
    assert tab.subtotal_consumed == Decimal("35.00")
    assert tab.balance_due == Decimal("35.00")

    clock.advance(minutes=15)
    tab, l3, created3 = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab12-launch-3",
        items=[QuickSaleItemInput(product_id=p10.id, quantity=1)],
    )
    assert created3 is True
    assert l3.subtotal == Decimal("10.00")
    assert tab.subtotal_consumed == Decimal("45.00")
    assert tab.balance_due == Decimal("45.00")
    assert len(tab.launches) == 3


def test_historical_price_preserved_across_tab_launches(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 500ml", "20.00")
    tab = container.tabs.open_tab(operator, number=8)

    # 1º lançamento com preço R$ 20,00
    tab, l1, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab8-hist-price-1",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )
    assert l1.subtotal == Decimal("20.00")
    assert l1.items[0].unit_price == Decimal("20.00")

    # Admin altera o preço do produto para R$ 25,00
    container.products.update_product(admin, p.id, "Açaí 500ml", "25.00")

    # 2º lançamento usa o novo preço (R$ 25,00), mas o 1º lançamento permanece R$ 20,00
    tab, l2, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab8-hist-price-2",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )
    assert l2.subtotal == Decimal("25.00")
    assert l2.items[0].unit_price == Decimal("25.00")

    # Total consumido deve ser 20.00 + 25.00 = 45.00 (nunca 50.00)
    assert tab.subtotal_consumed == Decimal("45.00")
    assert tab.launches[0].items[0].unit_price == Decimal("20.00")
    assert tab.launches[0].subtotal == Decimal("20.00")
    assert tab.launches[1].items[0].unit_price == Decimal("25.00")
    assert tab.launches[1].subtotal == Decimal("25.00")


def test_tab_launch_with_custom_items(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 300ml", "15.00")
    tab = container.tabs.open_tab(operator, number=9)

    tab, launch, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab9-custom-launch-1",
        items=[
            QuickSaleItemInput(product_id=p.id, quantity=2),
            QuickSaleItemInput(custom_description="Cobertura extra creme de avelã", custom_amount="6.50", quantity=1),
        ],
    )
    assert launch.subtotal == Decimal("36.50")
    assert tab.subtotal_consumed == Decimal("36.50")
    assert tab.balance_due == Decimal("36.50")
    assert len(launch.items) == 2
    assert launch.items[1].item_kind == SaleItemKind.CUSTOM
    assert launch.items[1].product_id is None
    assert launch.items[1].description == "Cobertura extra creme de avelã"
    assert launch.items[1].line_total == Decimal("6.50")


def test_partial_payments_and_subsequent_consumption_exact_spec_flow(container, admin, operator, clock):
    """Exemplo exato da especificação (§5):
    - Consumo inicial: R$ 40,00.
    - Pagamento Pix: R$ 15,00.
    - Saldo pendente: R$ 25,00.
    - Novo consumo: R$ 20,00.
    - Saldo pendente: R$ 45,00.
    - Pagamento dinheiro: R$ 45,00.
    - Saldo final: R$ 0,00.
    """
    p20 = container.products.create_product(admin, "Açaí 500ml", "20.00")
    tab = container.tabs.open_tab(operator, number=30, customer_name="Pedro")

    # 1. Consumo inicial: R$ 40,00 (2 x R$ 20,00)
    tab, _, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab30-consume-40",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=2)],
    )
    assert tab.subtotal_consumed == Decimal("40.00")
    assert tab.principal_paid == Decimal("0.00")
    assert tab.balance_due == Decimal("40.00")
    assert tab.can_close is False

    # 2. Pagamento parcial Pix: R$ 15,00 -> Saldo pendente: R$ 25,00
    clock.advance(minutes=10)
    tab, movs1, created1 = container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab30-pay-pix-15",
        payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
    )
    assert created1 is True
    assert len(movs1) == 1
    assert movs1[0].amount == Decimal("15.00")
    assert tab.subtotal_consumed == Decimal("40.00")
    assert tab.principal_paid == Decimal("15.00")
    assert tab.balance_due == Decimal("25.00")
    assert tab.can_close is False

    # 3. Novo consumo: R$ 20,00 -> Consumo acumulado R$ 60,00, Saldo pendente R$ 45,00
    clock.advance(minutes=15)
    tab, _, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab30-consume-20",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )
    assert tab.subtotal_consumed == Decimal("60.00")
    assert tab.principal_paid == Decimal("15.00")
    assert tab.balance_due == Decimal("45.00")
    assert tab.can_close is False

    # Não permite pagar principal maior que o saldo pendente (ex.: R$ 45,01)
    with pytest.raises(InvalidPaymentError):
        container.tabs.record_payment(
            actor=operator,
            tab_id=tab.id,
            idempotency_key="tab30-overpay-attempt",
            payments=[QuickSalePaymentInput(method="pix", amount="45.01")],
        )

    # 4. Pagamento dinheiro: R$ 45,00 -> Saldo final: R$ 0,00
    clock.advance(minutes=10)
    tab, movs2, created2 = container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab30-pay-cash-45",
        payments=[QuickSalePaymentInput(method="cash", amount="45.00", cash_tendered="45.00")],
    )
    assert created2 is True
    assert len(movs2) == 1
    assert movs2[0].amount == Decimal("45.00")
    assert tab.subtotal_consumed == Decimal("60.00")
    assert tab.principal_paid == Decimal("60.00")
    assert tab.balance_due == Decimal("0.00")
    assert tab.can_close is True
    assert len(tab.payments) == 2

    # Verifica que pagamentos não foram tratados como novas vendas nem duplicaram valores no resumo
    summary = container.sales.get_financial_summary()
    assert summary["active_sales_count"] == 2
    assert summary["active_sales_subtotal"] == "60.00"
    assert summary["tabs_consumed_subtotal"] == "60.00"
    assert summary["principal_received_total"] == "60.00"
    assert summary["tabs_pending_balance_total"] == "0.00"
    assert summary["by_method"]["pix"]["net_principal"] == "15.00"
    assert summary["by_method"]["cash"]["net_principal"] == "45.00"


def test_tab_split_payment_and_card_fee_only_on_card_portion(container, admin, operator):
    """Exemplos da especificação (§6):
    1. Saldo pendente: R$ 30,00. Pagamento integral em cartão com taxa de 3% (acréscimo simples):
       - Principal quitado: R$ 30,00
       - Taxa: R$ 0,90
       - Total cobrado: R$ 30,90
       - Saldo da comanda: R$ 0,00
    2. Pagamento dividido na comanda (Pix + Cartão + Dinheiro com troco) e preservação histórica da taxa.
    """
    p30 = container.products.create_product(admin, "Barca Média", "30.00")
    container.settings.update_settings(admin, card_fee_percent="3.00", card_fee_mode="simple_addition")

    # Caso 1: Saldo pendente R$ 30,00 pago integralmente no cartão com 3%
    tab1 = container.tabs.open_tab(operator, number=41)
    container.tabs.add_items(
        actor=operator,
        tab_id=tab1.id,
        idempotency_key="tab41-launch-30",
        items=[QuickSaleItemInput(product_id=p30.id, quantity=1)],
    )
    tab1, movs1, _ = container.tabs.record_payment(
        actor=operator,
        tab_id=tab1.id,
        idempotency_key="tab41-pay-card-30",
        payments=[QuickSalePaymentInput(method="card", amount="30.00")],
    )
    assert len(movs1) == 1
    assert movs1[0].amount == Decimal("30.00")
    assert movs1[0].fee_percent == Decimal("3.00")
    assert movs1[0].fee_mode == "simple_addition"
    assert movs1[0].fee_amount == Decimal("0.90")
    assert movs1[0].gross_amount == Decimal("30.90")
    assert tab1.subtotal_consumed == Decimal("30.00")
    assert tab1.principal_paid == Decimal("30.00")
    assert tab1.card_fee_paid == Decimal("0.90")
    assert tab1.total_charged_paid == Decimal("30.90")
    assert tab1.balance_due == Decimal("0.00")

    # Caso 2: Comanda #42 com consumo de R$ 60,00, paga de forma dividida:
    # R$ 15,00 Pix + R$ 20,00 Cartão (+ R$ 0,60 taxa = R$ 20,60) + R$ 25,00 Dinheiro (entregue R$ 50,00 -> troco R$ 25,00)
    tab2 = container.tabs.open_tab(operator, number=42)
    container.tabs.add_items(
        actor=operator,
        tab_id=tab2.id,
        idempotency_key="tab42-launch-60",
        items=[QuickSaleItemInput(product_id=p30.id, quantity=2)],
    )
    tab2, movs2, _ = container.tabs.record_payment(
        actor=operator,
        tab_id=tab2.id,
        idempotency_key="tab42-pay-split",
        payments=[
            QuickSalePaymentInput(method="pix", amount="15.00"),
            QuickSalePaymentInput(method="card", amount="20.00"),
            QuickSalePaymentInput(method="cash", amount="25.00", cash_tendered="50.00"),
        ],
    )
    assert len(movs2) == 3
    pix_m, card_m, cash_m = movs2
    assert pix_m.amount == Decimal("15.00")
    assert pix_m.fee_amount == Decimal("0.00")
    assert pix_m.gross_amount == Decimal("15.00")

    assert card_m.amount == Decimal("20.00")
    assert card_m.fee_percent == Decimal("3.00")
    assert card_m.fee_amount == Decimal("0.60")
    assert card_m.gross_amount == Decimal("20.60")

    assert cash_m.amount == Decimal("25.00")
    assert cash_m.fee_amount == Decimal("0.00")
    assert cash_m.gross_amount == Decimal("25.00")
    assert cash_m.cash_tendered == Decimal("50.00")
    assert cash_m.change_given == Decimal("25.00")

    assert tab2.subtotal_consumed == Decimal("60.00")
    assert tab2.principal_paid == Decimal("60.00")
    assert tab2.card_fee_paid == Decimal("0.60")
    assert tab2.total_charged_paid == Decimal("60.60")
    assert tab2.balance_due == Decimal("0.00")

    # Altera a configuração de taxa para 5.00% e verifica que os pagamentos históricos preservaram 3.00%
    container.settings.update_settings(admin, card_fee_percent="5.00", card_fee_mode="simple_addition")
    reloaded_tab1 = container.tabs.get_tab(tab1.id)
    assert reloaded_tab1.payments[0].fee_percent == Decimal("3.00")
    assert reloaded_tab1.payments[0].fee_amount == Decimal("0.90")
    assert reloaded_tab1.card_fee_paid == Decimal("0.90")


def test_cannot_close_tab_with_pending_balance_and_cannot_modify_closed_tab(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 500ml", "25.00")
    tab = container.tabs.open_tab(operator, number=50)

    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab50-launch-25",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )
    container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab50-pay-10",
        payments=[QuickSalePaymentInput(method="pix", amount="10.00")],
    )

    # Saldo pendente de R$ 15,00 impede encerramento
    with pytest.raises(TabBalancePendingError):
        container.tabs.close_tab(operator, tab_id=tab.id)

    # Quita os R$ 15,00 restantes e encerra a comanda
    container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab50-pay-15",
        payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
    )
    closed = container.tabs.close_tab(operator, tab_id=tab.id, notes="Quitado integralmente")
    assert closed.status == TabStatus.CLOSED
    assert closed.balance_due == Decimal("0.00")
    assert closed.can_close is False

    # Tentativas de alterar comanda já encerrada são bloqueadas com TabClosedError
    with pytest.raises(TabClosedError):
        container.tabs.add_items(
            actor=operator,
            tab_id=tab.id,
            idempotency_key="tab50-launch-after-close",
            items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        )

    with pytest.raises(TabClosedError):
        container.tabs.record_payment(
            actor=operator,
            tab_id=tab.id,
            idempotency_key="tab50-pay-after-close",
            payments=[QuickSalePaymentInput(method="pix", amount="5.00")],
        )

    with pytest.raises(TabClosedError):
        container.tabs.void_tab_item(
            actor=admin,
            tab_id=tab.id,
            sale_id=closed.launches[0].id,
            reason="Tentativa de cancelar item de comanda encerrada",
        )

    with pytest.raises(TabClosedError):
        container.tabs.close_tab(operator, tab_id=tab.id)

    # Consulta somente leitura continua disponível
    read_only = container.tabs.get_tab(tab.id)
    assert read_only.status == TabStatus.CLOSED
    assert read_only.subtotal_consumed == Decimal("25.00")
    assert read_only.principal_paid == Decimal("25.00")


def test_idempotency_on_tab_launches_and_tab_payments(client, admin, operator):
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 500ml", "price": "20.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    tab = client.post("/api/tabs", json={"number": 60}, headers=op_headers).get_json()["tab"]

    # 1. Reenvio idempotente de lançamento de consumo
    launch_payload = {
        "idempotency_key": "tab60-idem-launch-001",
        "items": [{"product_id": p["id"], "quantity": 2}],
    }
    first_l = client.post(f"/api/tabs/{tab['id']}/items", json=launch_payload, headers=op_headers)
    assert first_l.status_code == 201
    assert first_l.get_json()["idempotent_replay"] is False
    launch_id = first_l.get_json()["launch"]["id"]

    replay_l = client.post(f"/api/tabs/{tab['id']}/items", json=launch_payload, headers=op_headers)
    assert replay_l.status_code == 200
    assert replay_l.get_json()["idempotent_replay"] is True
    assert replay_l.get_json()["launch"]["id"] == launch_id
    assert replay_l.get_json()["tab"]["subtotal_consumed"] == "40.00"

    # Mesma chave de lançamento com payload diferente -> 409 Conflict
    conflict_l = client.post(
        f"/api/tabs/{tab['id']}/items",
        json={"idempotency_key": "tab60-idem-launch-001", "items": [{"product_id": p["id"], "quantity": 3}]},
        headers=op_headers,
    )
    assert conflict_l.status_code == 409
    assert conflict_l.get_json()["error"]["code"] == "idempotency_conflict"

    # 2. Reenvio idempotente de pagamento parcial na comanda
    pay_payload = {
        "idempotency_key": "tab60-idem-pay-001",
        "payments": [{"method": "pix", "amount": "15.00"}],
    }
    first_p = client.post(f"/api/tabs/{tab['id']}/payments", json=pay_payload, headers=op_headers)
    assert first_p.status_code == 201
    assert first_p.get_json()["idempotent_replay"] is False
    assert first_p.get_json()["tab"]["principal_paid"] == "15.00"
    assert first_p.get_json()["tab"]["balance_due"] == "25.00"

    replay_p = client.post(f"/api/tabs/{tab['id']}/payments", json=pay_payload, headers=op_headers)
    assert replay_p.status_code == 200
    assert replay_p.get_json()["idempotent_replay"] is True
    assert replay_p.get_json()["tab"]["principal_paid"] == "15.00"
    assert replay_p.get_json()["tab"]["balance_due"] == "25.00"

    # Mesma chave de pagamento com valor diferente -> 409 Conflict
    conflict_p = client.post(
        f"/api/tabs/{tab['id']}/payments",
        json={"idempotency_key": "tab60-idem-pay-001", "payments": [{"method": "pix", "amount": "20.00"}]},
        headers=op_headers,
    )
    assert conflict_p.status_code == 409
    assert conflict_p.get_json()["error"]["code"] == "idempotency_conflict"


def test_concurrency_open_same_tab_number_and_concurrent_overpayment_protection(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 500ml", "30.00")

    # 1. Concorrência ao abrir comanda com o mesmo número (#77): apenas uma deve ter sucesso
    def try_open():
        try:
            return ("ok", container.tabs.open_tab(operator, number=77))
        except TabAlreadyOpenError as exc:
            return ("conflict", exc)

    with ThreadPoolExecutor(max_workers=5) as pool:
        open_results = [f.result() for f in [pool.submit(try_open) for _ in range(5)]]

    successes = [r for status, r in open_results if status == "ok"]
    conflicts = [r for status, r in open_results if status == "conflict"]
    assert len(successes) == 1
    assert len(conflicts) == 4

    tab = successes[0]
    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab77-launch-30",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )

    # 2. Concorrência ao pagar: saldo é R$ 30,00, duas threads tentam pagar R$ 20,00 com chaves distintas
    # Apenas uma pode ser aceita (deixando saldo R$ 10,00); a outra deve ser rejeitada por exceder o saldo restante!
    def try_pay(idx: int):
        try:
            res = container.tabs.record_payment(
                actor=operator,
                tab_id=tab.id,
                idempotency_key=f"tab77-concurrent-pay-{idx}",
                payments=[QuickSalePaymentInput(method="pix", amount="20.00")],
            )
            return ("ok", res)
        except InvalidPaymentError as exc:
            return ("rejected", exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pay_results = [f.result() for f in [pool.submit(try_pay, 1), pool.submit(try_pay, 2)]]

    pay_ok = [r for status, r in pay_results if status == "ok"]
    pay_rejected = [r for status, r in pay_results if status == "rejected"]
    assert len(pay_ok) == 1
    assert len(pay_rejected) == 1

    final_tab = container.tabs.get_tab(tab.id)
    assert final_tab.subtotal_consumed == Decimal("30.00")
    assert final_tab.principal_paid == Decimal("20.00")
    assert final_tab.balance_due == Decimal("10.00")
    assert len(final_tab.payments) == 1


def test_atomic_rollback_on_tab_launch_and_tab_payment_failures(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 500ml", "20.00")
    tab = container.tabs.open_tab(operator, number=88)

    # 1. Simula falha ao gravar o 2º item do lançamento da comanda
    from src.adapters.db.repositories import MariaDbCashMovementRepository, MariaDbSaleRepository

    orig_add_item = MariaDbSaleRepository.add_item
    item_calls = {"n": 0}

    def fail_on_second_item(self, *args, **kwargs):
        item_calls["n"] += 1
        if item_calls["n"] == 2:
            raise RuntimeError("Falha simulada ao inserir segundo item na comanda")
        return orig_add_item(self, *args, **kwargs)

    with patch.object(MariaDbSaleRepository, "add_item", new=fail_on_second_item):
        with pytest.raises(RuntimeError):
            container.tabs.add_items(
                actor=operator,
                tab_id=tab.id,
                idempotency_key="tab88-rollback-items",
                items=[
                    QuickSaleItemInput(product_id=p.id, quantity=1),
                    QuickSaleItemInput(custom_description="Extra", custom_amount="5.00", quantity=1),
                ],
            )

    tab_after_item_fail = container.tabs.get_tab(tab.id)
    assert len(tab_after_item_fail.launches) == 0
    assert tab_after_item_fail.subtotal_consumed == Decimal("0.00")

    # Adiciona lançamento válido de R$ 40,00
    container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab88-valid-launch",
        items=[QuickSaleItemInput(product_id=p.id, quantity=2)],
    )

    # 2. Simula falha ao gravar a 2ª parcela de pagamento dividido na comanda
    orig_add_mov = MariaDbCashMovementRepository.add
    mov_calls = {"n": 0}

    def fail_on_second_mov(self, *args, **kwargs):
        mov_calls["n"] += 1
        if mov_calls["n"] == 2:
            raise RuntimeError("Falha simulada ao inserir segunda parcela de pagamento na comanda")
        return orig_add_mov(self, *args, **kwargs)

    with patch.object(MariaDbCashMovementRepository, "add", new=fail_on_second_mov):
        with pytest.raises(RuntimeError):
            container.tabs.record_payment(
                actor=operator,
                tab_id=tab.id,
                idempotency_key="tab88-rollback-pay",
                payments=[
                    QuickSalePaymentInput(method="pix", amount="15.00"),
                    QuickSalePaymentInput(method="cash", amount="25.00", cash_tendered="25.00"),
                ],
            )

    tab_after_pay_fail = container.tabs.get_tab(tab.id)
    assert len(tab_after_pay_fail.payments) == 0
    assert tab_after_pay_fail.principal_paid == Decimal("0.00")
    assert tab_after_pay_fail.balance_due == Decimal("40.00")


def test_auditable_cancellation_of_tab_launches_with_and_without_associated_payments(
    container, admin, operator, clock
):
    """Valida todas as regras da §10 para cancelamentos em comandas:
    - Exige autorização administrativa e motivo obrigatório.
    - Não exclui fisicamente registros.
    - Quando o consumo cancelado não gera excesso de pagamentos sobre o consumo restante,
      apenas reduz o consumo ativo e o saldo pendente (`customer_refund_status = 'not_applicable'`).
    - Quando há pagamento associado que excede o consumo restante:
      * NÃO gera devolução financeira fictícia automática (`direction = 'out'`).
      * Mantém o valor no caixa (`net_gross`) e registra obrigação de devolução pendente (`pending_refund_total`).
      * Bloqueia encerramento da comanda enquanto a obrigação de devolução estiver pendente.
      * Após confirmação explícita da devolução efetiva ao cliente, grava o `reversal` (`direction = 'out'`)
        e permite encerrar a comanda.
    - Bloqueia cancelamentos em turnos fechados.
    """
    p20 = container.products.create_product(admin, "Açaí 500ml", "20.00")
    tab = container.tabs.open_tab(operator, number=90)

    # Lançamento 1: R$ 20,00 | Lançamento 2: R$ 20,00 (Consumo total = R$ 40,00)
    _, l1, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab90-launch-1",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )
    tab, l2, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab90-launch-2",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )

    # Cliente paga parcialmente R$ 15,00 em Pix (Saldo pendente = R$ 25,00)
    tab, _, _ = container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab90-pay-15",
        payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
    )

    # Operador não tem permissão para cancelar lançamento
    with pytest.raises(PermissionDeniedError):
        container.tabs.void_tab_item(actor=operator, tab_id=tab.id, sale_id=l2.id, reason="Erro de digitação")

    # Motivo curto/vazio é rejeitado
    with pytest.raises(InvalidInputError):
        container.tabs.void_tab_item(actor=admin, tab_id=tab.id, sale_id=l2.id, reason="ok")

    # 1. Admin cancela o Lançamento 2 (R$ 20,00).
    # Como o consumo ativo restante (R$ 20,00 do Lançamento 1) >= R$ 15,00 já pagos,
    # não há devolução devida ao cliente; o saldo pendente cai de R$ 25,00 para R$ 5,00!
    tab, voided_l2 = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab.id,
        sale_id=l2.id,
        reason="Lançado duplicado por engano",
    )
    assert voided_l2.status == SaleStatus.VOIDED
    assert voided_l2.customer_refund_status == CustomerRefundStatus.NOT_APPLICABLE
    assert voided_l2.customer_refund_amount == Decimal("0.00")
    assert tab.subtotal_consumed == Decimal("20.00")
    assert tab.voided_subtotal == Decimal("20.00")
    assert tab.principal_paid == Decimal("15.00")
    assert tab.balance_due == Decimal("5.00")
    assert tab.pending_refund_total == Decimal("0.00")

    # 2. Agora Admin também cancela o Lançamento 1 (R$ 20,00) sem devolução imediata (customer_refund_completed=False).
    # Agora o consumo ativo vai a R$ 0,00, mas o cliente já havia pago R$ 15,00 em Pix!
    tab, voided_l1 = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab.id,
        sale_id=l1.id,
        reason="Cliente desistiu antes do preparo",
        customer_refund_completed=False,
    )
    assert voided_l1.status == SaleStatus.VOIDED
    assert voided_l1.customer_refund_status == CustomerRefundStatus.PENDING
    assert voided_l1.customer_refund_amount == Decimal("15.00")

    # Saldo pendente NÃO fica negativo (é R$ 0,00), mas há obrigação de devolução pendente de R$ 15,00!
    assert tab.subtotal_consumed == Decimal("0.00")
    assert tab.voided_subtotal == Decimal("40.00")
    assert tab.balance_due == Decimal("0.00")
    assert tab.pending_refund_total == Decimal("15.00")
    assert tab.net_principal_paid == Decimal("15.00")  # Dinheiro ainda não saiu do caixa!
    assert tab.principal_paid == Decimal("0.00")      # Receita reconhecida = 0.00!
    assert tab.can_close is False

    # Nenhum movimento de saída fictício foi criado automaticamente
    assert len(tab.payments) == 1
    assert tab.payments[0].direction == CashDirection.IN

    # Não permite encerrar a comanda enquanto houver devolução pendente (operação financeira incompleta)
    with pytest.raises(TabIncompleteOperationsError):
        container.tabs.close_tab(operator, tab_id=tab.id)

    summary_pending = container.sales.get_financial_summary()
    assert summary_pending["active_sales_subtotal"] == "0.00"
    assert summary_pending["pending_customer_refunds_count"] == 1
    assert summary_pending["pending_customer_refunds_total"] == "15.00"
    assert summary_pending["by_method"]["pix"]["recognized_gross"] == "0.00"
    assert summary_pending["by_method"]["pix"]["net_gross"] == "15.00"

    # 3. Admin confirma a devolução efetiva dos R$ 15,00 ao cliente
    tab, refunded_l1 = container.tabs.confirm_tab_customer_refund(
        actor=admin,
        tab_id=tab.id,
        sale_id=l1.id,
        method=PaymentMethod.PIX,
        notes="Estornado via Pix ao cliente",
    )
    assert refunded_l1.customer_refund_status == CustomerRefundStatus.COMPLETED
    assert tab.pending_refund_total == Decimal("0.00")
    assert tab.completed_refund_total == Decimal("15.00")
    assert tab.net_principal_paid == Decimal("0.00")
    assert tab.can_close is True
    assert len(tab.payments) == 2
    assert tab.payments[1].kind == CashMovementKind.REVERSAL
    assert tab.payments[1].direction == CashDirection.OUT
    assert tab.payments[1].amount == Decimal("15.00")
    assert tab.payments[1].reverses_id == tab.payments[0].id

    summary_after = container.sales.get_financial_summary()
    assert summary_after["pending_customer_refunds_count"] == 0
    assert summary_after["pending_customer_refunds_total"] == "0.00"
    assert summary_after["completed_customer_refunds_total"] == "15.00"
    assert summary_after["by_method"]["pix"]["net_gross"] == "0.00"

    # Agora a comanda pode ser encerrada
    closed = container.tabs.close_tab(operator, tab_id=tab.id)
    assert closed.status == TabStatus.CLOSED


def test_cannot_void_tab_launch_when_shift_is_closed(container, admin, operator, clock):
    p = container.products.create_product(admin, "Açaí 300ml", "15.00")
    tab = container.tabs.open_tab(operator, number=91)
    _, launch, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab91-launch-1",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
    )

    from src.adapters.db.database import MariaDbUnitOfWork
    with MariaDbUnitOfWork(container.db) as uow:
        uow.shifts.close_shift(
            shift_id=launch.shift_id,
            closed_by=admin.id,
            now=clock.now(),
            counted_cash=Decimal("0.00"),
            notes="Turno fechado",
        )

    with pytest.raises(ShiftClosedError):
        container.tabs.void_tab_item(
            actor=admin,
            tab_id=tab.id,
            sale_id=launch.id,
            reason="Tentativa após fechamento do turno",
        )


def test_integration_between_quick_sales_and_tabs_in_financial_summary(container, admin, operator):
    """Verifica que vendas rápidas e comandas compartilham o mesmo livro-razão sem duplicar valores,
    respeitando a identidade:
    active_sales_subtotal == principal_received_total + tabs_pending_balance_total.
    """
    p20 = container.products.create_product(admin, "Açaí 500ml", "20.00")
    p30 = container.products.create_product(admin, "Barca Açaí", "30.00")
    container.settings.update_settings(admin, card_fee_percent="3.00", card_fee_mode="simple_addition")

    # 1. Venda rápida: R$ 20,00 em dinheiro (entregue R$ 50,00 -> troco R$ 30,00)
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="integ-quick-sale-20",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="20.00", cash_tendered="50.00")],
    )

    # 2. Comanda #1 (encerrada): consumo de R$ 30,00 pago no cartão com taxa 3% (R$ 0,90)
    t1 = container.tabs.open_tab(operator, number=1, customer_name="Ana")
    container.tabs.add_items(
        actor=operator,
        tab_id=t1.id,
        idempotency_key="integ-tab1-launch-30",
        items=[QuickSaleItemInput(product_id=p30.id, quantity=1)],
    )
    container.tabs.record_payment(
        actor=operator,
        tab_id=t1.id,
        idempotency_key="integ-tab1-pay-card-30",
        payments=[QuickSalePaymentInput(method="card", amount="30.00")],
    )
    container.tabs.close_tab(operator, tab_id=t1.id)

    # 3. Comanda #2 (aberta com pagamento parcial): consumo de R$ 50,00, pago R$ 15,00 em Pix (pendente R$ 35,00)
    t2 = container.tabs.open_tab(operator, number=2, customer_name="Bruno")
    container.tabs.add_items(
        actor=operator,
        tab_id=t2.id,
        idempotency_key="integ-tab2-launch-50",
        items=[
            QuickSaleItemInput(product_id=p20.id, quantity=1),
            QuickSaleItemInput(product_id=p30.id, quantity=1),
        ],
    )
    container.tabs.record_payment(
        actor=operator,
        tab_id=t2.id,
        idempotency_key="integ-tab2-pay-pix-15",
        payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
    )

    summary = container.sales.get_financial_summary()
    # Consumo registrado: 20.00 (venda rápida) + 30.00 (comanda 1) + 50.00 (comanda 2) = 100.00
    assert summary["quick_sales_subtotal"] == "20.00"
    assert summary["tabs_consumed_subtotal"] == "80.00"
    assert summary["active_sales_subtotal"] == "100.00"

    # Principal recebido: 20.00 (dinheiro) + 30.00 (cartão) + 15.00 (pix) = 65.00
    assert summary["principal_received_total"] == "65.00"

    # Saldo pendente nas comandas abertas: 35.00 (100.00 - 65.00)
    assert summary["tabs_open_count"] == 1
    assert summary["tabs_closed_count"] == 1
    assert summary["tabs_pending_balance_total"] == "35.00"

    # Taxa de cartão recebida: 0.90
    assert summary["active_card_fees"] == "0.90"
    assert summary["card_fees_received_total"] == "0.90"

    # Por método:
    assert summary["by_method"]["cash"]["recognized_principal"] == "20.00"
    assert summary["by_method"]["cash"]["net_gross"] == "20.00"
    assert summary["by_method"]["card"]["recognized_principal"] == "30.00"
    assert summary["by_method"]["card"]["recognized_fee"] == "0.90"
    assert summary["by_method"]["card"]["recognized_gross"] == "30.90"
    assert summary["by_method"]["pix"]["recognized_principal"] == "15.00"
    assert summary["by_method"]["pix"]["net_gross"] == "15.00"


def test_mixed_payments_reversal_prioritizes_non_card_and_does_not_presume_card_fee_refund(
    container, admin, operator
):
    """Regressão:
    1. Em pagamentos mistos (ex.: Pix + Cartão, independentemente da ordem em que o cartão foi lançado),
       o cancelamento de consumo aloca o excesso primeiro sobre meios sem taxa (Pix/dinheiro) antes de tocar
       em parcelas de cartão.
    2. Quando o excesso alcança uma parcela de cartão (parcial ou integralmente), o sistema NÃO presume
       devolução automática nem proporcional da taxa de cartão pela maquininha: a obrigação de devolução
       limita-se ao principal excedente (`fee_amount = 0.00`), preservando a taxa cobrada no livro-razão.
    """
    p15 = container.products.create_product(admin, "Açaí 300ml", "15.00")
    p25 = container.products.create_product(admin, "Açaí 500ml", "25.00")
    container.settings.update_settings(admin, card_fee_percent="4.00", card_fee_mode="simple_addition")

    tab = container.tabs.open_tab(operator, number=95, customer_name="Pagamentos Mistos")
    _, l1, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab95-l1-25",
        items=[QuickSaleItemInput(product_id=p25.id, quantity=1)],
    )
    _, l2, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab95-l2-15",
        items=[QuickSaleItemInput(product_id=p15.id, quantity=1)],
    )

    # Pagamento dividido com Cartão enviado DEPOIS do Pix:
    # Pix R$ 15,00 (sem taxa) + Cartão R$ 25,00 (taxa 4% = R$ 1,00 -> bruto R$ 26,00)
    tab, _, _ = container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="tab95-split-pix-card",
        payments=[
            QuickSalePaymentInput(method="pix", amount="15.00"),
            QuickSalePaymentInput(method="card", amount="25.00"),
        ],
    )
    assert tab.principal_paid == Decimal("40.00")
    assert tab.card_fee_paid == Decimal("1.00")
    assert tab.balance_due == Decimal("0.00")

    # 1. Admin cancela o lançamento l2 (R$ 15,00) com devolução imediata em Pix.
    # Mesmo tendo o Cartão sido a última linha do pagamento dividido, o sistema deve reverter
    # os R$ 15,00 do Pix (meio sem taxa) e preservar intacta a transação de Cartão (R$ 25,00 + R$ 1,00)!
    tab, voided_l2 = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab.id,
        sale_id=l2.id,
        reason="Cancelamento coberto pela parcela Pix",
        customer_refund_completed=True,
        customer_refund_method="pix",
    )
    assert voided_l2.customer_refund_status == CustomerRefundStatus.COMPLETED
    assert voided_l2.customer_refund_amount == Decimal("15.00")
    assert tab.subtotal_consumed == Decimal("25.00")
    assert tab.principal_paid == Decimal("25.00")
    assert tab.card_fee_paid == Decimal("1.00")

    summary_after_l2 = container.sales.get_financial_summary()
    assert summary_after_l2["by_method"]["pix"]["recognized_principal"] == "0.00"
    assert summary_after_l2["by_method"]["pix"]["net_gross"] == "0.00"
    assert summary_after_l2["by_method"]["card"]["recognized_principal"] == "25.00"
    assert summary_after_l2["by_method"]["card"]["recognized_fee"] == "1.00"
    assert summary_after_l2["card_fees_charged_total"] == "1.00"
    assert summary_after_l2["card_fees_reversed_total"] == "0.00"

    # 2. Agora Admin cancela também o lançamento l1 (R$ 25,00), que foi pago no Cartão.
    # O sistema NÃO deve presumir devolução automática da taxa de R$ 1,00 pela maquininha:
    # a devolução ao cliente é restrita ao principal excedente (R$ 25,00), mantendo a taxa cobrada (R$ 1,00) registrada.
    tab, voided_l1 = container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab.id,
        sale_id=l1.id,
        reason="Cancelamento de item pago no cartão",
        customer_refund_completed=True,
        customer_refund_method="cash",
    )
    assert voided_l1.customer_refund_status == CustomerRefundStatus.COMPLETED
    assert voided_l1.customer_refund_amount == Decimal("25.00")
    assert tab.subtotal_consumed == Decimal("0.00")
    assert tab.net_principal_paid == Decimal("0.00")
    assert tab.card_fee_paid == Decimal("1.00")

    summary_after_l1 = container.sales.get_financial_summary()
    assert summary_after_l1["active_sales_subtotal"] == "0.00"
    assert summary_after_l1["principal_received_total"] == "0.00"
    assert summary_after_l1["card_fees_charged_total"] == "1.00"
    assert summary_after_l1["card_fees_reversed_total"] == "0.00"


def test_financial_summary_equality_invariants_and_advance_credit_isolation(
    container, admin, operator, clock
):
    """Regressão:
    1. Verifica que a igualdade fundamental:
       active_sales_subtotal == quick_sales_subtotal + tabs_consumed_subtotal == principal_received_total + tabs_pending_balance_total
       permanece válida em todos os estados intermediários (comanda aberta, pagamento parcial, cancelamento sem devolução,
       cancelamento com devolução pendente, devolução concluída e comanda encerrada).
    2. Garante que depósitos de crédito antecipado (`kind = 'credit_deposit'`, preparado para o Bloco 4)
       jamais sejam contabilizados como vendas (`active_sales_subtotal`) ou principal recebido de vendas (`principal_received_total`).
    """
    def assert_summary_invariants():
        s = container.sales.get_financial_summary()
        active_sub = Decimal(s["active_sales_subtotal"])
        quick_sub = Decimal(s["quick_sales_subtotal"])
        tabs_sub = Decimal(s["tabs_consumed_subtotal"])
        principal_rec = Decimal(s["principal_received_total"])
        tabs_pending = Decimal(s["tabs_pending_balance_total"])

        assert active_sub == quick_sub + tabs_sub
        assert active_sub == principal_rec + tabs_pending
        return s

    p20 = container.products.create_product(admin, "Açaí 500ml", "20.00")
    assert_summary_invariants()

    # Venda rápida de R$ 20,00
    container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="inv-quick-1",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="pix", amount="20.00")],
    )
    assert_summary_invariants()

    # Comanda com 2 lançamentos de R$ 20,00 (total R$ 40,00)
    tab = container.tabs.open_tab(operator, number=96)
    _, l1, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="inv-tab96-l1",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )
    assert_summary_invariants()

    _, l2, _ = container.tabs.add_items(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="inv-tab96-l2",
        items=[QuickSaleItemInput(product_id=p20.id, quantity=1)],
    )
    assert_summary_invariants()

    # Pagamento parcial de R$ 30,00 em dinheiro
    container.tabs.record_payment(
        actor=operator,
        tab_id=tab.id,
        idempotency_key="inv-tab96-pay-30",
        payments=[QuickSalePaymentInput(method="cash", amount="30.00", cash_tendered="30.00")],
    )
    assert_summary_invariants()

    # Cancela l2 (R$ 20,00) deixando excesso pendente de R$ 10,00 (customer_refund_status = 'pending')
    container.tabs.void_tab_item(
        actor=admin,
        tab_id=tab.id,
        sale_id=l2.id,
        reason="Teste invariante com devolução pendente",
        customer_refund_completed=False,
    )
    s_pending = assert_summary_invariants()
    assert s_pending["pending_customer_refunds_total"] == "10.00"

    # Confirma a devolução dos R$ 10,00 ao cliente
    container.tabs.confirm_tab_customer_refund(
        actor=admin,
        tab_id=tab.id,
        sale_id=l2.id,
        method="cash",
    )
    assert_summary_invariants()

    # Simula um depósito de crédito antecipado (Bloco 4: kind='credit_deposit') no livro-razão
    from src.adapters.db.database import MariaDbUnitOfWork
    with MariaDbUnitOfWork(container.db) as uow:
        uow.cash_movements.add(
            shift_id=tab.shift_id,
            tab_id=tab.id,
            sale_id=None,
            kind=CashMovementKind.CREDIT_DEPOSIT,
            method=PaymentMethod.PIX,
            direction=CashDirection.IN,
            amount=Decimal("50.00"),
            fee_percent=Decimal("0.00"),
            fee_mode="none",
            fee_amount=Decimal("0.00"),
            gross_amount=Decimal("50.00"),
            cash_tendered=None,
            change_given=None,
            reverses_id=None,
            operator_id=operator.id,
            created_at=clock.now(),
            idempotency_key="inv-credit-deposit-50",
            note="Depósito antecipado simulado (Bloco 4)",
        )

    # O depósito antecipado entra no saldo físico (net_gross), mas NUNCA em vendas ou principal reconhecido!
    s_with_credit = assert_summary_invariants()
    assert s_with_credit["active_sales_subtotal"] == "40.00"
    assert s_with_credit["principal_received_total"] == "40.00"
    assert s_with_credit["tabs_pending_balance_total"] == "0.00"
    assert s_with_credit["by_method"]["pix"]["recognized_principal"] == "20.00"
    assert s_with_credit["by_method"]["pix"]["net_gross"] == "70.00"

