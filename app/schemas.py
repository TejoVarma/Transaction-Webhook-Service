from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
)

from app.timeutils import format_utc

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


def _clean_currency(value):
    return value.strip().upper() if isinstance(value, str) else value


CurrencyCode = Annotated[
    str, BeforeValidator(_clean_currency), StringConstraints(pattern=r"^[A-Z]{3}$")
]


class WebhookPayload(BaseModel):
    transaction_id: Identifier
    source_account: Identifier
    destination_account: Identifier
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: CurrencyCode


class WebhookAck(BaseModel):
    status: str = "ACCEPTED"
    transaction_id: str
    duplicate: bool


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    status: str
    created_at: datetime
    processed_at: datetime | None

    @field_serializer("amount")
    def amount_as_number(self, amount: Decimal) -> float:
        return float(amount)

    @field_serializer("created_at", "processed_at")
    def as_utc_string(self, value: datetime | None) -> str | None:
        return format_utc(value) if value else None
