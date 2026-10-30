"""Testes de idempotência, concorrência e estornos."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from conftest import logged_in
from src.domain.errors import (
    IdempotencyConflictError,
    InvalidInputError,
    PermissionDeniedError,
    SaleAlreadyVoidedError,
    ShiftClosedError,
)
from src.domain.models import (
    CashDirection,
    CashMovementKind,
    CustomerRefundStatus,
    PaymentMethod,
    SaleStatus,
)
from src.use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput


def test_idempotency_replay_returns_original_sale_without_duplicating_records(client, admin, operator):
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 500ml", "price": "18.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    payload = {
        "idempotency_key": "idem-replay-key-001",
        "items": [{"product_id": p["id"], "quantity": 2}],
        "payments": [{"method": "pix", "amount": "36.00"}],
    }

    first = client.post("/api/sales/quick", json=payload, headers=op_headers)
    assert first.status_code == 201
    first_body = first.get_json()
    assert first_body["idempotent_replay"] is False
    sale_id = first_body["sale"]["id"]

    # Reenvio idêntico com a mesma chave retorna 200 e o mesmo ID, sem duplicar venda ou pagamento
    second = client.post("/api/sales/quick", json=payload, headers=op_headers)
    assert second.status_code == 200
    second_body = second.get_json()
    assert second_body["idempotent_replay"] is True
    assert second_body["sale"]["id"] == sale_id
    assert second_body["sale"]["total_charged"] == "36.00"

    sales_list = client.get("/api/sales").get_json()
    assert sales_list["summary"]["active_sales_count"] == 1
    assert sales_list["summary"]["active_sales_subtotal"] == "36.00"


def test_idempotency_key_reused_with_different_payload_is_rejected_with_409(client, admin, operator):
    admin_headers = logged_in(client, "admin")
    p = client.post(
        "/api/products",
        json={"name": "Açaí 300ml", "price": "12.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    first = client.post(
        "/api/sales/quick",
        json={
            "idempotency_key": "idem-conflict-key-001",
            "items": [{"product_id": p["id"], "quantity": 1}],
            "payments": [{"method": "pix", "amount": "12.00"}],
        },
        headers=op_headers,
    )
    assert first.status_code == 201

    # Mesma chave, mas quantidade ou forma de pagamento diferente -> 409 Conflict
    conflict = client.post(
        "/api/sales/quick",
        json={
            "idempotency_key": "idem-conflict-key-001",
            "items": [{"product_id": p["id"], "quantity": 2}],
            "payments": [{"method": "pix", "amount": "24.00"}],
        },
        headers=op_headers,
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"


def test_simultaneous_confirmations_with_same_idempotency_key_create_only_one_sale(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 500ml", "20.00")

    def attempt():
        return container.sales.confirm_quick_sale(
            actor=operator,
            idempotency_key="concurrent-idem-key-001",
            items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
            payments=[QuickSalePaymentInput(method="pix", amount="20.00")],
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(attempt) for _ in range(5)]
        results = [f.result() for f in futures]

    sale_ids = {sale.id for sale, _ in results}
    created_flags = [created for _, created in results]

    # Exatamente uma chamada criou o registro; todas retornaram a mesma venda
    assert len(sale_ids) == 1
    assert created_flags.count(True) == 1
    assert created_flags.count(False) == 4

    summary = container.sales.get_financial_summary()
    assert summary["active_sales_count"] == 1
    assert summary["active_sales_subtotal"] == "20.00"


def test_void_requires_admin_permission_and_mandatory_reason(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 300ml", "14.00")
    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-to-void-perm-001",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="pix", amount="14.00")],
    )

    # Operador comum não pode estornar
    with pytest.raises(PermissionDeniedError):
        container.sales.void_quick_sale(actor=operator, sale_id=sale.id, reason="Cliente desistiu")

    # Motivo vazio ou curto demais é rejeitado
    for bad_reason in ("", "  ", "ab"):
        with pytest.raises(InvalidInputError):
            container.sales.void_quick_sale(actor=admin, sale_id=sale.id, reason=bad_reason)


def test_void_sale_without_immediate_customer_refund_distinguishes_accounting_reversal_from_refund(
    container, admin, operator
):
    """Valida a distinção explícita exigida na §10:
    - Cancelamento contábil da venda (`status = 'voided'`)
    - Reversão do recebimento (`cash_movements` com `kind = 'reversal'`, `direction = 'out'`)
    - Devolução efetiva ao cliente (`customer_refund_status = 'pending'` até confirmação explícita)
    - Preservação integral dos registros originais (nenhum DELETE em `sales`, `sale_items` ou `cash_movements`)
    """
    p = container.products.create_product(admin, "Açaí 500ml", "30.00")
    container.settings.update_settings(admin, card_fee_percent="3.00", card_fee_mode="simple_addition")

    # Venda dividida: R$ 10,00 Pix + R$ 20,00 Cartão (+ R$ 0,60 taxa = R$ 20,60) -> Total cobrado R$ 30,60
    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-void-distinction-001",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[
            QuickSalePaymentInput(method="pix", amount="10.00"),
            QuickSalePaymentInput(method="card", amount="20.00"),
        ],
    )

    # Estorna sem marcar devolução financeira efetiva ao cliente (padrão seguro: customer_refund_completed=False)
    voided = container.sales.void_quick_sale(
        actor=admin,
        sale_id=sale.id,
        reason="Erro de digitação no pedido",
        customer_refund_completed=False,
    )

    assert voided.status == SaleStatus.VOIDED
    assert voided.voided_by == admin.id
    assert voided.void_reason == "Erro de digitação no pedido"
    assert voided.voided_at is not None
    # O sistema NÃO registrou falsamente que o dinheiro já foi devolvido ao cliente:
    assert voided.customer_refund_status == CustomerRefundStatus.PENDING
    assert voided.customer_refunded_at is None
    assert voided.customer_refunded_by is None

    # E NÃO registrou nenhuma saída de caixa (direction='out') antes da devolução efetiva!
    assert len(voided.items) == 1
    assert voided.items[0].line_total == Decimal("30.00")
    assert len(voided.payments) == 2
    originals = [m for m in voided.payments if m.kind == CashMovementKind.SALE_PAYMENT]
    reversals = [m for m in voided.payments if m.kind == CashMovementKind.REVERSAL]
    assert len(originals) == 2
    assert len(reversals) == 0

    # Relatório financeiro distingue:
    # - Recebimento contábil reconhecido (recognized_gross) = 0.00
    # - Saldo financeiro ainda em posse do caixa (net_gross) = 10.00 Pix e 20.60 Cartão
    # - Obrigação de devolução pendente ao cliente (pending_customer_refunds_total) = 30.60
    summary = container.sales.get_financial_summary()
    assert summary["active_sales_count"] == 0
    assert summary["voided_sales_count"] == 1
    assert summary["pending_customer_refunds_count"] == 1
    assert summary["active_sales_subtotal"] == "0.00"
    assert summary["active_card_fees"] == "0.00"
    assert summary["active_total_charged"] == "0.00"
    assert summary["voided_total_charged"] == "30.60"
    assert summary["pending_customer_refunds_total"] == "30.60"
    assert summary["completed_customer_refunds_total"] == "0.00"
    assert summary["by_method"]["pix"]["recognized_gross"] == "0.00"
    assert summary["by_method"]["card"]["recognized_gross"] == "0.00"
    assert summary["by_method"]["pix"]["net_gross"] == "10.00"
    assert summary["by_method"]["card"]["net_gross"] == "20.60"

    # Quando a devolução efetiva ao cliente ocorre, o administrador a confirma explicitamente:
    refunded = container.sales.confirm_customer_refund(
        actor=admin,
        sale_id=sale.id,
        method=PaymentMethod.PIX,
        notes="Devolvido via Pix ao cliente",
    )
    assert refunded.customer_refund_status == CustomerRefundStatus.COMPLETED
    assert refunded.customer_refunded_by == admin.id
    assert refunded.customer_refunded_at is not None
    assert refunded.customer_refund_method == PaymentMethod.PIX
    assert refunded.customer_refund_notes == "Devolvido via Pix ao cliente"

    # Agora sim os movimentos de saída (reversal, direction='out') foram gravados no livro-razão!
    assert len(refunded.payments) == 4
    reversals_after = [m for m in refunded.payments if m.kind == CashMovementKind.REVERSAL]
    assert len(reversals_after) == 2
    assert all(r.direction == CashDirection.OUT for r in reversals_after)
    assert {r.reverses_id for r in reversals_after} == {o.id for o in originals}

    summary_after = container.sales.get_financial_summary()
    assert summary_after["pending_customer_refunds_count"] == 0
    assert summary_after["pending_customer_refunds_total"] == "0.00"
    assert summary_after["completed_customer_refunds_total"] == "30.60"
    assert summary_after["by_method"]["pix"]["net_gross"] == "0.00"
    assert summary_after["by_method"]["card"]["net_gross"] == "0.00"

    # Não permite estornar novamente a mesma venda
    with pytest.raises(SaleAlreadyVoidedError):
        container.sales.void_quick_sale(actor=admin, sale_id=sale.id, reason="Tentativa duplicada")


def test_void_sale_with_immediate_customer_refund_records_outflow_immediately(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 300ml", "15.00")
    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-void-immediate-refund-001",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="15.00", cash_tendered="20.00")],
    )

    voided = container.sales.void_quick_sale(
        actor=admin,
        sale_id=sale.id,
        reason="Cancelado e devolvido no balcão",
        customer_refund_completed=True,
        customer_refund_method="cash",
    )
    assert voided.status == SaleStatus.VOIDED
    assert voided.customer_refund_status == CustomerRefundStatus.COMPLETED
    assert len(voided.payments) == 2
    assert voided.payments[1].kind == CashMovementKind.REVERSAL
    assert voided.payments[1].direction == CashDirection.OUT
    assert voided.payments[1].reverses_id == voided.payments[0].id

    summary = container.sales.get_financial_summary()
    assert summary["pending_customer_refunds_count"] == 0
    assert summary["completed_customer_refunds_total"] == "15.00"
    assert summary["by_method"]["cash"]["recognized_gross"] == "0.00"
    assert summary["by_method"]["cash"]["net_gross"] == "0.00"


def test_cannot_void_sale_from_closed_shift(container, admin, operator, clock):
    p = container.products.create_product(admin, "Açaí 300ml", "15.00")
    sale, _ = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-closed-shift-001",
        items=[QuickSaleItemInput(product_id=p.id, quantity=1)],
        payments=[QuickSalePaymentInput(method="cash", amount="15.00", cash_tendered="20.00")],
    )

    # Fecha o turno da venda
    from src.adapters.db.database import MariaDbUnitOfWork
    with MariaDbUnitOfWork(container.db) as uow:
        uow.shifts.close_shift(
            shift_id=sale.shift_id,
            closed_by=admin.id,
            now=clock.now(),
            counted_cash=Decimal("15.00"),
            notes="Turno encerrado",
        )

    # Tentativa de estornar venda de turno fechado é bloqueada com ShiftClosedError (409)
    with pytest.raises(ShiftClosedError):
        container.sales.void_quick_sale(
            actor=admin,
            sale_id=sale.id,
            reason="Tentativa após fechar o turno",
        )
