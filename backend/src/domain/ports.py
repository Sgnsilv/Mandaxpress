"""Portas (interfaces) exigidas pelo núcleo. Os adaptadores as implementam."""
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from .models import (
    AuditLogEntry,
    CardFeeMode,
    CashDirection,
    CashMovement,
    CashMovementKind,
    PaymentMethod,
    PaymentSettings,
    Product,
    Role,
    Sale,
    SaleItem,
    SaleItemKind,
    SaleKind,
    SessionRecord,
    Shift,
    User,
)


class UserRepository(ABC):
    @abstractmethod
    def get_by_username(self, username: str) -> Optional[User]: ...

    @abstractmethod
    def add(self, name: str, username: str, password_hash: str, role: Role) -> User:
        """Levanta UserAlreadyExistsError se o username já existir."""

    @abstractmethod
    def update_password(self, user_id: int, password_hash: str) -> None: ...


class SessionRepository(ABC):
    @abstractmethod
    def add(self, token_hash: str, csrf_token: str, user_id: int, now: datetime,
            expires_at: datetime, user_agent: Optional[str]) -> None: ...

    @abstractmethod
    def find_by_token_hash(self, token_hash: str) -> Optional[Tuple[SessionRecord, User]]: ...

    @abstractmethod
    def touch(self, session_id: int, now: datetime) -> None: ...

    @abstractmethod
    def revoke(self, token_hash: str, now: datetime) -> None: ...

    @abstractmethod
    def revoke_all_for_user(self, user_id: int, now: datetime) -> None: ...

    @abstractmethod
    def delete_expired(self, before: datetime) -> int: ...


class ProductRepository(ABC):
    @abstractmethod
    def add(self, name: str, category: str, price: Decimal, is_active: bool = True) -> Product: ...

    @abstractmethod
    def get_by_id(self, product_id: int, for_update: bool = False) -> Optional[Product]: ...

    @abstractmethod
    def get_by_ids(self, product_ids: Sequence[int], for_update: bool = False) -> Dict[int, Product]: ...

    @abstractmethod
    def list_products(self, active_only: bool = False, search: Optional[str] = None,
                      category: Optional[str] = None) -> List[Product]: ...

    @abstractmethod
    def update(self, product_id: int, name: str, category: str, price: Decimal) -> Product: ...

    @abstractmethod
    def set_active(self, product_id: int, is_active: bool) -> Product: ...


class PaymentSettingsRepository(ABC):
    @abstractmethod
    def get(self, for_update: bool = False) -> PaymentSettings: ...

    @abstractmethod
    def update(self, card_fee_percent: Decimal, card_fee_mode: CardFeeMode,
               updated_by: int, now: datetime) -> PaymentSettings: ...


class ShiftRepository(ABC):
    @abstractmethod
    def get_open_shift(self, for_update: bool = False) -> Optional[Shift]: ...

    @abstractmethod
    def get_by_id(self, shift_id: int, for_update: bool = False) -> Optional[Shift]: ...

    @abstractmethod
    def create_open_shift(self, opened_by: int, now: datetime,
                          opening_cash: Decimal = Decimal("0.00")) -> Shift: ...

    @abstractmethod
    def close_shift(self, shift_id: int, closed_by: int, now: datetime,
                    counted_cash: Optional[Decimal] = None,
                    notes: Optional[str] = None) -> Shift: ...


class SaleRepository(ABC):
    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str, for_update: bool = False) -> Optional[Sale]: ...

    @abstractmethod
    def get_by_id(self, sale_id: int, for_update: bool = False) -> Optional[Sale]: ...

    @abstractmethod
    def list_recent(self, limit: int = 50, shift_id: Optional[int] = None) -> List[Sale]: ...

    @abstractmethod
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
    ) -> int: ...

    @abstractmethod
    def add_item(
        self,
        sale_id: int,
        product_id: Optional[int],
        item_kind: SaleItemKind,
        description: str,
        quantity: int,
        unit_price: Decimal,
        line_total: Decimal,
    ) -> SaleItem: ...

    @abstractmethod
    def mark_voided(self, sale_id: int, voided_by: int, voided_at: datetime,
                    reason: str, customer_refund_completed: bool = False,
                    customer_refund_method: Optional[PaymentMethod] = None,
                    customer_refund_notes: Optional[str] = None) -> None: ...

    @abstractmethod
    def mark_customer_refunded(self, sale_id: int, refunded_by: int, refunded_at: datetime,
                               method: PaymentMethod, notes: Optional[str] = None) -> None: ...


class CashMovementRepository(ABC):
    @abstractmethod
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
    ) -> CashMovement: ...

    @abstractmethod
    def list_by_sale(self, sale_id: int, for_update: bool = False) -> List[CashMovement]: ...

    @abstractmethod
    def get_financial_summary(self, shift_id: Optional[int] = None) -> dict: ...


class AuditLogRepository(ABC):
    @abstractmethod
    def add(self, operator_id: int, action: str, entity: str, entity_id: int,
            created_at: datetime, reason: Optional[str] = None,
            details: Optional[str] = None) -> AuditLogEntry: ...

    @abstractmethod
    def list_for_entity(self, entity: str, entity_id: int) -> List[AuditLogEntry]: ...


class UnitOfWork(ABC):
    """Fronteira transacional. Commit ao sair sem erro; rollback com erro."""
    users: UserRepository
    sessions: SessionRepository
    products: ProductRepository
    payment_settings: PaymentSettingsRepository
    shifts: ShiftRepository
    sales: SaleRepository
    cash_movements: CashMovementRepository
    audit_logs: AuditLogRepository

    @abstractmethod
    def __enter__(self) -> "UnitOfWork": ...

    @abstractmethod
    def __exit__(self, exc_type, exc, tb) -> bool: ...


class PasswordHasher(ABC):
    @abstractmethod
    def hash(self, password: str) -> str: ...

    @abstractmethod
    def verify(self, password: str, password_hash: str) -> bool: ...


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Data/hora atual em UTC, sem tzinfo (naive)."""
