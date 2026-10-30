"""Testes de vendas rápidas, pagamentos, taxas de cartão e troco (valores pré-calculados)."""
from decimal import Decimal
from unittest.mock import patch

import pytest

from conftest import logged_in
from src.domain.errors import (
    InactiveProductError,
    InvalidInputError,
    InvalidPaymentError,
    ProductNotFoundError,
)
from src.domain.finance import calculate_card_fee
from src.domain.models import CardFeeMode, PaymentMethod, SaleItemKind
from src.use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput


@pytest.fixture(autouse=True)
def _open_default_shift(container, operator):
    container.shifts.open_shift(actor=operator, opening_cash="0.00")


def test_quick_sale_single_product_pix(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 300ml", "15.00")

    sale, created = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-single-pix-001",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
    )
    assert created is True
    assert sale.subtotal == Decimal("15.00")
    assert sale.card_fee_total == Decimal("0.00")
    assert sale.total_charged == Decimal("15.00")
    assert len(sale.items) == 1
    assert sale.items[0].unit_price == Decimal("15.00")
    assert sale.items[0].line_total == Decimal("15.00")
    assert len(sale.payments) == 1
    pay = sale.payments[0]
    assert pay.method == PaymentMethod.PIX
    assert pay.amount == Decimal("15.00")
    assert pay.fee_amount == Decimal("0.00")
    assert pay.gross_amount == Decimal("15.00")
    assert pay.cash_tendered is None
    assert pay.change_given is None


def test_quick_sale_multiple_products_and_custom_item(container, admin, operator):
    p1 = container.products.create_product(admin, "Açaí 500ml", "20.00")
    p2 = container.products.create_product(admin, "Adicional Granola", "3.50")

    # 2 x 20.00 (40.00) + 3 x 3.50 (10.50) + 1 avulso de 4.50 = 55.00
    sale, created = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-multi-custom-001",
        items=[
            QuickSaleItemInput(product_id=p1.id, quantity=2),
            QuickSaleItemInput(product_id=p2.id, quantity=3),
            QuickSaleItemInput(custom_description="Colher extra embalagem", custom_amount="4.50", quantity=1),
        ],
        payments=[QuickSalePaymentInput(method="pix", amount="55.00")],
    )
    assert created is True
    assert sale.subtotal == Decimal("55.00")
    assert sale.total_charged == Decimal("55.00")
    assert len(sale.items) == 3
    assert sale.items[2].item_kind == SaleItemKind.CUSTOM
    assert sale.items[2].product_id is None
    assert sale.items[2].description == "Colher extra embalagem"
    assert sale.items[2].line_total == Decimal("4.50")


def test_quick_sale_custom_item_only(container, operator):
    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-custom-only-001",
        items=[QuickSaleItemInput(custom_description="Combo Especial Evento", custom_amount="28.90", quantity=1)],
        payments=[QuickSalePaymentInput(method="pix", amount="28.90")],
    )
    assert sale.subtotal == Decimal("28.90")
    assert sale.total_charged == Decimal("28.90")
    assert len(sale.items) == 1
    assert sale.items[0].item_kind == SaleItemKind.CUSTOM


def test_quick_sale_rejects_invalid_quantity_and_inactive_or_unknown_product(container, admin, operator):
    active_p = container.products.create_product(admin, "Açaí Ativo", "15.00")
    inactive_p = container.products.create_product(admin, "Açaí Inativo", "15.00", is_active=False)

    for bad_qty in (0, -1, 1001, True):
        with pytest.raises(InvalidInputError):
            container.sales.confirm_quick_sale(
                actor=operator,
                idempotency_key=f"bad-qty-{bad_qty}",
                items=[QuickSaleItemInput(product_id=active_p.id, quantity=bad_qty)],
                payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
            )

    with pytest.raises(InactiveProductError):
        container.sales.confirm_quick_sale(
            actor=operator,
            idempotency_key="inactive-prod-001",
            items=[QuickSaleItemInput(product_id=inactive_p.id, quantity=1)],
            payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
        )

    with pytest.raises(ProductNotFoundError):
        container.sales.confirm_quick_sale(
            actor=operator,
            idempotency_key="missing-prod-001",
            items=[QuickSaleItemInput(product_id=99999, quantity=1)],
            payments=[QuickSalePaymentInput(method="pix", amount="15.00")],
        )


def test_backend_calculates_price_and_rejects_client_injected_unit_price(client, admin, operator):
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 500ml", "price": "22.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    # Cliente tenta enviar unit_price adulterado no payload JSON (extra="forbid" no Pydantic rejeita)
    tampered = client.post(
        "/api/sales/quick",
        json={
            "idempotency_key": "tampered-price-001",
            "items": [{"product_id": p["id"], "quantity": 2, "unit_price": "1.00"}],
            "payments": [{"method": "pix", "amount": "2.00"}],
        },
        headers=op_headers,
    )
    assert tampered.status_code == 400

    # E se o cliente envia apenas product_id e tenta pagar R$ 2,00 para 2 itens de R$ 22,00 (R$ 44,00):
    underpaid = client.post(
        "/api/sales/quick",
        json={
            "idempotency_key": "tampered-price-002",
            "items": [{"product_id": p["id"], "quantity": 2}],
            "payments": [{"method": "pix", "amount": "2.00"}],
        },
        headers=op_headers,
    )
    assert underpaid.status_code == 400
    assert underpaid.get_json()["error"]["code"] == "invalid_payment"


def test_atomic_rollback_on_failure_persists_nothing(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 500ml", "20.00")

    # Simula falha no meio da persistência (ao gravar o segundo pagamento)
    from src.adapters.db.repositories import MariaDbCashMovementRepository
    original_add = MariaDbCashMovementRepository.add
    calls = {"count": 0}

    def boom(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("Falha simulada ao gravar segunda parcela de pagamento")
        return original_add(self, *args, **kwargs)

    with patch.object(MariaDbCashMovementRepository, "add", new=boom):
        with pytest.raises(RuntimeError):
            container.sales.confirm_quick_sale(
                actor=operator,
                idempotency_key="atomic-rollback-001",
                items=[QuickSaleItemInput(product_id=p.id, quantity=2)],
                payments=[
                    QuickSalePaymentInput(method="pix", amount="15.00"),
                    QuickSalePaymentInput(method="cash", amount="25.00", cash_tendered="25.00"),
                ],
            )

    # Verifica no banco que NADA (nem sale, nem sale_items, nem o 1º cash_movement) ficou gravado
    conn = container.db.connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM sales")
            assert cur.fetchone()["c"] == 0
            cur.execute("SELECT COUNT(*) AS c FROM sale_items")
            assert cur.fetchone()["c"] == 0
            cur.execute("SELECT COUNT(*) AS c FROM cash_movements")
            assert cur.fetchone()["c"] == 0
    finally:
        conn.close()


def test_cash_payment_and_change_recorded_accurately_without_double_counting(container, admin, operator):
    """Exemplo exato da especificação (§7):
    Venda: R$ 32,00. Cliente entrega R$ 50,00 em dinheiro. Troco: R$ 18,00.
    Entrada líquida em dinheiro: R$ 32,00.
    """
    p = container.products.create_product(admin, "Açaí Completo", "32.00")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-cash-change-32-50",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="32.00", cash_tendered="50.00")],
    )
    assert sale.subtotal == Decimal("32.00")
    assert sale.card_fee_total == Decimal("0.00")
    assert sale.total_charged == Decimal("32.00")

    mov = sale.payments[0]
    assert mov.method == PaymentMethod.CASH
    assert mov.amount == Decimal("32.00")
    assert mov.gross_amount == Decimal("32.00")
    assert mov.cash_tendered == Decimal("50.00")
    assert mov.change_given == Decimal("18.00")

    summary = container.sales.get_financial_summary()
    assert summary["active_sales_subtotal"] == "32.00"
    assert summary["active_total_charged"] == "32.00"
    # Entrada líquida em dinheiro no caixa é exatamente 32.00 (nem 50.00, nem 14.00)
    assert summary["by_method"]["cash"]["net_principal"] == "32.00"
    assert summary["by_method"]["cash"]["net_gross"] == "32.00"


def test_split_payment_pix_and_cash(container, admin, operator):
    """Exemplo da especificação (§4):
    Venda de R$ 40,00: R$ 15,00 em Pix e R$ 25,00 em dinheiro.
    """
    p = container.products.create_product(admin, "Açaí 500ml", "20.00")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-split-40-15-25",
        items=[QuickSaleItemInput(product_id=p.id, quantity=2)],
        payments=[
            QuickSalePaymentInput(method="pix", amount="15.00"),
            QuickSalePaymentInput(method="cash", amount="25.00", cash_tendered="30.00"),
        ],
    )
    assert sale.subtotal == Decimal("40.00")
    assert sale.total_charged == Decimal("40.00")
    assert len(sale.payments) == 2
    assert sale.payments[0].method == PaymentMethod.PIX
    assert sale.payments[0].amount == Decimal("15.00")
    assert sale.payments[1].method == PaymentMethod.CASH
    assert sale.payments[1].amount == Decimal("25.00")
    assert sale.payments[1].cash_tendered == Decimal("30.00")
    assert sale.payments[1].change_given == Decimal("5.00")

    summary = container.sales.get_financial_summary()
    assert summary["by_method"]["pix"]["net_principal"] == "15.00"
    assert summary["by_method"]["cash"]["net_principal"] == "25.00"


def test_insufficient_and_excess_payments_are_rejected(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 500ml", "40.00")

    # Insuficiente (R$ 39,99 para venda de R$ 40,00)
    with pytest.raises(InvalidPaymentError):
        container.sales.confirm_quick_sale(
            actor=operator,
            idempotency_key="sale-insufficient-001",
            items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
            payments=[
                QuickSalePaymentInput(method="pix", amount="15.00"),
                QuickSalePaymentInput(method="cash", amount="24.99"),
            ],
        )

    # Pagamento excedente indevido (soma das parcelas de principal > subtotal)
    with pytest.raises(InvalidPaymentError):
        container.sales.confirm_quick_sale(
            actor=operator,
            idempotency_key="sale-excess-001",
            items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
            payments=[
                QuickSalePaymentInput(method="pix", amount="20.00"),
                QuickSalePaymentInput(method="card", amount="25.00"),
            ],
        )

    # Valor entregue em dinheiro menor que a parcela em dinheiro
    with pytest.raises(InvalidPaymentError):
        container.sales.confirm_quick_sale(
            actor=operator,
            idempotency_key="sale-cash-under-tendered",
            items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
            payments=[QuickSalePaymentInput(method="cash", amount="40.00", cash_tendered="30.00")],
        )


def test_card_fee_zero_percent_default_and_configured_simple_addition(container, admin, operator):
    """Exemplos da especificação (§5):
    - Taxa inicial: 0%.
    - Exemplo 1: Produtos R$ 20,00, taxa 3% no modo acréscimo simples ->
      Produtos: R$ 20,00, Taxa: R$ 0,60, Total cobrado: R$ 20,60.
    """
    p = container.products.create_product(admin, "Açaí 500ml", "20.00")

    # 1. Com taxa padrão de 0%:
    sale_zero, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-card-0pct",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="card", amount="20.00")],
    )
    assert sale_zero.subtotal == Decimal("20.00")
    assert sale_zero.card_fee_total == Decimal("0.00")
    assert sale_zero.total_charged == Decimal("20.00")
    assert sale_zero.payments[0].fee_percent == Decimal("0.00")
    assert sale_zero.payments[0].fee_amount == Decimal("0.00")
    assert sale_zero.payments[0].gross_amount == Decimal("20.00")

    # 2. Configura taxa administrativa de 3.00% (simple_addition)
    container.settings.update_settings(admin, card_fee_percent="3.00", card_fee_mode="simple_addition")

    sale_3pct, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-card-3pct",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="card", amount="20.00")],
    )
    assert sale_3pct.subtotal == Decimal("20.00")
    assert sale_3pct.card_fee_total == Decimal("0.60")
    assert sale_3pct.total_charged == Decimal("20.60")
    mov = sale_3pct.payments[0]
    assert mov.amount == Decimal("20.00")
    assert mov.fee_percent == Decimal("3.00")
    assert mov.fee_mode == "simple_addition"
    assert mov.fee_amount == Decimal("0.60")
    assert mov.gross_amount == Decimal("20.60")

    # 3. Altera taxa para 4.50% e verifica que as vendas anteriores preservaram 0% e 3%
    container.settings.update_settings(admin, card_fee_percent="4.50", card_fee_mode="simple_addition")
    assert container.sales.get_sale(sale_zero.id).payments[0].fee_percent == Decimal("0.00")
    assert container.sales.get_sale(sale_3pct.id).payments[0].fee_percent == Decimal("3.00")
    assert container.sales.get_sale(sale_3pct.id).total_charged == Decimal("20.60")


def test_split_payment_with_card_fee_applies_only_to_card_portion(container, admin, operator):
    """Exemplo exato da especificação (§6):
    Produtos: R$ 50,00.
    Cliente paga: R$ 20,00 em Pix e R$ 30,00 de principal no cartão.
    Taxa hipotética de 3% sobre a parcela do cartão (acréscimo simples).
    Resultado:
    - Produtos: R$ 50,00
    - Pix: R$ 20,00 (sem taxa)
    - Principal do cartão: R$ 30,00
    - Taxa do cartão: R$ 0,90
    - Total cobrado no cartão: R$ 30,90
    - Total recebido: R$ 50,90
    """
    p = container.products.create_product(admin, "Barca de Açaí", "50.00")
    container.settings.update_settings(admin, card_fee_percent="3.00", card_fee_mode="simple_addition")

    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-split-fee-50-20-30",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[
            QuickSalePaymentInput(method="pix", amount="20.00"),
            QuickSalePaymentInput(method="card", amount="30.00"),
        ],
    )
    assert sale.subtotal == Decimal("50.00")
    assert sale.card_fee_total == Decimal("0.90")
    assert sale.total_charged == Decimal("50.90")

    pix_mov, card_mov = sale.payments
    assert pix_mov.method == PaymentMethod.PIX
    assert pix_mov.amount == Decimal("20.00")
    assert pix_mov.fee_amount == Decimal("0.00")
    assert pix_mov.gross_amount == Decimal("20.00")

    assert card_mov.method == PaymentMethod.CARD
    assert card_mov.amount == Decimal("30.00")
    assert card_mov.fee_percent == Decimal("3.00")
    assert card_mov.fee_amount == Decimal("0.90")
    assert card_mov.gross_amount == Decimal("30.90")

    summary = container.sales.get_financial_summary()
    assert summary["active_sales_subtotal"] == "50.00"
    assert summary["active_card_fees"] == "0.90"
    assert summary["active_total_charged"] == "50.90"
    assert summary["by_method"]["pix"]["net_gross"] == "20.00"
    assert summary["by_method"]["card"]["net_principal"] == "30.00"
    assert summary["by_method"]["card"]["net_fee"] == "0.90"
    assert summary["by_method"]["card"]["net_gross"] == "30.90"


def test_monetary_rounding_and_pass_through_fee_calculation():
    """Valida arredondamento financeiro (ROUND_HALF_UP) com valores pré-calculados."""
    # 1. Acréscimo simples: R$ 10,00 com 2.99% -> 10 * 0.0299 = 0.299 -> R$ 0,30
    assert calculate_card_fee(Decimal("10.00"), Decimal("2.99"), CardFeeMode.SIMPLE_ADDITION) == Decimal("0.30")
    # R$ 15,00 com 2.50% -> 15 * 0.025 = 0.375 -> R$ 0,38 (HALF_UP)
    assert calculate_card_fee(Decimal("15.00"), Decimal("2.50"), CardFeeMode.SIMPLE_ADDITION) == Decimal("0.38")
    # R$ 13,00 com 3.15% -> 13 * 0.0315 = 0.4095 -> R$ 0,41
    assert calculate_card_fee(Decimal("13.00"), Decimal("3.15"), CardFeeMode.SIMPLE_ADDITION) == Decimal("0.41")

    # 2. Repasse por dentro (pass_through):
    # Principal R$ 97,00 com taxa de 3.00% descontada sobre o bruto:
    # Bruto = 97.00 / (1 - 0.03) = 97.00 / 0.97 = 100.00 -> Taxa = R$ 3,00
    assert calculate_card_fee(Decimal("97.00"), Decimal("3.00"), CardFeeMode.PASS_THROUGH) == Decimal("3.00")
    # Principal R$ 20,00 com taxa de 3.00% por dentro:
    # Bruto = 20.00 / 0.97 = 20.61855... -> R$ 20,62 -> Taxa = R$ 0,62
    assert calculate_card_fee(Decimal("20.00"), Decimal("3.00"), CardFeeMode.PASS_THROUGH) == Decimal("0.62")
