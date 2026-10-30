"""Repositórios SQL (consultas sempre parametrizadas)."""
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

import pymysql

from ...domain.errors import (
    ProductAlreadyExistsError,
    ProductNotFoundError,
    UserAlreadyExistsError,
)
from ...domain.models import (
    AuditLogEntry,
    CardFeeMode,
    CashDirection,
    CashMovement,
    CashMovementKind,
    CustomerRefundStatus,
    PaymentMethod,
    PaymentSettings,
    Product,
    Role,
    Sale,
    SaleItem,
    SaleItemKind,
    SaleKind,
    SaleStatus,
    SessionRecord,
    Shift,
    ShiftStatus,
    User,
)
from ...domain.ports import (
    AuditLogRepository,
    CashMovementRepository,
    PaymentSettingsRepository,
    ProductRepository,
    SaleRepository,
    SessionRepository,
    ShiftRepository,
    UserRepository,
)

_DUPLICATE_ENTRY = 1062


def _row_to_user(row: dict) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=Role(row["role"]),
        is_active=bool(row["is_active"]),
    )


def _row_to_product(row: dict) -> Product:
    return Product(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        price=Decimal(str(row["price"])),
        is_active=bool(row["is_active"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_shift(row: dict) -> Shift:
    return Shift(
        id=row["id"],
        status=ShiftStatus(row["status"]),
        opened_by=row["opened_by"],
        opened_at=row["opened_at"],
        opening_cash=Decimal(str(row["opening_cash"])),
        closed_by=row.get("closed_by"),
        closed_at=row.get("closed_at"),
        counted_cash=Decimal(str(row["counted_cash"])) if row.get("counted_cash") is not None else None,
        notes=row.get("notes"),
    )


def _row_to_sale_item(row: dict) -> SaleItem:
    return SaleItem(
        id=row["id"],
        sale_id=row["sale_id"],
        product_id=row["product_id"],
        item_kind=SaleItemKind(row["item_kind"]),
        description=row["description"],
        quantity=int(row["quantity"]),
        unit_price=Decimal(str(row["unit_price"])),
        line_total=Decimal(str(row["line_total"])),
    )


def _row_to_cash_movement(row: dict) -> CashMovement:
    return CashMovement(
        id=row["id"],
        shift_id=row["shift_id"],
        tab_id=row.get("tab_id"),
        sale_id=row.get("sale_id"),
        kind=CashMovementKind(row["kind"]),
        method=PaymentMethod(row["method"]),
        direction=CashDirection(row["direction"]),
        amount=Decimal(str(row["amount"])),
        fee_percent=Decimal(str(row["fee_percent"])),
        fee_mode=row["fee_mode"],
        fee_amount=Decimal(str(row["fee_amount"])),
        gross_amount=Decimal(str(row["gross_amount"])),
        cash_tendered=Decimal(str(row["cash_tendered"])) if row.get("cash_tendered") is not None else None,
        change_given=Decimal(str(row["change_given"])) if row.get("change_given") is not None else None,
        reverses_id=row.get("reverses_id"),
        operator_id=row["operator_id"],
        created_at=row["created_at"],
        idempotency_key=row["idempotency_key"],
        note=row.get("note"),
    )


def _row_to_sale(row: dict, items: List[SaleItem], payments: List[CashMovement]) -> Sale:
    return Sale(
        id=row["id"],
        shift_id=row["shift_id"],
        tab_id=row.get("tab_id"),
        kind=SaleKind(row["kind"]),
        status=SaleStatus(row["status"]),
        subtotal=Decimal(str(row["subtotal"])),
        card_fee_total=Decimal(str(row["card_fee_total"])),
        total_charged=Decimal(str(row["total_charged"])),
        operator_id=row["operator_id"],
        created_at=row["created_at"],
        idempotency_key=row["idempotency_key"],
        request_fingerprint=row["request_fingerprint"],
        voided_by=row.get("voided_by"),
        voided_at=row.get("voided_at"),
        void_reason=row.get("void_reason"),
        customer_refund_status=CustomerRefundStatus(row["customer_refund_status"]),
        customer_refunded_at=row.get("customer_refunded_at"),
        customer_refunded_by=row.get("customer_refunded_by"),
        customer_refund_method=PaymentMethod(row["customer_refund_method"]) if row.get("customer_refund_method") else None,
        customer_refund_notes=row.get("customer_refund_notes"),
        items=items,
        payments=payments,
    )


class MariaDbUserRepository(UserRepository):
    def __init__(self, conn):
        self._conn = conn

    def get_by_username(self, username: str) -> Optional[User]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, username, password_hash, role, is_active "
                "FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
        return _row_to_user(row) if row else None

    def add(self, name: str, username: str, password_hash: str, role: Role) -> User:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (name, username, password_hash, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (name, username, password_hash, role.value),
                )
                new_id = cur.lastrowid
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                raise UserAlreadyExistsError("Já existe um usuário com esse nome de usuário.") from exc
            raise
        return User(id=new_id, name=name, username=username, role=role,
                    is_active=True, password_hash=password_hash)

    def update_password(self, user_id: int, password_hash: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                        (password_hash, user_id))


class MariaDbSessionRepository(SessionRepository):
    def __init__(self, conn):
        self._conn = conn

    def add(self, token_hash, csrf_token, user_id, now, expires_at, user_agent) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (token_hash, csrf_token, user_id, created_at, "
                "last_seen_at, expires_at, user_agent) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (token_hash, csrf_token, user_id, now, now, expires_at, user_agent),
            )

    def find_by_token_hash(self, token_hash: str) -> Optional[Tuple[SessionRecord, User]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT s.id AS session_id, s.csrf_token, s.user_id, s.created_at, "
                "s.last_seen_at, s.expires_at, s.revoked_at, "
                "u.id, u.name, u.username, u.password_hash, u.role, u.is_active "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s",
                (token_hash,),
            )
            row = cur.fetchone()
        if not row:
            return None
        session = SessionRecord(
            id=row["session_id"],
            user_id=row["user_id"],
            csrf_token=row["csrf_token"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )
        return session, _row_to_user(row)

    def touch(self, session_id: int, now: datetime) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UPDATE sessions SET last_seen_at = %s WHERE id = %s", (now, session_id))

    def revoke(self, token_hash: str, now: datetime) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
                (now, token_hash),
            )

    def revoke_all_for_user(self, user_id: int, now: datetime) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
                (now, user_id),
            )

    def delete_expired(self, before: datetime) -> int:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < %s", (before,))
            return cur.rowcount


class MariaDbProductRepository(ProductRepository):
    def __init__(self, conn):
        self._conn = conn

    def add(self, name: str, category: str, price: Decimal, is_active: bool = True) -> Product:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO products (name, category, price, is_active) VALUES (%s, %s, %s, %s)",
                    (name, category, str(price), 1 if is_active else 0),
                )
                product_id = cur.lastrowid
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                raise ProductAlreadyExistsError("Já existe um produto cadastrado com este nome.") from exc
            raise
        product = self.get_by_id(product_id)
        assert product is not None
        return product

    def get_by_id(self, product_id: int, for_update: bool = False) -> Optional[Product]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, category, price, is_active, created_at, updated_at "
                f"FROM products WHERE id = %s{lock_sql}",
                (product_id,),
            )
            row = cur.fetchone()
        return _row_to_product(row) if row else None

    def get_by_ids(self, product_ids: Sequence[int], for_update: bool = False) -> Dict[int, Product]:
        unique_ids = sorted(set(product_ids))
        if not unique_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(unique_ids))
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, category, price, is_active, created_at, updated_at "
                f"FROM products WHERE id IN ({placeholders}) ORDER BY id ASC{lock_sql}",
                tuple(unique_ids),
            )
            rows = cur.fetchall()
        return {r["id"]: _row_to_product(r) for r in rows}

    def list_products(self, active_only: bool = False, search: Optional[str] = None,
                      category: Optional[str] = None) -> List[Product]:
        clauses = []
        params = []
        if active_only:
            clauses.append("is_active = 1")
        if search:
            clauses.append("name LIKE %s")
            params.append(f"%{search}%")
        if category:
            clauses.append("category = %s")
            params.append(category)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, category, price, is_active, created_at, updated_at "
                f"FROM products {where} ORDER BY is_active DESC, category ASC, name ASC",
                tuple(params),
            )
            rows = cur.fetchall()
        return [_row_to_product(r) for r in rows]

    def update(self, product_id: int, name: str, category: str, price: Decimal) -> Product:
        existing = self.get_by_id(product_id, for_update=True)
        if existing is None:
            raise ProductNotFoundError("Produto não encontrado.")
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE products SET name = %s, category = %s, price = %s WHERE id = %s",
                    (name, category, str(price), product_id),
                )
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                raise ProductAlreadyExistsError("Já existe um produto cadastrado com este nome.") from exc
            raise
        updated = self.get_by_id(product_id)
        assert updated is not None
        return updated

    def set_active(self, product_id: int, is_active: bool) -> Product:
        existing = self.get_by_id(product_id, for_update=True)
        if existing is None:
            raise ProductNotFoundError("Produto não encontrado.")
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE products SET is_active = %s WHERE id = %s",
                (1 if is_active else 0, product_id),
            )
        updated = self.get_by_id(product_id)
        assert updated is not None
        return updated


class MariaDbPaymentSettingsRepository(PaymentSettingsRepository):
    def __init__(self, conn):
        self._conn = conn

    def get(self, for_update: bool = False) -> PaymentSettings:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT card_fee_percent, card_fee_mode, updated_by, updated_at "
                f"FROM payment_settings WHERE id = 1{lock_sql}"
            )
            row = cur.fetchone()
        if not row:
            return PaymentSettings(
                card_fee_percent=Decimal("0.00"),
                card_fee_mode=CardFeeMode.SIMPLE_ADDITION,
            )
        return PaymentSettings(
            card_fee_percent=Decimal(str(row["card_fee_percent"])),
            card_fee_mode=CardFeeMode(row["card_fee_mode"]),
            updated_by=row.get("updated_by"),
            updated_at=row.get("updated_at"),
        )

    def update(self, card_fee_percent: Decimal, card_fee_mode: CardFeeMode,
               updated_by: int, now: datetime) -> PaymentSettings:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE payment_settings "
                "SET card_fee_percent = %s, card_fee_mode = %s, updated_by = %s, updated_at = %s "
                "WHERE id = 1",
                (str(card_fee_percent), card_fee_mode.value, updated_by, now),
            )
        return self.get()


class MariaDbShiftRepository(ShiftRepository):
    def __init__(self, conn):
        self._conn = conn

    def get_open_shift(self, for_update: bool = False) -> Optional[Shift]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, opened_by, opened_at, opening_cash, "
                "closed_by, closed_at, counted_cash, notes "
                f"FROM shifts WHERE status = 'open' ORDER BY id DESC LIMIT 1{lock_sql}"
            )
            row = cur.fetchone()
        return _row_to_shift(row) if row else None

    def get_by_id(self, shift_id: int, for_update: bool = False) -> Optional[Shift]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, opened_by, opened_at, opening_cash, "
                "closed_by, closed_at, counted_cash, notes "
                f"FROM shifts WHERE id = %s{lock_sql}",
                (shift_id,),
            )
            row = cur.fetchone()
        return _row_to_shift(row) if row else None

    def create_open_shift(self, opened_by: int, now: datetime,
                          opening_cash: Decimal = Decimal("0.00")) -> Shift:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shifts (status, opened_by, opened_at, opening_cash) "
                "VALUES ('open', %s, %s, %s)",
                (opened_by, now, str(opening_cash)),
            )
            shift_id = cur.lastrowid
        shift = self.get_by_id(shift_id)
        assert shift is not None
        return shift

    def close_shift(self, shift_id: int, closed_by: int, now: datetime,
                    counted_cash: Optional[Decimal] = None,
                    notes: Optional[str] = None) -> Shift:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE shifts SET status = 'closed', closed_by = %s, closed_at = %s, "
                "counted_cash = %s, notes = %s WHERE id = %s",
                (closed_by, now, str(counted_cash) if counted_cash is not None else None, notes, shift_id),
            )
        shift = self.get_by_id(shift_id)
        assert shift is not None
        return shift


class MariaDbSaleRepository(SaleRepository):
    def __init__(self, conn):
        self._conn = conn

    def _load_details(self, sale_id: int) -> Tuple[List[SaleItem], List[CashMovement]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, sale_id, product_id, item_kind, description, quantity, unit_price, line_total "
                "FROM sale_items WHERE sale_id = %s ORDER BY id ASC",
                (sale_id,),
            )
            items = [_row_to_sale_item(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT id, shift_id, tab_id, sale_id, kind, method, direction, amount, "
                "fee_percent, fee_mode, fee_amount, gross_amount, cash_tendered, change_given, "
                "reverses_id, operator_id, created_at, idempotency_key, note "
                "FROM cash_movements WHERE sale_id = %s ORDER BY id ASC",
                (sale_id,),
            )
            payments = [_row_to_cash_movement(r) for r in cur.fetchall()]
        return items, payments

    def get_by_idempotency_key(self, idempotency_key: str, for_update: bool = False) -> Optional[Sale]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sales WHERE idempotency_key = %s" + lock_sql,
                (idempotency_key,),
            )
            row = cur.fetchone()
        if not row:
            return None
        items, payments = self._load_details(row["id"])
        return _row_to_sale(row, items, payments)

    def get_by_id(self, sale_id: int, for_update: bool = False) -> Optional[Sale]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sales WHERE id = %s" + lock_sql,
                (sale_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        items, payments = self._load_details(row["id"])
        return _row_to_sale(row, items, payments)

    def list_recent(self, limit: int = 50, shift_id: Optional[int] = None) -> List[Sale]:
        where = "WHERE shift_id = %s" if shift_id is not None else ""
        params = (shift_id, limit) if shift_id is not None else (limit,)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM sales {where} ORDER BY id DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
        result = []
        for row in rows:
            items, payments = self._load_details(row["id"])
            result.append(_row_to_sale(row, items, payments))
        return result

    def create_sale(
        self,
        shift_id: int,
        kind: SaleKind,
        subtotal: Decimal,
        card_fee_total: Decimal,
        total_charged: Decimal,
        operator_id: int,
        created_at: datetime,
        idempotency_key: str,
        request_fingerprint: str,
        tab_id: Optional[int] = None,
    ) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sales (shift_id, tab_id, kind, status, subtotal, card_fee_total, "
                "total_charged, operator_id, created_at, idempotency_key, request_fingerprint) "
                "VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)",
                (
                    shift_id,
                    tab_id,
                    kind.value,
                    str(subtotal),
                    str(card_fee_total),
                    str(total_charged),
                    operator_id,
                    created_at,
                    idempotency_key,
                    request_fingerprint,
                ),
            )
            return cur.lastrowid

    def add_item(
        self,
        sale_id: int,
        product_id: Optional[int],
        item_kind: SaleItemKind,
        description: str,
        quantity: int,
        unit_price: Decimal,
        line_total: Decimal,
    ) -> SaleItem:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sale_items (sale_id, product_id, item_kind, description, "
                "quantity, unit_price, line_total) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    sale_id,
                    product_id,
                    item_kind.value,
                    description,
                    quantity,
                    str(unit_price),
                    str(line_total),
                ),
            )
            item_id = cur.lastrowid
        return SaleItem(
            id=item_id,
            sale_id=sale_id,
            product_id=product_id,
            item_kind=item_kind,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            line_total=line_total,
        )

    def mark_voided(self, sale_id: int, voided_by: int, voided_at: datetime,
                    reason: str, customer_refund_completed: bool = False,
                    customer_refund_method: Optional[PaymentMethod] = None,
                    customer_refund_notes: Optional[str] = None) -> None:
        refund_status = (
            CustomerRefundStatus.COMPLETED if customer_refund_completed else CustomerRefundStatus.PENDING
        )
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE sales SET status = 'voided', voided_by = %s, voided_at = %s, void_reason = %s, "
                "customer_refund_status = %s, customer_refunded_at = %s, customer_refunded_by = %s, "
                "customer_refund_method = %s, customer_refund_notes = %s WHERE id = %s",
                (
                    voided_by,
                    voided_at,
                    reason,
                    refund_status.value,
                    voided_at if customer_refund_completed else None,
                    voided_by if customer_refund_completed else None,
                    customer_refund_method.value if (customer_refund_completed and customer_refund_method) else None,
                    customer_refund_notes if customer_refund_completed else None,
                    sale_id,
                ),
            )

    def mark_customer_refunded(self, sale_id: int, refunded_by: int, refunded_at: datetime,
                               method: PaymentMethod, notes: Optional[str] = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE sales SET customer_refund_status = 'completed', "
                "customer_refunded_by = %s, customer_refunded_at = %s, "
                "customer_refund_method = %s, customer_refund_notes = %s WHERE id = %s",
                (refunded_by, refunded_at, method.value, notes, sale_id),
            )


class MariaDbCashMovementRepository(CashMovementRepository):
    def __init__(self, conn):
        self._conn = conn

    def add(
        self,
        shift_id: int,
        sale_id: Optional[int],
        kind: CashMovementKind,
        method: PaymentMethod,
        direction: CashDirection,
        amount: Decimal,
        fee_percent: Decimal,
        fee_mode: str,
        fee_amount: Decimal,
        gross_amount: Decimal,
        cash_tendered: Optional[Decimal],
        change_given: Optional[Decimal],
        reverses_id: Optional[int],
        operator_id: int,
        created_at: datetime,
        idempotency_key: str,
        note: Optional[str] = None,
        tab_id: Optional[int] = None,
    ) -> CashMovement:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cash_movements (shift_id, tab_id, sale_id, kind, method, direction, "
                "amount, fee_percent, fee_mode, fee_amount, gross_amount, cash_tendered, change_given, "
                "reverses_id, operator_id, created_at, idempotency_key, note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    shift_id,
                    tab_id,
                    sale_id,
                    kind.value,
                    method.value,
                    direction.value,
                    str(amount),
                    str(fee_percent),
                    fee_mode,
                    str(fee_amount),
                    str(gross_amount),
                    str(cash_tendered) if cash_tendered is not None else None,
                    str(change_given) if change_given is not None else None,
                    reverses_id,
                    operator_id,
                    created_at,
                    idempotency_key,
                    note,
                ),
            )
            mov_id = cur.lastrowid
        return CashMovement(
            id=mov_id,
            shift_id=shift_id,
            tab_id=tab_id,
            sale_id=sale_id,
            kind=kind,
            method=method,
            direction=direction,
            amount=amount,
            fee_percent=fee_percent,
            fee_mode=fee_mode,
            fee_amount=fee_amount,
            gross_amount=gross_amount,
            cash_tendered=cash_tendered,
            change_given=change_given,
            reverses_id=reverses_id,
            operator_id=operator_id,
            created_at=created_at,
            idempotency_key=idempotency_key,
            note=note,
        )

    def list_by_sale(self, sale_id: int, for_update: bool = False) -> List[CashMovement]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cash_movements WHERE sale_id = %s ORDER BY id ASC" + lock_sql,
                (sale_id,),
            )
            rows = cur.fetchall()
        return [_row_to_cash_movement(r) for r in rows]

    def get_financial_summary(self, shift_id: Optional[int] = None) -> dict:
        where_sales = "WHERE shift_id = %s" if shift_id is not None else ""
        params_sales = (shift_id,) if shift_id is not None else ()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT "
                "COUNT(CASE WHEN status = 'active' THEN 1 END) AS active_sales_count, "
                "COUNT(CASE WHEN status = 'voided' THEN 1 END) AS voided_sales_count, "
                "COUNT(CASE WHEN status = 'voided' AND customer_refund_status = 'pending' THEN 1 END) AS pending_customer_refunds_count, "
                "COALESCE(SUM(CASE WHEN status = 'active' THEN subtotal ELSE 0 END), 0.00) AS active_sales_subtotal, "
                "COALESCE(SUM(CASE WHEN status = 'active' THEN card_fee_total ELSE 0 END), 0.00) AS active_card_fees, "
                "COALESCE(SUM(CASE WHEN status = 'active' THEN total_charged ELSE 0 END), 0.00) AS active_total_charged, "
                "COALESCE(SUM(CASE WHEN status = 'voided' THEN total_charged ELSE 0 END), 0.00) AS voided_total_charged, "
                "COALESCE(SUM(CASE WHEN status = 'voided' AND customer_refund_status = 'pending' THEN total_charged ELSE 0 END), 0.00) AS pending_customer_refunds_total, "
                "COALESCE(SUM(CASE WHEN status = 'voided' AND customer_refund_status = 'completed' THEN total_charged ELSE 0 END), 0.00) AS completed_customer_refunds_total "
                f"FROM sales {where_sales}",
                params_sales,
            )
            s_row = cur.fetchone()

            where_mov = "WHERE m.shift_id = %s" if shift_id is not None else ""
            cur.execute(
                "SELECT m.method, "
                "COALESCE(SUM(CASE "
                "  WHEN m.direction = 'in' AND (s.id IS NULL OR s.status = 'active') THEN m.amount "
                "  ELSE 0 "
                "END), 0.00) AS recognized_principal, "
                "COALESCE(SUM(CASE "
                "  WHEN m.direction = 'in' AND (s.id IS NULL OR s.status = 'active') THEN m.fee_amount "
                "  ELSE 0 "
                "END), 0.00) AS recognized_fee, "
                "COALESCE(SUM(CASE "
                "  WHEN m.direction = 'in' AND (s.id IS NULL OR s.status = 'active') THEN m.gross_amount "
                "  ELSE 0 "
                "END), 0.00) AS recognized_gross, "
                "COALESCE(SUM(CASE "
                "  WHEN m.direction = 'in' THEN m.amount "
                "  ELSE -m.amount "
                "END), 0.00) AS net_principal, "
                "COALESCE(SUM(CASE "
                "  WHEN m.direction = 'in' THEN m.fee_amount "
                "  ELSE -m.fee_amount "
                "END), 0.00) AS net_fee, "
                "COALESCE(SUM(CASE "
                "  WHEN m.direction = 'in' THEN m.gross_amount "
                "  ELSE -m.gross_amount "
                "END), 0.00) AS net_gross "
                f"FROM cash_movements m LEFT JOIN sales s ON s.id = m.sale_id {where_mov} GROUP BY m.method",
                params_sales,
            )
            m_rows = cur.fetchall()

        by_method = {
            m.value: {
                "recognized_principal": "0.00",
                "recognized_fee": "0.00",
                "recognized_gross": "0.00",
                "net_principal": "0.00",
                "net_fee": "0.00",
                "net_gross": "0.00",
            }
            for m in PaymentMethod
        }
        for r in m_rows:
            by_method[r["method"]] = {
                "recognized_principal": f"{Decimal(str(r['recognized_principal'])):.2f}",
                "recognized_fee": f"{Decimal(str(r['recognized_fee'])):.2f}",
                "recognized_gross": f"{Decimal(str(r['recognized_gross'])):.2f}",
                "net_principal": f"{Decimal(str(r['net_principal'])):.2f}",
                "net_fee": f"{Decimal(str(r['net_fee'])):.2f}",
                "net_gross": f"{Decimal(str(r['net_gross'])):.2f}",
            }

        return {
            "active_sales_count": int(s_row["active_sales_count"] or 0),
            "voided_sales_count": int(s_row["voided_sales_count"] or 0),
            "pending_customer_refunds_count": int(s_row["pending_customer_refunds_count"] or 0),
            "active_sales_subtotal": f"{Decimal(str(s_row['active_sales_subtotal'])):.2f}",
            "active_card_fees": f"{Decimal(str(s_row['active_card_fees'])):.2f}",
            "active_total_charged": f"{Decimal(str(s_row['active_total_charged'])):.2f}",
            "voided_total_charged": f"{Decimal(str(s_row['voided_total_charged'])):.2f}",
            "pending_customer_refunds_total": f"{Decimal(str(s_row['pending_customer_refunds_total'])):.2f}",
            "completed_customer_refunds_total": f"{Decimal(str(s_row['completed_customer_refunds_total'])):.2f}",
            "by_method": by_method,
        }


class MariaDbAuditLogRepository(AuditLogRepository):
    def __init__(self, conn):
        self._conn = conn

    def add(self, operator_id: int, action: str, entity: str, entity_id: int,
            created_at: datetime, reason: Optional[str] = None,
            details: Optional[str] = None) -> AuditLogEntry:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_logs (operator_id, action, entity, entity_id, reason, details, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (operator_id, action, entity, entity_id, reason, details, created_at),
            )
            log_id = cur.lastrowid
        return AuditLogEntry(
            id=log_id,
            operator_id=operator_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            reason=reason,
            details=details,
            created_at=created_at,
        )

    def list_for_entity(self, entity: str, entity_id: int) -> List[AuditLogEntry]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, operator_id, action, entity, entity_id, reason, details, created_at "
                "FROM audit_logs WHERE entity = %s AND entity_id = %s ORDER BY id ASC",
                (entity, entity_id),
            )
            rows = cur.fetchall()
        return [
            AuditLogEntry(
                id=r["id"],
                operator_id=r["operator_id"],
                action=r["action"],
                entity=r["entity"],
                entity_id=r["entity_id"],
                reason=r.get("reason"),
                details=r.get("details"),
                created_at=r["created_at"],
            )
            for r in rows
        ]
