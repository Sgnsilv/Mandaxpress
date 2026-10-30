"""Testes de gerenciamento de produtos (cadastro, edição, status, preço, histórico)."""
from decimal import Decimal

import pymysql
import pytest

from conftest import logged_in
from src.domain.errors import (
    InvalidInputError,
    PermissionDeniedError,
    ProductAlreadyExistsError,
    ProductNotFoundError,
)
from src.use_cases.sales import QuickSaleItemInput, QuickSalePaymentInput


def test_admin_can_create_and_list_products_via_api(client, admin):
    headers = logged_in(client, "admin")
    resp = client.post(
        "/api/products",
        json={"name": "Açaí 500ml", "category": "Açaí", "price": "18.50"},
        headers=headers,
    )
    assert resp.status_code == 201
    prod = resp.get_json()["product"]
    assert prod["name"] == "Açaí 500ml"
    assert prod["category"] == "Açaí"
    assert prod["price"] == "18.50"
    assert prod["is_active"] is True

    list_resp = client.get("/api/products")
    assert list_resp.status_code == 200
    items = list_resp.get_json()["products"]
    assert len(items) == 1
    assert items[0]["id"] == prod["id"]


def test_operator_cannot_create_edit_or_deactivate_products(client, admin, operator):
    admin_headers = logged_in(client, "admin")
    prod = client.post(
        "/api/products",
        json={"name": "Água Mineral", "category": "Bebidas", "price": "5.00"},
        headers=admin_headers,
    ).get_json()["product"]

    op_headers = logged_in(client, "caixa")
    assert client.post(
        "/api/products",
        json={"name": "Suco", "category": "Bebidas", "price": "8.00"},
        headers=op_headers,
    ).status_code == 403
    assert client.put(
        f"/api/products/{prod['id']}",
        json={"name": "Água Alterada", "category": "Bebidas", "price": "6.00"},
        headers=op_headers,
    ).status_code == 403
    assert client.patch(
        f"/api/products/{prod['id']}/status",
        json={"is_active": False},
        headers=op_headers,
    ).status_code == 403

    # Mas operador pode listar produtos ativos para a tela de vendas
    list_resp = client.get("/api/products?active_only=true")
    assert list_resp.status_code == 200
    assert len(list_resp.get_json()["products"]) == 1


def test_product_edit_name_price_and_category(client, admin):
    headers = logged_in(client, "admin")
    created = client.post(
        "/api/products",
        json={"name": "Açaí 300ml", "category": "Copos", "price": "12.00"},
        headers=headers,
    ).get_json()["product"]

    updated_resp = client.put(
        f"/api/products/{created['id']}",
        json={"name": "Açaí Tradicional 300ml", "category": "Açaí", "price": "14.50"},
        headers=headers,
    )
    assert updated_resp.status_code == 200
    updated = updated_resp.get_json()["product"]
    assert updated["name"] == "Açaí Tradicional 300ml"
    assert updated["category"] == "Açaí"
    assert updated["price"] == "14.50"


def test_product_activate_and_deactivate_filters_active_only(client, admin):
    headers = logged_in(client, "admin")
    p1 = client.post(
        "/api/products",
        json={"name": "Açaí 300ml", "category": "Açaí", "price": "12.00"},
        headers=headers,
    ).get_json()["product"]
    p2 = client.post(
        "/api/products",
        json={"name": "Creme de Cupuaçu", "category": "Cremes", "price": "15.00"},
        headers=headers,
    ).get_json()["product"]

    # Desativa p2
    deact = client.patch(
        f"/api/products/{p2['id']}/status",
        json={"is_active": False},
        headers=headers,
    )
    assert deact.status_code == 200
    assert deact.get_json()["product"]["is_active"] is False

    # Na tela de vendas (active_only=true) só aparece p1
    active_list = client.get("/api/products?active_only=true").get_json()["products"]
    assert [p["id"] for p in active_list] == [p1["id"]]

    # Na tela administrativa (active_only=false) aparecem ambos
    all_list = client.get("/api/products").get_json()["products"]
    assert len(all_list) == 2

    # Reativa p2
    react = client.patch(
        f"/api/products/{p2['id']}/status",
        json={"is_active": True},
        headers=headers,
    )
    assert react.status_code == 200
    assert react.get_json()["product"]["is_active"] is True
    assert len(client.get("/api/products?active_only=true").get_json()["products"]) == 2


def test_product_search_by_name(client, admin):
    headers = logged_in(client, "admin")
    for name, price in [("Açaí 300ml", "12.00"), ("Açaí 500ml", "18.00"), ("Tapioca de Queijo", "10.00")]:
        client.post("/api/products", json={"name": name, "price": price}, headers=headers)

    found = client.get("/api/products?q=Açaí").get_json()["products"]
    assert len(found) == 2
    assert {p["name"] for p in found} == {"Açaí 300ml", "Açaí 500ml"}


def test_product_invalid_prices_are_rejected(container, admin):
    for bad_price in ("-0.01", "-10.00", "abc", "", "12.345", 12.5, "1000000.00"):
        with pytest.raises(InvalidInputError):
            container.products.create_product(admin, "Produto Inválido", bad_price)


def test_product_duplicate_name_and_not_found(container, admin, operator):
    p = container.products.create_product(admin, "Açaí 700ml", "22.00")
    with pytest.raises(ProductAlreadyExistsError):
        container.products.create_product(admin, "Açaí 700ml", "25.00")
    with pytest.raises(ProductNotFoundError):
        container.products.update_product(admin, p.id + 999, "Outro", "10.00")
    with pytest.raises(PermissionDeniedError):
        container.products.create_product(operator, "Proibido", "10.00")


def test_price_change_and_deactivation_preserve_past_sales_and_forbid_physical_delete(container, admin, operator):
    prod = container.products.create_product(admin, "Açaí Especial", "20.00", category="Açaí")

    sale, created = container.sales.confirm_quick_sale(
        actor=operator,
        idempotency_key="sale-preserve-price-001",
        items=[QuickSaleItemInput(product_id=prod.id, quantity=2)],
        payments=[QuickSalePaymentInput(method="pix", amount="40.00")],
    )
    assert created is True
    assert sale.subtotal == Decimal("40.00")
    assert sale.items[0].unit_price == Decimal("20.00")
    assert sale.items[0].line_total == Decimal("40.00")
    assert sale.items[0].description == "Açaí Especial"

    # Altera preço e nome e desativa o produto posteriormente
    container.products.update_product(admin, prod.id, "Açaí Especial Novo Nome", "27.50", category="Premium")
    container.products.set_product_active(admin, prod.id, False)

    # A venda histórica permanece intacta com o preço e nome do momento da venda
    reloaded = container.sales.get_sale(sale.id)
    assert reloaded.subtotal == Decimal("40.00")
    assert reloaded.total_charged == Decimal("40.00")
    assert reloaded.items[0].product_id == prod.id
    assert reloaded.items[0].description == "Açaí Especial"
    assert reloaded.items[0].unit_price == Decimal("20.00")
    assert reloaded.items[0].line_total == Decimal("40.00")

    # E o banco bloqueia exclusão física de produto com histórico em sale_items (FK ON DELETE RESTRICT)
    conn = container.db.connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            with pytest.raises(pymysql.err.IntegrityError):
                cur.execute("DELETE FROM products WHERE id = %s", (prod.id,))
    finally:
        conn.close()
