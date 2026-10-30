"""Composição das dependências (um único lugar que conhece os adaptadores)."""
from dataclasses import dataclass
from datetime import timedelta

from ...config import Config
from ...domain.ports import Clock, PasswordHasher
from ...use_cases.auth import AuthService
from ...use_cases.products import ProductService
from ...use_cases.sales import QuickSaleService
from ...use_cases.settings import PaymentSettingsService
from ...use_cases.users import UserAdminService
from ..db.database import Database, MariaDbUnitOfWork
from ..security.services import BcryptPasswordHasher, SystemClock


@dataclass
class Container:
    config: Config
    db: Database
    clock: Clock
    hasher: PasswordHasher
    auth: AuthService
    users: UserAdminService
    products: ProductService
    settings: PaymentSettingsService
    sales: QuickSaleService


def build_container(config: Config, clock: Clock = None, hasher: PasswordHasher = None) -> Container:
    db = Database(config)
    clock = clock or SystemClock()
    hasher = hasher or BcryptPasswordHasher()
    uow_factory = lambda: MariaDbUnitOfWork(db)  # noqa: E731
    return Container(
        config=config,
        db=db,
        clock=clock,
        hasher=hasher,
        auth=AuthService(
            uow_factory, hasher, clock,
            idle_timeout=timedelta(minutes=config.session_idle_minutes),
            absolute_timeout=timedelta(hours=config.session_absolute_hours),
        ),
        users=UserAdminService(uow_factory, hasher, clock),
        products=ProductService(uow_factory, clock),
        settings=PaymentSettingsService(uow_factory, clock),
        sales=QuickSaleService(uow_factory, clock),
    )
