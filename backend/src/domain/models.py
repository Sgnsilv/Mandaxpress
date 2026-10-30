"""Entidades de domínio (usuários, sessões, produtos, vendas e movimentos financeiros)."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional


class Role(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"


class PaymentMethod(str, Enum):
    PIX = "pix"
    CARD = "card"
    CASH = "cash"


class CardFeeMode(str, Enum):
    SIMPLE_ADDITION = "simple_addition"
    PASS_THROUGH = "pass_through"


class ShiftStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class TabStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class SaleKind(str, Enum):
    QUICK = "quick"
    TAB_ITEM = "tab_item"


class SaleStatus(str, Enum):
    ACTIVE = "active"
    VOIDED = "voided"


class CustomerRefundStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    COMPLETED = "completed"


class SaleItemKind(str, Enum):
    PRODUCT = "product"
    CUSTOM = "custom"


class CashMovementKind(str, Enum):
    SALE_PAYMENT = "sale_payment"
    CREDIT_DEPOSIT = "credit_deposit"
    CREDIT_REFUND = "credit_refund"
    REVERSAL = "reversal"


class CashDirection(str, Enum):
    IN = "in"
    OUT = "out"


@dataclass(frozen=True)
class User:
    id: int
    name: str
    username: str
    role: Role
    is_active: bool
    # repr=False: o hash nunca deve aparecer em logs ou tracebacks.
    password_hash: str = field(repr=False)


@dataclass(frozen=True)
class SessionRecord:
    id: int
    user_id: int
    csrf_token: str = field(repr=False)
    created_at: datetime = None  # type: ignore[assignment]
    last_seen_at: datetime = None  # type: ignore[assignment]
    expires_at: datetime = None  # type: ignore[assignment]
    revoked_at: Optional[datetime] = None


@dataclass(frozen=True)
class AuthContext:
    """Resultado de uma autenticação bem-sucedida."""
    user: User
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    category: str
    price: Decimal
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class PaymentSettings:
    card_fee_percent: Decimal
    card_fee_mode: CardFeeMode
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class Shift:
    id: int
    status: ShiftStatus
    opened_by: int
    opened_at: datetime
    opening_cash: Decimal
    closed_by: Optional[int] = None
    closed_at: Optional[datetime] = None
    counted_cash: Optional[Decimal] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class SaleItem:
    id: int
    sale_id: int
    product_id: Optional[int]
    item_kind: SaleItemKind
    description: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class CashMovement:
    id: int
    shift_id: int
    tab_id: Optional[int]
    sale_id: Optional[int]
    kind: CashMovementKind
    method: PaymentMethod
    direction: CashDirection
    amount: Decimal
    fee_percent: Decimal
    fee_mode: str
    fee_amount: Decimal
    gross_amount: Decimal
    cash_tendered: Optional[Decimal]
    change_given: Optional[Decimal]
    reverses_id: Optional[int]
    operator_id: int
    created_at: datetime
    idempotency_key: str
    note: Optional[str] = None
    batch_key: Optional[str] = None
    request_fingerprint: Optional[str] = None
    voided_by: Optional[int] = None
    voided_at: Optional[datetime] = None
    void_reason: Optional[str] = None
    customer_refund_status: CustomerRefundStatus = CustomerRefundStatus.NOT_APPLICABLE
    customer_refund_amount: Decimal = Decimal("0.00")
    customer_refunded_at: Optional[datetime] = None
    customer_refunded_by: Optional[int] = None
    customer_refund_method: Optional[PaymentMethod] = None
    customer_refund_notes: Optional[str] = None


@dataclass(frozen=True)
class Sale:
    id: int
    shift_id: int
    tab_id: Optional[int]
    kind: SaleKind
    status: SaleStatus
    subtotal: Decimal
    card_fee_total: Decimal
    total_charged: Decimal
    operator_id: int
    created_at: datetime
    idempotency_key: str
    request_fingerprint: str
    voided_by: Optional[int] = None
    voided_at: Optional[datetime] = None
    void_reason: Optional[str] = None
    customer_refund_status: CustomerRefundStatus = CustomerRefundStatus.NOT_APPLICABLE
    customer_refund_amount: Decimal = Decimal("0.00")
    customer_refunded_at: Optional[datetime] = None
    customer_refunded_by: Optional[int] = None
    customer_refund_method: Optional[PaymentMethod] = None
    customer_refund_notes: Optional[str] = None
    items: List[SaleItem] = field(default_factory=list)
    payments: List[CashMovement] = field(default_factory=list)


@dataclass(frozen=True)
class Tab:
    id: int
    shift_id: int
    number: int
    customer_name: Optional[str]
    status: TabStatus
    opened_by: int
    opened_at: datetime
    closed_by: Optional[int] = None
    closed_at: Optional[datetime] = None
    notes: Optional[str] = None
    subtotal_consumed: Decimal = Decimal("0.00")
    voided_subtotal: Decimal = Decimal("0.00")
    credit_deposited: Decimal = Decimal("0.00")
    credit_card_fee_paid: Decimal = Decimal("0.00")
    credit_applied: Decimal = Decimal("0.00")
    credit_refunded: Decimal = Decimal("0.00")
    credit_balance: Decimal = Decimal("0.00")
    principal_paid: Decimal = Decimal("0.00")
    net_principal_paid: Decimal = Decimal("0.00")
    sale_card_fee_paid: Decimal = Decimal("0.00")
    card_fee_paid: Decimal = Decimal("0.00")
    total_charged_paid: Decimal = Decimal("0.00")
    settled_subtotal: Decimal = Decimal("0.00")
    balance_due: Decimal = Decimal("0.00")
    pending_refund_total: Decimal = Decimal("0.00")
    completed_refund_total: Decimal = Decimal("0.00")
    launches: List[Sale] = field(default_factory=list)
    payments: List[CashMovement] = field(default_factory=list)
    credit_deposits: List[CashMovement] = field(default_factory=list)
    credit_refunds: List[CashMovement] = field(default_factory=list)

    @property
    def can_close(self) -> bool:
        return (
            self.status == TabStatus.OPEN
            and self.balance_due == Decimal("0.00")
            and self.credit_balance == Decimal("0.00")
            and self.pending_refund_total == Decimal("0.00")
            and self.net_principal_paid <= self.subtotal_consumed
        )

    @property
    def close_blockers(self) -> List[str]:
        blockers: List[str] = []
        if self.status != TabStatus.OPEN:
            blockers.append("already_closed")
            return blockers
        if self.pending_refund_total > Decimal("0.00"):
            blockers.append("customer_refund_pending")
        if self.balance_due > Decimal("0.00"):
            blockers.append("balance_due_pending")
        if self.credit_balance > Decimal("0.00"):
            blockers.append("credit_balance_pending")
        if self.net_principal_paid > self.subtotal_consumed and self.pending_refund_total == Decimal("0.00"):
            blockers.append("reconciliation_pending")
        return blockers


@dataclass(frozen=True)
class PaymentLinePreview:
    method: PaymentMethod
    amount: Decimal
    fee_percent: Decimal
    fee_mode: str
    fee_amount: Decimal
    gross_amount: Decimal
    cash_tendered: Optional[Decimal] = None
    change_given: Optional[Decimal] = None


@dataclass(frozen=True)
class AuditLogEntry:
    id: int
    operator_id: int
    action: str
    entity: str
    entity_id: int
    reason: Optional[str]
    details: Optional[str]
    created_at: datetime
