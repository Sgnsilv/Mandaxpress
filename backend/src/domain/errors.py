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
