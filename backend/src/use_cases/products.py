"""Casos de uso de gerenciamento de produtos."""
from decimal import Decimal
from typing import Callable, List, Optional, Union

from ..domain.errors import InvalidInputError, PermissionDeniedError
from ..domain.finance import parse_money
from ..domain.models import Product, Role, User
from ..domain.ports import Clock, UnitOfWork


def _validate_product_name(name: str) -> str:
    if not isinstance(name, str):
        raise InvalidInputError("O nome do produto é obrigatório.")
    cleaned = " ".join(name.strip().split())
    if len(cleaned) < 2 or len(cleaned) > 120:
        raise InvalidInputError("O nome do produto deve ter entre 2 e 120 caracteres.")
    return cleaned


def _validate_category(category: Optional[str]) -> str:
    if category is None:
        return "Geral"
    if not isinstance(category, str):
        raise InvalidInputError("Categoria inválida.")
    cleaned = " ".join(category.strip().split())
    if not cleaned:
        return "Geral"
    if len(cleaned) > 80:
        raise InvalidInputError("A categoria deve ter no máximo 80 caracteres.")
    return cleaned


class ProductService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock):
        self._uow = uow_factory
        self._clock = clock

    @staticmethod
    def _require_admin(actor: User) -> None:
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem gerenciar produtos.")

    def create_product(
        self,
        actor: User,
        name: str,
        price: Union[str, int, Decimal],
        category: Optional[str] = "Geral",
        is_active: bool = True,
    ) -> Product:
        self._require_admin(actor)
        clean_name = _validate_product_name(name)
        clean_cat = _validate_category(category)
        clean_price = parse_money(price, field_name="preço", allow_zero=True)
        if not isinstance(is_active, bool):
            raise InvalidInputError("O campo 'is_active' deve ser booleano.")

        with self._uow() as uow:
            product = uow.products.add(clean_name, clean_cat, clean_price, is_active=is_active)
            uow.audit_logs.add(
                operator_id=actor.id,
                action="product_created",
                entity="product",
                entity_id=product.id,
                created_at=self._clock.now(),
                details=f"name={product.name} price={product.price} category={product.category} active={product.is_active}",
            )
            return product

    def update_product(
        self,
        actor: User,
        product_id: int,
        name: str,
        price: Union[str, int, Decimal],
        category: Optional[str] = "Geral",
    ) -> Product:
        self._require_admin(actor)
        clean_name = _validate_product_name(name)
        clean_cat = _validate_category(category)
        clean_price = parse_money(price, field_name="preço", allow_zero=True)

        with self._uow() as uow:
            product = uow.products.update(product_id, clean_name, clean_cat, clean_price)
            uow.audit_logs.add(
                operator_id=actor.id,
                action="product_updated",
                entity="product",
                entity_id=product.id,
                created_at=self._clock.now(),
                details=f"name={product.name} price={product.price} category={product.category}",
            )
            return product

    def set_product_active(self, actor: User, product_id: int, is_active: bool) -> Product:
        self._require_admin(actor)
        if not isinstance(is_active, bool):
            raise InvalidInputError("O campo 'is_active' deve ser booleano.")
        with self._uow() as uow:
            product = uow.products.set_active(product_id, is_active)
            uow.audit_logs.add(
                operator_id=actor.id,
                action="product_activated" if is_active else "product_deactivated",
                entity="product",
                entity_id=product.id,
                created_at=self._clock.now(),
                details=f"name={product.name} active={product.is_active}",
            )
            return product

    def list_products(
        self,
        active_only: bool = False,
        search: Optional[str] = None,
        category: Optional[str] = None,
    ) -> List[Product]:
        clean_search = search.strip() if isinstance(search, str) and search.strip() else None
        clean_cat = category.strip() if isinstance(category, str) and category.strip() else None
        with self._uow() as uow:
            return uow.products.list_products(
                active_only=active_only,
                search=clean_search,
                category=clean_cat,
            )
