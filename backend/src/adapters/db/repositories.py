"""Repositórios SQL (consultas sempre parametrizadas)."""
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

import pymysql

from ...domain.errors import (
    ProductAlreadyExistsError,
    ProductNotFoundError,
    TabAlreadyOpenError,
    UserAlreadyExistsError,
)
from ...domain.finance import ZERO, plan_tab_excess_reversals
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
    Tab,
    TabStatus,
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
    TabRepository,
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


def _dec_or_none(value) -> Optional[Decimal]:
    return Decimal(str(value)) if value is not None else None


def _row_to_shift(
    row: dict,
    opened_by_name: Optional[str] = None,
    closed_by_name: Optional[str] = None,
) -> Shift:
    return Shift(
        id=row["id"],
        status=ShiftStatus(row["status"]),
        opened_by=row["opened_by"],
        opened_at=row["opened_at"],
        opening_cash=Decimal(str(row["opening_cash"])),
        closed_by=row.get("closed_by"),
        closed_at=row.get("closed_at"),
        counted_cash=_dec_or_none(row.get("counted_cash")),
        notes=row.get("notes"),
        open_idempotency_key=row.get("open_idempotency_key"),
        open_request_fingerprint=row.get("open_request_fingerprint"),
        expected_cash=_dec_or_none(row.get("expected_cash")),
        cash_difference=_dec_or_none(row.get("cash_difference")),
        expected_pix=_dec_or_none(row.get("expected_pix")),
        counted_pix=_dec_or_none(row.get("counted_pix")),
        pix_difference=_dec_or_none(row.get("pix_difference")),
        expected_card=_dec_or_none(row.get("expected_card")),
        counted_card=_dec_or_none(row.get("counted_card")),
        card_difference=_dec_or_none(row.get("card_difference")),
        discrepancy_justification=row.get("discrepancy_justification"),
        close_idempotency_key=row.get("close_idempotency_key"),
        close_request_fingerprint=row.get("close_request_fingerprint"),
        closing_snapshot=row.get("closing_snapshot"),
        opened_by_name=opened_by_name or row.get("opened_by_name"),
        closed_by_name=closed_by_name or row.get("closed_by_name"),
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
        batch_key=row.get("batch_key"),
        request_fingerprint=row.get("request_fingerprint"),
        voided_by=row.get("voided_by"),
        voided_at=row.get("voided_at"),
        void_reason=row.get("void_reason"),
        customer_refund_status=CustomerRefundStatus(row.get("customer_refund_status", "not_applicable")),
        customer_refund_amount=Decimal(str(row.get("customer_refund_amount", "0.00"))),
        customer_refunded_at=row.get("customer_refunded_at"),
        customer_refunded_by=row.get("customer_refunded_by"),
        customer_refund_method=(
            PaymentMethod(row["customer_refund_method"])
            if row.get("customer_refund_method")
            else None
        ),
        customer_refund_notes=row.get("customer_refund_notes"),
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
        customer_refund_amount=Decimal(str(row.get("customer_refund_amount", "0.00"))),
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

    def _hydrate_shift(self, row: Optional[dict]) -> Optional[Shift]:
        if not row:
            return None
        user_ids = {row["opened_by"]}
        if row.get("closed_by") is not None:
            user_ids.add(row["closed_by"])
        placeholders = ", ".join(["%s"] * len(user_ids))
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT id, name FROM users WHERE id IN ({placeholders})",
                tuple(user_ids),
            )
            names_map = {r["id"]: r["name"] for r in cur.fetchall()}
        return _row_to_shift(
            row,
            opened_by_name=names_map.get(row["opened_by"]),
            closed_by_name=names_map.get(row.get("closed_by")),
        )

    def get_open_shift(self, for_update: bool = False) -> Optional[Shift]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM shifts WHERE status = 'open' ORDER BY id DESC LIMIT 1{lock_sql}"
            )
            row = cur.fetchone()
        return self._hydrate_shift(row)

    def get_by_id(self, shift_id: int, for_update: bool = False) -> Optional[Shift]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM shifts WHERE id = %s{lock_sql}",
                (shift_id,),
            )
            row = cur.fetchone()
        return self._hydrate_shift(row)

    def get_by_open_idempotency_key(self, idempotency_key: str, for_update: bool = False) -> Optional[Shift]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM shifts WHERE open_idempotency_key = %s{lock_sql}",
                (idempotency_key,),
            )
            row = cur.fetchone()
        return self._hydrate_shift(row)

    def get_by_close_idempotency_key(self, idempotency_key: str, for_update: bool = False) -> Optional[Shift]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM shifts WHERE close_idempotency_key = %s{lock_sql}",
                (idempotency_key,),
            )
            row = cur.fetchone()
        return self._hydrate_shift(row)

    def list_shifts(self, status: Optional[ShiftStatus] = None, limit: int = 50) -> List[Shift]:
        where = "WHERE status = %s" if status is not None else ""
        params = (status.value, limit) if status is not None else (limit,)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM shifts {where} ORDER BY id DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
        return [self._hydrate_shift(r) for r in rows if r is not None]  # type: ignore[misc]

    def create_open_shift(
        self,
        opened_by: int,
        now: datetime,
        opening_cash: Decimal = Decimal("0.00"),
        notes: Optional[str] = None,
        open_idempotency_key: Optional[str] = None,
        open_request_fingerprint: Optional[str] = None,
    ) -> Shift:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shifts (status, opened_by, opened_at, opening_cash, "
                "open_idempotency_key, open_request_fingerprint, notes) "
                "VALUES ('open', %s, %s, %s, %s, %s, %s)",
                (
                    opened_by,
                    now,
                    str(opening_cash),
                    open_idempotency_key,
                    open_request_fingerprint,
                    notes,
                ),
            )
            shift_id = cur.lastrowid
        shift = self.get_by_id(shift_id)
        assert shift is not None
        return shift

    def close_shift(
        self,
        shift_id: int,
        closed_by: int,
        now: datetime,
        counted_cash: Optional[Decimal] = None,
        notes: Optional[str] = None,
        expected_cash: Optional[Decimal] = None,
        cash_difference: Optional[Decimal] = None,
        expected_pix: Optional[Decimal] = None,
        counted_pix: Optional[Decimal] = None,
        pix_difference: Optional[Decimal] = None,
        expected_card: Optional[Decimal] = None,
        counted_card: Optional[Decimal] = None,
        card_difference: Optional[Decimal] = None,
        discrepancy_justification: Optional[str] = None,
        closing_snapshot: Optional[str] = None,
        close_idempotency_key: Optional[str] = None,
        close_request_fingerprint: Optional[str] = None,
    ) -> Shift:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE shifts SET status = 'closed', closed_by = %s, closed_at = %s, "
                "expected_cash = %s, counted_cash = %s, cash_difference = %s, "
                "expected_pix = %s, counted_pix = %s, pix_difference = %s, "
                "expected_card = %s, counted_card = %s, card_difference = %s, "
                "notes = COALESCE(%s, notes), discrepancy_justification = %s, "
                "close_idempotency_key = %s, close_request_fingerprint = %s, "
                "closing_snapshot = %s WHERE id = %s",
                (
                    closed_by,
                    now,
                    str(expected_cash) if expected_cash is not None else None,
                    str(counted_cash) if counted_cash is not None else None,
                    str(cash_difference) if cash_difference is not None else None,
                    str(expected_pix) if expected_pix is not None else None,
                    str(counted_pix) if counted_pix is not None else None,
                    str(pix_difference) if pix_difference is not None else None,
                    str(expected_card) if expected_card is not None else None,
                    str(counted_card) if counted_card is not None else None,
                    str(card_difference) if card_difference is not None else None,
                    notes,
                    discrepancy_justification,
                    close_idempotency_key,
                    close_request_fingerprint,
                    closing_snapshot,
                    shift_id,
                ),
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
                "reverses_id, operator_id, created_at, idempotency_key, batch_key, request_fingerprint, note "
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

    def list_by_tab(self, tab_id: int, for_update: bool = False) -> List[Sale]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sales WHERE tab_id = %s ORDER BY id ASC" + lock_sql,
                (tab_id,),
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
                    customer_refund_notes: Optional[str] = None,
                    customer_refund_status: Optional[CustomerRefundStatus] = None,
                    customer_refund_amount: Optional[Decimal] = None) -> None:
        refund_status = customer_refund_status or (
            CustomerRefundStatus.COMPLETED if customer_refund_completed else CustomerRefundStatus.PENDING
        )
        is_completed = refund_status == CustomerRefundStatus.COMPLETED
        with self._conn.cursor() as cur:
            if customer_refund_amount is None:
                if refund_status == CustomerRefundStatus.NOT_APPLICABLE:
                    resolved_amount = ZERO
                else:
                    cur.execute("SELECT total_charged FROM sales WHERE id = %s", (sale_id,))
                    row = cur.fetchone()
                    resolved_amount = Decimal(str(row["total_charged"])) if row else ZERO
            else:
                resolved_amount = customer_refund_amount

            cur.execute(
                "UPDATE sales SET status = 'voided', voided_by = %s, voided_at = %s, void_reason = %s, "
                "customer_refund_status = %s, customer_refund_amount = %s, "
                "customer_refunded_at = %s, customer_refunded_by = %s, "
                "customer_refund_method = %s, customer_refund_notes = %s WHERE id = %s",
                (
                    voided_by,
                    voided_at,
                    reason,
                    refund_status.value,
                    str(resolved_amount),
                    voided_at if is_completed else None,
                    voided_by if is_completed else None,
                    customer_refund_method.value if (is_completed and customer_refund_method) else None,
                    customer_refund_notes if is_completed else None,
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


class MariaDbTabRepository(TabRepository):
    def __init__(self, conn):
        self._conn = conn

    def _hydrate_tab(self, row: dict, for_update: bool = False) -> Tab:
        tab_id = row["id"]
        sales_repo = MariaDbSaleRepository(self._conn)
        mov_repo = MariaDbCashMovementRepository(self._conn)
        launches = sales_repo.list_by_tab(tab_id, for_update=for_update)
        payments = mov_repo.list_by_tab(tab_id, for_update=for_update)

        subtotal_consumed = sum(
            (s.subtotal for s in launches if s.status == SaleStatus.ACTIVE),
            ZERO,
        )
        voided_subtotal = sum(
            (s.subtotal for s in launches if s.status == SaleStatus.VOIDED),
            ZERO,
        )

        sale_payment_ids = {
            m.id
            for m in payments
            if m.direction == CashDirection.IN and m.kind == CashMovementKind.SALE_PAYMENT
        }
        credit_deposit_ids = {
            m.id
            for m in payments
            if m.direction == CashDirection.IN and m.kind == CashMovementKind.CREDIT_DEPOSIT
        }

        in_principal = sum(
            (
                m.amount
                for m in payments
                if m.direction == CashDirection.IN and m.kind == CashMovementKind.SALE_PAYMENT
            ),
            ZERO,
        )
        out_principal = sum(
            (
                m.amount
                for m in payments
                if m.direction == CashDirection.OUT
                and m.kind == CashMovementKind.REVERSAL
                and (m.reverses_id in sale_payment_ids or m.reverses_id is None)
            ),
            ZERO,
        )
        net_principal_paid = in_principal - out_principal

        in_sale_fee = sum(
            (
                m.fee_amount
                for m in payments
                if m.direction == CashDirection.IN and m.kind == CashMovementKind.SALE_PAYMENT
            ),
            ZERO,
        )
        out_sale_fee = sum(
            (
                m.fee_amount
                for m in payments
                if m.direction == CashDirection.OUT
                and m.kind == CashMovementKind.REVERSAL
                and (m.reverses_id in sale_payment_ids or m.reverses_id is None)
            ),
            ZERO,
        )
        sale_card_fee_paid = in_sale_fee - out_sale_fee

        voided_credit_ids = {
            m.reverses_id
            for m in payments
            if m.direction == CashDirection.OUT
            and m.kind == CashMovementKind.REVERSAL
            and m.reverses_id in credit_deposit_ids
        } | {
            m.id
            for m in payments
            if m.direction == CashDirection.IN
            and m.kind == CashMovementKind.CREDIT_DEPOSIT
            and m.voided_at is not None
        }
        credit_deposited = sum(
            (
                m.amount
                for m in payments
                if m.direction == CashDirection.IN
                and m.kind == CashMovementKind.CREDIT_DEPOSIT
                and m.id not in voided_credit_ids
            ),
            ZERO,
        )

        in_credit_fee = sum(
            (
                m.fee_amount
                for m in payments
                if m.direction == CashDirection.IN and m.kind == CashMovementKind.CREDIT_DEPOSIT
            ),
            ZERO,
        )
        out_credit_fee = sum(
            (
                m.fee_amount
                for m in payments
                if m.direction == CashDirection.OUT
                and m.kind == CashMovementKind.REVERSAL
                and m.reverses_id in credit_deposit_ids
            ),
            ZERO,
        )
        credit_card_fee_paid = in_credit_fee - out_credit_fee

        credit_refunded = sum(
            (
                m.amount
                for m in payments
                if m.direction == CashDirection.OUT and m.kind == CashMovementKind.CREDIT_REFUND
            ),
            ZERO,
        )
        credit_pool = max(ZERO, credit_deposited - credit_refunded)

        pending_sale_refund_total = sum(
            (
                s.customer_refund_amount
                for s in launches
                if s.status == SaleStatus.VOIDED and s.customer_refund_status == CustomerRefundStatus.PENDING
            ),
            ZERO,
        )
        pending_credit_refund_total = sum(
            (
                m.customer_refund_amount
                for m in payments
                if m.direction == CashDirection.IN
                and m.kind == CashMovementKind.CREDIT_DEPOSIT
                and m.voided_at is not None
                and m.customer_refund_status == CustomerRefundStatus.PENDING
            ),
            ZERO,
        )
        pending_refund_total = pending_sale_refund_total + pending_credit_refund_total

        completed_sale_refund_total = sum(
            (
                s.customer_refund_amount
                for s in launches
                if s.status == SaleStatus.VOIDED and s.customer_refund_status == CustomerRefundStatus.COMPLETED
            ),
            ZERO,
        )
        completed_credit_refund_total = sum(
            (
                m.customer_refund_amount
                for m in payments
                if m.direction == CashDirection.IN
                and m.kind == CashMovementKind.CREDIT_DEPOSIT
                and m.voided_at is not None
                and m.customer_refund_status == CustomerRefundStatus.COMPLETED
            ),
            ZERO,
        )
        completed_refund_total = completed_sale_refund_total + completed_credit_refund_total

        active_ordinary_principal = max(ZERO, net_principal_paid - pending_sale_refund_total)
        principal_paid = min(subtotal_consumed, active_ordinary_principal)
        unpaid_by_ordinary = max(ZERO, subtotal_consumed - principal_paid)
        credit_applied = min(unpaid_by_ordinary, credit_pool)
        credit_balance = max(ZERO, credit_pool - credit_applied)
        settled_subtotal = principal_paid + credit_applied
        balance_due = max(ZERO, subtotal_consumed - settled_subtotal)

        card_fee_paid = sale_card_fee_paid + credit_card_fee_paid
        total_charged_paid = net_principal_paid + credit_pool + card_fee_paid

        ordinary_payments = [
            m
            for m in payments
            if m.kind == CashMovementKind.SALE_PAYMENT
            or (
                m.kind == CashMovementKind.REVERSAL
                and (m.reverses_id in sale_payment_ids or m.reverses_id is None)
            )
        ]
        credit_deposits = [
            m
            for m in payments
            if m.kind == CashMovementKind.CREDIT_DEPOSIT
            or (m.kind == CashMovementKind.REVERSAL and m.reverses_id in credit_deposit_ids)
        ]
        credit_refunds = [
            m
            for m in payments
            if m.kind == CashMovementKind.CREDIT_REFUND
        ]

        return Tab(
            id=tab_id,
            shift_id=row["shift_id"],
            number=int(row["number"]),
            customer_name=row.get("customer_name"),
            status=TabStatus(row["status"]),
            opened_by=row["opened_by"],
            opened_at=row["opened_at"],
            closed_by=row.get("closed_by"),
            closed_at=row.get("closed_at"),
            notes=row.get("notes"),
            subtotal_consumed=subtotal_consumed,
            voided_subtotal=voided_subtotal,
            credit_deposited=credit_deposited,
            credit_card_fee_paid=credit_card_fee_paid,
            credit_applied=credit_applied,
            credit_refunded=credit_refunded,
            credit_balance=credit_balance,
            principal_paid=principal_paid,
            net_principal_paid=net_principal_paid,
            sale_card_fee_paid=sale_card_fee_paid,
            card_fee_paid=card_fee_paid,
            total_charged_paid=total_charged_paid,
            settled_subtotal=settled_subtotal,
            balance_due=balance_due,
            pending_refund_total=pending_refund_total,
            completed_refund_total=completed_refund_total,
            launches=launches,
            payments=ordinary_payments,
            credit_deposits=credit_deposits,
            credit_refunds=credit_refunds,
        )

    def create_tab(
        self,
        shift_id: int,
        number: int,
        customer_name: Optional[str],
        opened_by: int,
        opened_at: datetime,
        notes: Optional[str] = None,
    ) -> int:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tabs (shift_id, number, customer_name, status, opened_by, opened_at, notes) "
                    "VALUES (%s, %s, %s, 'open', %s, %s, %s)",
                    (shift_id, number, customer_name, opened_by, opened_at, notes),
                )
                return cur.lastrowid
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                raise TabAlreadyOpenError(
                    f"Já existe uma comanda aberta com o número {number}."
                ) from exc
            raise

    def get_by_id(self, tab_id: int, for_update: bool = False) -> Optional[Tab]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, shift_id, number, customer_name, status, opened_by, opened_at, "
                f"closed_by, closed_at, notes FROM tabs WHERE id = %s{lock_sql}",
                (tab_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._hydrate_tab(row, for_update=for_update)

    def get_open_by_number(self, number: int, for_update: bool = False) -> Optional[Tab]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, shift_id, number, customer_name, status, opened_by, opened_at, "
                f"closed_by, closed_at, notes FROM tabs WHERE number = %s AND status = 'open'{lock_sql}",
                (number,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._hydrate_tab(row, for_update=for_update)

    def list_tabs(
        self,
        status: Optional[TabStatus] = None,
        number: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 100,
        shift_id: Optional[int] = None,
    ) -> List[Tab]:
        clauses = []
        params = []
        if shift_id is not None:
            clauses.append("shift_id = %s")
            params.append(shift_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        if number is not None:
            clauses.append("number = %s")
            params.append(number)
        if search:
            if search.isdigit():
                clauses.append("(number = %s OR customer_name LIKE %s)")
                params.extend([int(search), f"%{search}%"])
            else:
                clauses.append("customer_name LIKE %s")
                params.append(f"%{search}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, shift_id, number, customer_name, status, opened_by, opened_at, "
                "closed_by, closed_at, notes FROM tabs "
                f"{where} ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END ASC, number ASC, id DESC LIMIT %s",
                tuple(params),
            )
            rows = cur.fetchall()
        return [self._hydrate_tab(r, for_update=False) for r in rows]

    def close_tab(
        self,
        tab_id: int,
        closed_by: int,
        closed_at: datetime,
        notes: Optional[str] = None,
    ) -> None:
        with self._conn.cursor() as cur:
            if notes is not None:
                cur.execute(
                    "UPDATE tabs SET status = 'closed', closed_by = %s, closed_at = %s, notes = %s WHERE id = %s",
                    (closed_by, closed_at, notes, tab_id),
                )
            else:
                cur.execute(
                    "UPDATE tabs SET status = 'closed', closed_by = %s, closed_at = %s WHERE id = %s",
                    (closed_by, closed_at, tab_id),
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
        batch_key: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
    ) -> CashMovement:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cash_movements (shift_id, tab_id, sale_id, kind, method, direction, "
                "amount, fee_percent, fee_mode, fee_amount, gross_amount, cash_tendered, change_given, "
                "reverses_id, operator_id, created_at, idempotency_key, batch_key, request_fingerprint, note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                    batch_key,
                    request_fingerprint,
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
            batch_key=batch_key,
            request_fingerprint=request_fingerprint,
        )

    def get_by_idempotency_key(self, idempotency_key: str, for_update: bool = False) -> Optional[CashMovement]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cash_movements WHERE idempotency_key = %s" + lock_sql,
                (idempotency_key,),
            )
            row = cur.fetchone()
        return _row_to_cash_movement(row) if row else None

    def list_by_batch_key(self, batch_key: str, for_update: bool = False) -> List[CashMovement]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cash_movements WHERE batch_key = %s ORDER BY id ASC" + lock_sql,
                (batch_key,),
            )
            rows = cur.fetchall()
        return [_row_to_cash_movement(r) for r in rows]

    def list_by_sale(self, sale_id: int, for_update: bool = False) -> List[CashMovement]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cash_movements WHERE sale_id = %s ORDER BY id ASC" + lock_sql,
                (sale_id,),
            )
            rows = cur.fetchall()
        return [_row_to_cash_movement(r) for r in rows]

    def list_by_tab(self, tab_id: int, for_update: bool = False) -> List[CashMovement]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cash_movements WHERE tab_id = %s ORDER BY id ASC" + lock_sql,
                (tab_id,),
            )
            rows = cur.fetchall()
        return [_row_to_cash_movement(r) for r in rows]

    def mark_credit_deposit_voided(
        self,
        movement_id: int,
        voided_by: int,
        voided_at: datetime,
        reason: str,
        customer_refund_status: CustomerRefundStatus,
        customer_refund_amount: Decimal,
        customer_refund_method: Optional[PaymentMethod] = None,
        customer_refund_notes: Optional[str] = None,
    ) -> None:
        is_completed = customer_refund_status == CustomerRefundStatus.COMPLETED
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE cash_movements SET voided_by = %s, voided_at = %s, void_reason = %s, "
                "customer_refund_status = %s, customer_refund_amount = %s, "
                "customer_refunded_at = %s, customer_refunded_by = %s, "
                "customer_refund_method = %s, customer_refund_notes = %s WHERE id = %s",
                (
                    voided_by,
                    voided_at,
                    reason,
                    customer_refund_status.value,
                    str(customer_refund_amount),
                    voided_at if is_completed else None,
                    voided_by if is_completed else None,
                    customer_refund_method.value if (is_completed and customer_refund_method) else None,
                    customer_refund_notes if is_completed else None,
                    movement_id,
                ),
            )

    def mark_credit_deposit_refunded(
        self,
        movement_id: int,
        refunded_by: int,
        refunded_at: datetime,
        method: PaymentMethod,
        notes: Optional[str] = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE cash_movements SET customer_refund_status = 'completed', "
                "customer_refunded_by = %s, customer_refunded_at = %s, "
                "customer_refund_method = %s, customer_refund_notes = %s WHERE id = %s",
                (refunded_by, refunded_at, method.value, notes, movement_id),
            )

    def list_drawer_movements(self, shift_id: int, for_update: bool = False) -> List[dict]:
        lock_sql = " FOR UPDATE" if for_update else ""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT m.id, m.shift_id, m.kind, m.method, m.direction, m.amount, m.gross_amount, "
                "m.operator_id, u.name AS operator_name, m.created_at, m.idempotency_key, m.note "
                "FROM cash_movements m "
                "LEFT JOIN users u ON u.id = m.operator_id "
                "WHERE m.shift_id = %s AND m.kind IN ('cash_supply', 'cash_withdrawal') "
                f"ORDER BY m.id ASC{lock_sql}",
                (shift_id,),
            )
            rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "shift_id": r["shift_id"],
                "kind": r["kind"],
                "method": r["method"],
                "direction": r["direction"],
                "amount": f"{Decimal(str(r['amount'])):.2f}",
                "gross_amount": f"{Decimal(str(r['gross_amount'])):.2f}",
                "operator_id": r["operator_id"],
                "operator_name": r.get("operator_name"),
                "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
                "idempotency_key": r["idempotency_key"],
                "reason": r.get("note") or "",
                "note": r.get("note") or "",
            })
        return result

    def list_pending_refunds(self, shift_id: int, for_update: bool = False) -> List[dict]:
        lock_sql = " FOR UPDATE" if for_update else ""
        pending: List[dict] = []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT s.id AS sale_id, s.shift_id, s.tab_id, t.number AS tab_number, "
                "t.customer_name, s.kind, s.customer_refund_amount, s.void_reason, s.voided_at "
                "FROM sales s "
                "LEFT JOIN tabs t ON t.id = s.tab_id "
                "WHERE s.shift_id = %s AND s.status = 'voided' AND s.customer_refund_status = 'pending' "
                f"ORDER BY s.id ASC{lock_sql}",
                (shift_id,),
            )
            for r in cur.fetchall():
                pending.append({
                    "obligation_kind": "quick_sale_refund" if r["kind"] == "quick" else "tab_item_refund",
                    "sale_id": r["sale_id"],
                    "movement_id": None,
                    "tab_id": r.get("tab_id"),
                    "tab_number": r.get("tab_number"),
                    "customer_name": r.get("customer_name"),
                    "amount": f"{Decimal(str(r['customer_refund_amount'])):.2f}",
                    "refund_amount": f"{Decimal(str(r['customer_refund_amount'])):.2f}",
                    "void_reason": r.get("void_reason"),
                    "voided_at": r["voided_at"].isoformat() if r.get("voided_at") else None,
                })

            cur.execute(
                "SELECT m.id AS movement_id, m.shift_id, m.tab_id, t.number AS tab_number, "
                "t.customer_name, m.customer_refund_amount, m.void_reason, m.voided_at "
                "FROM cash_movements m "
                "LEFT JOIN tabs t ON t.id = m.tab_id "
                "WHERE m.shift_id = %s AND m.kind = 'credit_deposit' "
                "AND m.voided_at IS NOT NULL AND m.customer_refund_status = 'pending' "
                f"ORDER BY m.id ASC{lock_sql}",
                (shift_id,),
            )
            for r in cur.fetchall():
                pending.append({
                    "obligation_kind": "credit_deposit_refund",
                    "sale_id": None,
                    "movement_id": r["movement_id"],
                    "tab_id": r.get("tab_id"),
                    "tab_number": r.get("tab_number"),
                    "customer_name": r.get("customer_name"),
                    "amount": f"{Decimal(str(r['customer_refund_amount'])):.2f}",
                    "refund_amount": f"{Decimal(str(r['customer_refund_amount'])):.2f}",
                    "void_reason": r.get("void_reason"),
                    "voided_at": r["voided_at"].isoformat() if r.get("voided_at") else None,
                })
        return pending

    def get_financial_summary(self, shift_id: Optional[int] = None) -> dict:
        resolved_shift_id = shift_id
        opening_cash = ZERO
        with self._conn.cursor() as cur:
            if resolved_shift_id is not None:
                cur.execute(
                    "SELECT id, opening_cash FROM shifts WHERE id = %s",
                    (resolved_shift_id,),
                )
                sh_row = cur.fetchone()
                if sh_row:
                    opening_cash = Decimal(str(sh_row["opening_cash"]))
            else:
                cur.execute(
                    "SELECT id, opening_cash FROM shifts WHERE status = 'open' ORDER BY id DESC LIMIT 1"
                )
                sh_row = cur.fetchone()
                if not sh_row:
                    cur.execute(
                        "SELECT id, opening_cash FROM shifts ORDER BY id DESC LIMIT 1"
                    )
                    sh_row = cur.fetchone()
                if sh_row:
                    resolved_shift_id = sh_row["id"]
                    opening_cash = Decimal(str(sh_row["opening_cash"]))

            where_sales = "WHERE shift_id = %s" if resolved_shift_id is not None else ""
            params_sales = (resolved_shift_id,) if resolved_shift_id is not None else ()
            cur.execute(
                "SELECT "
                "COUNT(CASE WHEN status = 'active' THEN 1 END) AS active_sales_count, "
                "COUNT(CASE WHEN status = 'voided' THEN 1 END) AS voided_sales_count, "
                "COUNT(CASE WHEN status = 'voided' AND customer_refund_status = 'pending' THEN 1 END) AS pending_customer_refunds_count, "
                "COALESCE(SUM(CASE WHEN status = 'active' THEN subtotal ELSE 0 END), 0.00) AS active_sales_subtotal, "
                "COALESCE(SUM(CASE WHEN status = 'active' AND kind = 'quick' THEN subtotal ELSE 0 END), 0.00) AS quick_sales_subtotal, "
                "COALESCE(SUM(CASE WHEN status = 'active' AND kind = 'tab_item' THEN subtotal ELSE 0 END), 0.00) AS tabs_consumed_subtotal, "
                "COALESCE(SUM(CASE WHEN status = 'voided' THEN total_charged ELSE 0 END), 0.00) AS voided_total_charged, "
                "COALESCE(SUM(CASE WHEN status = 'voided' AND customer_refund_status = 'pending' THEN customer_refund_amount ELSE 0 END), 0.00) AS pending_customer_refunds_total, "
                "COALESCE(SUM(CASE WHEN status = 'voided' AND customer_refund_status = 'completed' THEN customer_refund_amount ELSE 0 END), 0.00) AS completed_customer_refunds_total "
                f"FROM sales {where_sales}",
                params_sales,
            )
            s_row = cur.fetchone()

            where_mov = "WHERE m.shift_id = %s" if resolved_shift_id is not None else ""
            cur.execute(
                "SELECT m.method, "
                "COALESCE(SUM(CASE "
                "  WHEN m.tab_id IS NULL AND m.direction = 'in' AND m.kind = 'sale_payment' AND s.status = 'active' THEN m.amount "
                "  WHEN m.tab_id IS NOT NULL AND m.direction = 'in' AND m.kind = 'sale_payment' THEN m.amount "
                "  WHEN m.tab_id IS NOT NULL AND m.direction = 'out' AND m.kind = 'reversal' "
                "       AND (orig.kind = 'sale_payment' OR orig.id IS NULL) THEN -m.amount "
                "  ELSE 0 "
                "END), 0.00) AS recognized_principal, "
                "COALESCE(SUM(CASE "
                "  WHEN m.tab_id IS NULL AND m.direction = 'in' AND m.kind = 'sale_payment' AND s.status = 'active' THEN m.fee_amount "
                "  WHEN m.tab_id IS NOT NULL AND m.direction = 'in' AND m.kind = 'sale_payment' THEN m.fee_amount "
                "  WHEN m.tab_id IS NOT NULL AND m.direction = 'out' AND m.kind = 'reversal' "
                "       AND (orig.kind = 'sale_payment' OR orig.id IS NULL) THEN -m.fee_amount "
                "  ELSE 0 "
                "END), 0.00) AS recognized_fee, "
                "COALESCE(SUM(CASE "
                "  WHEN m.tab_id IS NULL AND m.direction = 'in' AND m.kind = 'sale_payment' AND s.status = 'active' THEN m.gross_amount "
                "  WHEN m.tab_id IS NOT NULL AND m.direction = 'in' AND m.kind = 'sale_payment' THEN m.gross_amount "
                "  WHEN m.tab_id IS NOT NULL AND m.direction = 'out' AND m.kind = 'reversal' "
                "       AND (orig.kind = 'sale_payment' OR orig.id IS NULL) THEN -m.gross_amount "
                "  ELSE 0 "
                "END), 0.00) AS recognized_gross, "
                "COALESCE(SUM(CASE "
                "  WHEN m.kind IN ('sale_payment', 'credit_deposit', 'credit_refund', 'reversal') AND m.direction = 'in' THEN m.amount "
                "  WHEN m.kind IN ('sale_payment', 'credit_deposit', 'credit_refund', 'reversal') AND m.direction = 'out' THEN -m.amount "
                "  ELSE 0 "
                "END), 0.00) AS net_principal, "
                "COALESCE(SUM(CASE "
                "  WHEN m.kind IN ('sale_payment', 'credit_deposit', 'credit_refund', 'reversal') AND m.direction = 'in' THEN m.fee_amount "
                "  WHEN m.kind IN ('sale_payment', 'credit_deposit', 'credit_refund', 'reversal') AND m.direction = 'out' THEN -m.fee_amount "
                "  ELSE 0 "
                "END), 0.00) AS net_fee, "
                "COALESCE(SUM(CASE "
                "  WHEN m.kind IN ('sale_payment', 'credit_deposit', 'credit_refund', 'reversal') AND m.direction = 'in' THEN m.gross_amount "
                "  WHEN m.kind IN ('sale_payment', 'credit_deposit', 'credit_refund', 'reversal') AND m.direction = 'out' THEN -m.gross_amount "
                "  ELSE 0 "
                "END), 0.00) AS net_gross, "
                "COALESCE(SUM(CASE WHEN m.kind IN ('sale_payment', 'credit_deposit') AND m.direction = 'in' THEN m.amount ELSE 0 END), 0.00) AS inflow_principal, "
                "COALESCE(SUM(CASE WHEN m.kind IN ('sale_payment', 'credit_deposit') AND m.direction = 'in' THEN m.fee_amount ELSE 0 END), 0.00) AS inflow_fee, "
                "COALESCE(SUM(CASE WHEN m.kind IN ('sale_payment', 'credit_deposit') AND m.direction = 'in' THEN m.gross_amount ELSE 0 END), 0.00) AS inflow_gross, "
                "COALESCE(SUM(CASE WHEN m.kind IN ('credit_refund', 'reversal') AND m.direction = 'out' THEN m.amount ELSE 0 END), 0.00) AS outflow_principal, "
                "COALESCE(SUM(CASE WHEN m.kind IN ('credit_refund', 'reversal') AND m.direction = 'out' THEN m.fee_amount ELSE 0 END), 0.00) AS outflow_fee, "
                "COALESCE(SUM(CASE WHEN m.kind IN ('credit_refund', 'reversal') AND m.direction = 'out' THEN m.gross_amount ELSE 0 END), 0.00) AS outflow_gross, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'sale_payment' THEN m.amount ELSE 0 END), 0.00) AS sale_payment_in_principal, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'sale_payment' THEN m.fee_amount ELSE 0 END), 0.00) AS sale_payment_in_fee, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'sale_payment' THEN m.gross_amount ELSE 0 END), 0.00) AS sale_payment_in_gross, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'credit_deposit' THEN m.amount ELSE 0 END), 0.00) AS credit_deposit_in_principal, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'credit_deposit' THEN m.fee_amount ELSE 0 END), 0.00) AS credit_deposit_in_fee, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'credit_deposit' THEN m.gross_amount ELSE 0 END), 0.00) AS credit_deposit_in_gross, "
                "COALESCE(SUM(CASE WHEN m.direction = 'out' AND m.kind = 'credit_refund' THEN m.gross_amount ELSE 0 END), 0.00) AS credit_refund_out_gross, "
                "COALESCE(SUM(CASE WHEN m.direction = 'out' AND m.kind = 'reversal' THEN m.amount ELSE 0 END), 0.00) AS reversal_out_principal, "
                "COALESCE(SUM(CASE WHEN m.direction = 'out' AND m.kind = 'reversal' THEN m.fee_amount ELSE 0 END), 0.00) AS reversal_out_fee, "
                "COALESCE(SUM(CASE WHEN m.direction = 'out' AND m.kind = 'reversal' THEN m.gross_amount ELSE 0 END), 0.00) AS reversal_out_gross, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'cash_supply' THEN m.gross_amount ELSE 0 END), 0.00) AS cash_supply_in_gross, "
                "COALESCE(SUM(CASE WHEN m.direction = 'out' AND m.kind = 'cash_withdrawal' THEN m.gross_amount ELSE 0 END), 0.00) AS cash_withdrawal_out_gross, "
                "COUNT(CASE WHEN m.direction = 'in' AND m.kind = 'cash_supply' THEN 1 END) AS cash_supplies_count, "
                "COUNT(CASE WHEN m.direction = 'out' AND m.kind = 'cash_withdrawal' THEN 1 END) AS cash_withdrawals_count, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'sale_payment' THEN m.fee_amount ELSE 0 END), 0.00) AS sale_charged_fee, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind = 'credit_deposit' THEN m.fee_amount ELSE 0 END), 0.00) AS credit_charged_fee, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind IN ('sale_payment', 'credit_deposit') THEN m.fee_amount ELSE 0 END), 0.00) AS charged_fee, "
                "COALESCE(SUM(CASE WHEN m.direction = 'out' AND m.kind IN ('credit_refund', 'reversal') THEN m.fee_amount ELSE 0 END), 0.00) AS reversed_fee, "
                "COALESCE(SUM(CASE WHEN m.direction = 'in' AND m.kind IN ('sale_payment', 'credit_deposit') THEN COALESCE(m.change_given, 0) ELSE 0 END), 0.00) AS change_given_total "
                "FROM cash_movements m "
                "LEFT JOIN sales s ON s.id = m.sale_id "
                "LEFT JOIN cash_movements orig ON orig.id = m.reverses_id "
                f"{where_mov} GROUP BY m.method",
                params_sales,
            )
            m_rows = cur.fetchall()

            where_tabs = "WHERE shift_id = %s" if resolved_shift_id is not None else ""
            cur.execute(
                f"SELECT id, status FROM tabs {where_tabs}",
                params_sales,
            )
            tab_rows = cur.fetchall()

            cur.execute(
                "SELECT "
                "COUNT(CASE WHEN kind = 'credit_deposit' AND voided_at IS NOT NULL AND customer_refund_status = 'pending' THEN 1 END) AS pending_credit_refunds_count, "
                "COALESCE(SUM(CASE WHEN kind = 'credit_deposit' AND voided_at IS NOT NULL AND customer_refund_status = 'pending' THEN customer_refund_amount ELSE 0 END), 0.00) AS pending_credit_refunds_total, "
                "COALESCE(SUM(CASE WHEN kind = 'credit_deposit' AND voided_at IS NOT NULL AND customer_refund_status = 'completed' THEN customer_refund_amount ELSE 0 END), 0.00) AS completed_credit_refunds_total "
                f"FROM cash_movements {where_sales}",
                params_sales,
            )
            cm_void_row = cur.fetchone()

        rec_principal = {m.value: ZERO for m in PaymentMethod}
        rec_fee = {m.value: ZERO for m in PaymentMethod}
        rec_gross = {m.value: ZERO for m in PaymentMethod}
        net_principal_map = {m.value: ZERO for m in PaymentMethod}
        net_fee_map = {m.value: ZERO for m in PaymentMethod}
        net_gross_map = {m.value: ZERO for m in PaymentMethod}
        inflow_principal_map = {m.value: ZERO for m in PaymentMethod}
        inflow_fee_map = {m.value: ZERO for m in PaymentMethod}
        inflow_gross_map = {m.value: ZERO for m in PaymentMethod}
        outflow_principal_map = {m.value: ZERO for m in PaymentMethod}
        outflow_fee_map = {m.value: ZERO for m in PaymentMethod}
        outflow_gross_map = {m.value: ZERO for m in PaymentMethod}
        sale_payment_in_principal_map = {m.value: ZERO for m in PaymentMethod}
        sale_payment_in_fee_map = {m.value: ZERO for m in PaymentMethod}
        sale_payment_in_gross_map = {m.value: ZERO for m in PaymentMethod}
        credit_deposit_in_principal_map = {m.value: ZERO for m in PaymentMethod}
        credit_deposit_in_fee_map = {m.value: ZERO for m in PaymentMethod}
        credit_deposit_in_gross_map = {m.value: ZERO for m in PaymentMethod}
        credit_refund_out_gross_map = {m.value: ZERO for m in PaymentMethod}
        reversal_out_principal_map = {m.value: ZERO for m in PaymentMethod}
        reversal_out_fee_map = {m.value: ZERO for m in PaymentMethod}
        reversal_out_gross_map = {m.value: ZERO for m in PaymentMethod}
        cash_supply_in_gross_map = {m.value: ZERO for m in PaymentMethod}
        cash_withdrawal_out_gross_map = {m.value: ZERO for m in PaymentMethod}
        sale_charged_fee_map = {m.value: ZERO for m in PaymentMethod}
        credit_charged_fee_map = {m.value: ZERO for m in PaymentMethod}
        charged_fee_map = {m.value: ZERO for m in PaymentMethod}
        reversed_fee_map = {m.value: ZERO for m in PaymentMethod}
        change_given_map = {m.value: ZERO for m in PaymentMethod}
        cash_supplies_count = 0
        cash_withdrawals_count = 0

        for r in m_rows:
            meth = r["method"]
            rec_principal[meth] = Decimal(str(r["recognized_principal"]))
            rec_fee[meth] = Decimal(str(r["recognized_fee"]))
            rec_gross[meth] = Decimal(str(r["recognized_gross"]))
            net_principal_map[meth] = Decimal(str(r["net_principal"]))
            net_fee_map[meth] = Decimal(str(r["net_fee"]))
            net_gross_map[meth] = Decimal(str(r["net_gross"]))
            inflow_principal_map[meth] = Decimal(str(r["inflow_principal"]))
            inflow_fee_map[meth] = Decimal(str(r["inflow_fee"]))
            inflow_gross_map[meth] = Decimal(str(r["inflow_gross"]))
            outflow_principal_map[meth] = Decimal(str(r["outflow_principal"]))
            outflow_fee_map[meth] = Decimal(str(r["outflow_fee"]))
            outflow_gross_map[meth] = Decimal(str(r["outflow_gross"]))
            sale_payment_in_principal_map[meth] = Decimal(str(r["sale_payment_in_principal"]))
            sale_payment_in_fee_map[meth] = Decimal(str(r["sale_payment_in_fee"]))
            sale_payment_in_gross_map[meth] = Decimal(str(r["sale_payment_in_gross"]))
            credit_deposit_in_principal_map[meth] = Decimal(str(r["credit_deposit_in_principal"]))
            credit_deposit_in_fee_map[meth] = Decimal(str(r["credit_deposit_in_fee"]))
            credit_deposit_in_gross_map[meth] = Decimal(str(r["credit_deposit_in_gross"]))
            credit_refund_out_gross_map[meth] = Decimal(str(r["credit_refund_out_gross"]))
            reversal_out_principal_map[meth] = Decimal(str(r["reversal_out_principal"]))
            reversal_out_fee_map[meth] = Decimal(str(r["reversal_out_fee"]))
            reversal_out_gross_map[meth] = Decimal(str(r["reversal_out_gross"]))
            cash_supply_in_gross_map[meth] = Decimal(str(r["cash_supply_in_gross"]))
            cash_withdrawal_out_gross_map[meth] = Decimal(str(r["cash_withdrawal_out_gross"]))
            cash_supplies_count += int(r["cash_supplies_count"] or 0)
            cash_withdrawals_count += int(r["cash_withdrawals_count"] or 0)
            sale_charged_fee_map[meth] = Decimal(str(r["sale_charged_fee"]))
            credit_charged_fee_map[meth] = Decimal(str(r["credit_charged_fee"]))
            charged_fee_map[meth] = Decimal(str(r["charged_fee"]))
            reversed_fee_map[meth] = Decimal(str(r["reversed_fee"]))
            change_given_map[meth] = Decimal(str(r["change_given_total"]))

        tabs_open_count = 0
        tabs_closed_count = 0
        tabs_pending_balance_total = ZERO
        tabs_credit_deposited_total = ZERO
        tabs_credit_applied_total = ZERO
        tabs_credit_refunded_total = ZERO
        tabs_credit_balance_total = ZERO
        tabs_credit_card_fees_total = ZERO
        incomplete_operations_count = 0

        tab_repo = MariaDbTabRepository(self._conn)
        for tr in tab_rows:
            t = tab_repo.get_by_id(tr["id"])
            if t is None:
                continue
            if t.status == TabStatus.OPEN:
                tabs_open_count += 1
                tabs_pending_balance_total += t.balance_due
                tabs_credit_balance_total += t.credit_balance
                if t.net_principal_paid > t.subtotal_consumed and t.pending_refund_total == ZERO:
                    incomplete_operations_count += 1
            else:
                tabs_closed_count += 1

            tabs_credit_deposited_total += t.credit_deposited
            tabs_credit_applied_total += t.credit_applied
            tabs_credit_refunded_total += t.credit_refunded
            tabs_credit_card_fees_total += t.credit_card_fee_paid

            # Se a comanda possui excesso de principal de pagamento ordinário ainda não devolvido
            # (cancelamento contábil de consumo com customer_refund_status = 'pending'), deduz do reconhecido:
            if t.net_principal_paid > t.principal_paid:
                excess_p = t.net_principal_paid - t.principal_paid
                plan = plan_tab_excess_reversals(t.payments, excess_p)
                for orig_mov, take_p, take_f, take_g in plan:
                    meth = orig_mov.method.value
                    rec_principal[meth] = max(ZERO, rec_principal[meth] - take_p)
                    rec_fee[meth] = max(ZERO, rec_fee[meth] - take_f)
                    rec_gross[meth] = max(ZERO, rec_gross[meth] - take_g)

        # Soma a taxa de cartão dos depósitos de crédito ativos ao recognized_fee do cartão:
        rec_fee[PaymentMethod.CARD.value] += tabs_credit_card_fees_total
        rec_gross[PaymentMethod.CARD.value] += tabs_credit_card_fees_total

        cash_supplies_total = sum(cash_supply_in_gross_map.values(), ZERO)
        cash_withdrawals_total = sum(cash_withdrawal_out_gross_map.values(), ZERO)
        expected_cash = (
            opening_cash
            + inflow_gross_map[PaymentMethod.CASH.value]
            - outflow_gross_map[PaymentMethod.CASH.value]
            + cash_supplies_total
            - cash_withdrawals_total
        )
        expected_pix = net_gross_map[PaymentMethod.PIX.value]
        expected_card = net_gross_map[PaymentMethod.CARD.value]

        by_method = {}
        for m in PaymentMethod:
            meth = m.value
            expected_for_method = (
                expected_cash
                if m == PaymentMethod.CASH
                else (expected_pix if m == PaymentMethod.PIX else expected_card)
            )
            by_method[meth] = {
                "recognized_principal": f"{rec_principal[meth]:.2f}",
                "recognized_fee": f"{rec_fee[meth]:.2f}",
                "recognized_gross": f"{rec_gross[meth]:.2f}",
                "in_principal": f"{inflow_principal_map[meth]:.2f}",
                "in_fee": f"{inflow_fee_map[meth]:.2f}",
                "in_gross": f"{inflow_gross_map[meth]:.2f}",
                "out_principal": f"{outflow_principal_map[meth]:.2f}",
                "out_fee": f"{outflow_fee_map[meth]:.2f}",
                "out_gross": f"{outflow_gross_map[meth]:.2f}",
                "principal_in": f"{inflow_principal_map[meth]:.2f}",
                "fee_in": f"{inflow_fee_map[meth]:.2f}",
                "gross_in": f"{inflow_gross_map[meth]:.2f}",
                "principal_out": f"{outflow_principal_map[meth]:.2f}",
                "fee_out": f"{outflow_fee_map[meth]:.2f}",
                "gross_out": f"{outflow_gross_map[meth]:.2f}",
                "inflow_principal": f"{inflow_principal_map[meth]:.2f}",
                "inflow_fee": f"{inflow_fee_map[meth]:.2f}",
                "inflow_gross": f"{inflow_gross_map[meth]:.2f}",
                "outflow_principal": f"{outflow_principal_map[meth]:.2f}",
                "outflow_fee": f"{outflow_fee_map[meth]:.2f}",
                "outflow_gross": f"{outflow_gross_map[meth]:.2f}",
                "sale_payment_in_principal": f"{sale_payment_in_principal_map[meth]:.2f}",
                "sale_payment_in_fee": f"{sale_payment_in_fee_map[meth]:.2f}",
                "sale_payment_in_gross": f"{sale_payment_in_gross_map[meth]:.2f}",
                "credit_deposit_in_principal": f"{credit_deposit_in_principal_map[meth]:.2f}",
                "credit_deposit_in_fee": f"{credit_deposit_in_fee_map[meth]:.2f}",
                "credit_deposit_in_gross": f"{credit_deposit_in_gross_map[meth]:.2f}",
                "credit_refund_out_gross": f"{credit_refund_out_gross_map[meth]:.2f}",
                "reversal_out_principal": f"{reversal_out_principal_map[meth]:.2f}",
                "reversal_out_fee": f"{reversal_out_fee_map[meth]:.2f}",
                "reversal_out_gross": f"{reversal_out_gross_map[meth]:.2f}",
                "cash_supply_in_gross": f"{cash_supply_in_gross_map[meth]:.2f}",
                "cash_withdrawal_out_gross": f"{cash_withdrawal_out_gross_map[meth]:.2f}",
                "net_principal": f"{net_principal_map[meth]:.2f}",
                "net_fee": f"{net_fee_map[meth]:.2f}",
                "net_gross": f"{net_gross_map[meth]:.2f}",
                "expected_amount": f"{expected_for_method:.2f}",
                "change_given_total": f"{change_given_map[meth]:.2f}",
            }

        active_sales_subtotal = Decimal(str(s_row["active_sales_subtotal"]))
        quick_sales_subtotal = Decimal(str(s_row["quick_sales_subtotal"]))
        tabs_consumed_subtotal = Decimal(str(s_row["tabs_consumed_subtotal"]))
        active_card_fees = sum(rec_fee.values(), ZERO)
        sale_card_fees_charged_total = sum(sale_charged_fee_map.values(), ZERO)
        credit_card_fees_charged_total = sum(credit_charged_fee_map.values(), ZERO)
        card_fees_charged_total = sum(charged_fee_map.values(), ZERO)
        card_fees_reversed_total = sum(reversed_fee_map.values(), ZERO)
        active_total_charged = active_sales_subtotal + active_card_fees
        principal_received_total = sum(rec_principal.values(), ZERO)
        settled_sales_subtotal = principal_received_total + tabs_credit_applied_total
        inflow_gross_total = sum(inflow_gross_map.values(), ZERO)
        outflow_gross_total = sum(outflow_gross_map.values(), ZERO)
        net_gross_total = sum(net_gross_map.values(), ZERO)
        net_principal_total = sum(net_principal_map.values(), ZERO)

        pending_refunds_count = int(s_row["pending_customer_refunds_count"] or 0) + int(
            cm_void_row["pending_credit_refunds_count"] or 0
        )
        pending_refunds_total = Decimal(str(s_row["pending_customer_refunds_total"])) + Decimal(
            str(cm_void_row["pending_credit_refunds_total"])
        )
        completed_refunds_total = Decimal(str(s_row["completed_customer_refunds_total"])) + Decimal(
            str(cm_void_row["completed_credit_refunds_total"])
        )

        return {
            "shift_id": resolved_shift_id,
            "opening_cash": f"{opening_cash:.2f}",
            "expected_cash": f"{expected_cash:.2f}",
            "expected_pix": f"{expected_pix:.2f}",
            "expected_card": f"{expected_card:.2f}",
            "cash_supplies_count": cash_supplies_count,
            "cash_supplies_total": f"{cash_supplies_total:.2f}",
            "cash_withdrawals_count": cash_withdrawals_count,
            "cash_withdrawals_total": f"{cash_withdrawals_total:.2f}",
            "sales_count": int(s_row["active_sales_count"] or 0),
            "active_sales_count": int(s_row["active_sales_count"] or 0),
            "voided_sales_count": int(s_row["voided_sales_count"] or 0),
            "pending_customer_refunds_count": pending_refunds_count,
            "incomplete_operations_count": incomplete_operations_count,
            "total_products_sold": f"{active_sales_subtotal:.2f}",
            "active_sales_subtotal": f"{active_sales_subtotal:.2f}",
            "quick_sales_subtotal": f"{quick_sales_subtotal:.2f}",
            "tabs_consumed_subtotal": f"{tabs_consumed_subtotal:.2f}",
            "tabs_active_consumed": f"{tabs_consumed_subtotal:.2f}",
            "principal_received_total": f"{principal_received_total:.2f}",
            "settled_sales_subtotal": f"{settled_sales_subtotal:.2f}",
            "credit_deposits_total": f"{tabs_credit_deposited_total:.2f}",
            "tabs_credit_deposited_total": f"{tabs_credit_deposited_total:.2f}",
            "tabs_credit_applied_total": f"{tabs_credit_applied_total:.2f}",
            "credit_refunds_total": f"{tabs_credit_refunded_total:.2f}",
            "tabs_credit_refunded_total": f"{tabs_credit_refunded_total:.2f}",
            "tabs_credit_balance_total": f"{tabs_credit_balance_total:.2f}",
            "card_fees_received_total": f"{active_card_fees:.2f}",
            "card_fees_charged_total": f"{card_fees_charged_total:.2f}",
            "sale_card_fees_charged_total": f"{sale_card_fees_charged_total:.2f}",
            "credit_card_fees_charged_total": f"{credit_card_fees_charged_total:.2f}",
            "card_fees_reversed_total": f"{card_fees_reversed_total:.2f}",
            "active_card_fees": f"{active_card_fees:.2f}",
            "active_total_charged": f"{active_total_charged:.2f}",
            "inflow_gross_total": f"{inflow_gross_total:.2f}",
            "outflow_gross_total": f"{outflow_gross_total:.2f}",
            "net_principal": f"{net_principal_total:.2f}",
            "net_principal_total": f"{net_principal_total:.2f}",
            "net_gross": f"{net_gross_total:.2f}",
            "net_gross_total": f"{net_gross_total:.2f}",
            "net_received_total": f"{net_gross_total:.2f}",
            "tabs_credit_card_fees_total": f"{tabs_credit_card_fees_total:.2f}",
            "voided_total_charged": f"{Decimal(str(s_row['voided_total_charged'])):.2f}",
            "pending_customer_refunds_total": f"{pending_refunds_total:.2f}",
            "completed_customer_refunds_total": f"{completed_refunds_total:.2f}",
            "tabs_open_count": tabs_open_count,
            "tabs_closed_count": tabs_closed_count,
            "tabs_pending_balance_total": f"{tabs_pending_balance_total:.2f}",
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
