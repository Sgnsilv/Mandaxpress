"""Casos de uso de configuração administrativa (taxa de cartão)."""
from decimal import Decimal
from typing import Callable, Union

from ..domain.errors import InvalidInputError, PermissionDeniedError
from ..domain.finance import parse_percentage
from ..domain.models import CardFeeMode, PaymentSettings, Role, User
from ..domain.ports import Clock, UnitOfWork


class PaymentSettingsService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock):
        self._uow = uow_factory
        self._clock = clock

    def get_settings(self) -> PaymentSettings:
        with self._uow() as uow:
            return uow.payment_settings.get()

    def update_settings(
        self,
        actor: User,
        card_fee_percent: Union[str, int, Decimal],
        card_fee_mode: Union[str, CardFeeMode] = CardFeeMode.SIMPLE_ADDITION,
    ) -> PaymentSettings:
        if actor.role != Role.ADMIN:
            raise PermissionDeniedError("Apenas administradores podem alterar a configuração de taxa.")

        pct = parse_percentage(card_fee_percent, field_name="taxa do cartão")
        try:
            mode = card_fee_mode if isinstance(card_fee_mode, CardFeeMode) else CardFeeMode(str(card_fee_mode))
        except ValueError as exc:
            raise InvalidInputError("Modo de cálculo de taxa inválido.") from exc

        now = self._clock.now()
        with self._uow() as uow:
            settings = uow.payment_settings.update(
                card_fee_percent=pct,
                card_fee_mode=mode,
                updated_by=actor.id,
                now=now,
            )
            uow.audit_logs.add(
                operator_id=actor.id,
                action="payment_settings_updated",
                entity="payment_settings",
                entity_id=1,
                created_at=now,
                details=f"card_fee_percent={pct} card_fee_mode={mode.value}",
            )
            return settings
