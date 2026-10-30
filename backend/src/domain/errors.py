"""Exceções de domínio. A camada web as converte em respostas HTTP."""


class DomainError(Exception):
    """Base de todos os erros esperados de negócio."""
    code = "domain_error"

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)
        self.message = message or self.code


class InvalidCredentialsError(DomainError):
    code = "invalid_credentials"


class NotAuthenticatedError(DomainError):
    code = "not_authenticated"


class PermissionDeniedError(DomainError):
    code = "forbidden"


class WeakPasswordError(DomainError):
    code = "weak_password"


class InvalidInputError(DomainError):
    code = "invalid_input"


class UserAlreadyExistsError(DomainError):
    code = "user_already_exists"


class UserNotFoundError(DomainError):
    code = "user_not_found"


class ProductAlreadyExistsError(DomainError):
    code = "product_already_exists"


class ProductNotFoundError(DomainError):
    code = "product_not_found"


class InactiveProductError(DomainError):
    code = "inactive_product"


class SaleNotFoundError(DomainError):
    code = "sale_not_found"


class IdempotencyConflictError(DomainError):
    code = "idempotency_conflict"


class SaleAlreadyVoidedError(DomainError):
    code = "sale_already_voided"


class ShiftClosedError(DomainError):
    code = "shift_closed"


class InvalidPaymentError(DomainError):
    code = "invalid_payment"


class TabNotFoundError(DomainError):
    code = "tab_not_found"


class TabAlreadyOpenError(DomainError):
    code = "tab_already_open"


class TabClosedError(DomainError):
    code = "tab_closed"


class TabBalancePendingError(DomainError):
    code = "tab_balance_pending"


class TabCreditBalancePendingError(DomainError):
    code = "tab_credit_balance_pending"


class InsufficientCreditError(DomainError):
    code = "insufficient_credit"


class TabIncompleteOperationsError(DomainError):
    code = "tab_incomplete_operations"

