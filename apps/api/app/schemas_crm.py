"""Validated CRM configuration, scheduling and manual billing inputs."""
import uuid
from datetime import date, time
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

Stage = Literal["new", "qualified", "proposal", "won", "lost"]


class CRMSettings(BaseModel):
    business_faq: str = Field(default="", max_length=10000)
    auto_leads: bool = False
    appointments_enabled: bool = False
    resources: list[str] = Field(default_factory=lambda: ["General"], min_length=1, max_length=20)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7)
    opens_at: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    closes_at: str = Field(default="17:00", pattern=r"^\d{2}:\d{2}$")
    slot_minutes: int = Field(default=30, ge=15, le=240)
    reminder_minutes: int = Field(default=60, ge=0, le=43200)
    reminder_text: str = Field(default="", max_length=4000)
    reminder_template: str = Field(default="", max_length=512, pattern=r"^[a-z0-9_]*$")
    reminder_language: str = Field(default="es", min_length=2, max_length=10)
    reminder_parameters: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_schedule(self):
        try:
            opening, closing = time.fromisoformat(self.opens_at), time.fromisoformat(self.closes_at)
        except ValueError as exc:
            raise ValueError("Use valid opening and closing times") from exc
        if closing <= opening or any(day < 0 or day > 6 for day in self.weekdays):
            raise ValueError("Use valid weekdays and a closing time after opening")
        self.resources = [name.strip() for name in self.resources]
        if any(not name or len(name) > 80 for name in self.resources) or len(set(self.resources)) != len(self.resources):
            raise ValueError("Resource names must be distinct and between 1 and 80 characters")
        if any(len(value) > 1024 for value in self.reminder_parameters):
            raise ValueError("Reminder parameters are limited to 1024 characters")
        return self


class OpportunityIn(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    contact_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    stage: Stage = "new"
    value_cents: int = Field(default=0, ge=0, le=1_000_000_000)
    notes: str = Field(default="", max_length=10000)


class OpportunityUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    stage: Stage | None = None
    value_cents: int | None = Field(default=None, ge=0, le=1_000_000_000)
    notes: str | None = Field(default=None, max_length=10000)


class AppointmentIn(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    contact_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    resource: str = Field(min_length=1, max_length=80)
    starts_at: AwareDatetime
    reminder: bool = False


class ConsentIn(BaseModel):
    consent: bool
    source: str = Field(min_length=1, max_length=240)


class BillingIn(BaseModel):
    plan_name: str = Field(default="", max_length=80)
    status: Literal["active", "suspended"] = "active"
    currency: Literal["GTQ", "USD"] = "GTQ"
    starts_on: date = Field(default_factory=date.today)
    setup_cents: int = Field(default=0, ge=0, le=100_000_000)
    monthly_cents: int = Field(default=0, ge=0, le=100_000_000)
    included_messages: int = Field(default=1000, ge=0, le=10_000_000)
    overage_cents: int = Field(default=0, ge=0, le=100000)
    message_limit: int | None = Field(default=None, ge=1, le=10_000_000)
    max_users: int | None = Field(default=None, ge=1, le=10000)


class PaymentIn(BaseModel):
    amount_cents: int = Field(ge=1, le=100_000_000)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    reference: str = Field(min_length=1, max_length=240)
