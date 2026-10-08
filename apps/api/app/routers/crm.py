"""The same CRM workspace for agency staff and each business's portal."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from ..database import get_db
from ..deps import get_current_user
from ..models import Appointment, Client, ClientPayment, Contact, Conversation, KapsoWebhookEvent, Message, Opportunity, WhatsAppCloudChannel, now_utc
from ..schemas_crm import AppointmentIn, BillingIn, CRMSettings, ConsentIn, OpportunityIn, OpportunityUpdate, PaymentIn
from ..services import crm
from .kapso import agency_client
from .portal import _portal_client, require_permission
from .whatsapp_cloud import _public_channel


def row_out(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def workspace_router(prefix, client_dependency, permissions=()):
    router = APIRouter(prefix=prefix, tags=["Client CRM"])
    edit = list(permissions)

    @router.get("")
    def overview(client=Depends(client_dependency), db=Depends(get_db)):
        contacts = db.scalars(select(Contact).where(Contact.client_id == client.id)
            .order_by(Contact.updated_at.desc()).limit(500)).all()
        opportunities = db.scalars(select(Opportunity).where(Opportunity.client_id == client.id)
            .order_by(Opportunity.updated_at.desc()).limit(500)).all()
        appointments = db.scalars(select(Appointment).where(Appointment.client_id == client.id,
            Appointment.ends_at >= now_utc()).order_by(Appointment.starts_at).limit(500)).all()
        cloud = db.scalars(select(WhatsAppCloudChannel).where(WhatsAppCloudChannel.client_id == client.id)).all()
        events = db.scalars(select(KapsoWebhookEvent).join(WhatsAppCloudChannel).where(
            WhatsAppCloudChannel.client_id == client.id, KapsoWebhookEvent.state.in_(["failed", "uncertain"]))
            .order_by(KapsoWebhookEvent.created_at.desc()).limit(20)).all()
        return {"settings": crm.settings_for(client), "timezone": client.timezone,
            "contacts": [row_out(row) for row in contacts],
            "opportunities": [row_out(row) for row in opportunities],
            "appointments": [{**row_out(row), "delivery_status": db.get(Message, row.reminder_message_id).delivery_status
                if row.reminder_message_id and db.get(Message, row.reminder_message_id) else None} for row in appointments],
            "channels": [_public_channel(row) for row in cloud],
            "webhook_errors": [{"id": row.id, "state": row.state, "error": row.last_error,
                "created_at": row.created_at} for row in events]}

    @router.put("/settings", dependencies=edit)
    def settings(payload: CRMSettings, client=Depends(client_dependency), db=Depends(get_db)):
        client.crm_settings = payload.model_dump()
        db.commit()
        return payload

    @router.post("/opportunities", status_code=201)
    def opportunity(payload: OpportunityIn, client=Depends(client_dependency), db=Depends(get_db)):
        row = crm.create_opportunity(db, client, payload)
        db.commit()
        return row_out(row)

    @router.patch("/opportunities/{opportunity_id}")
    def update_opportunity(opportunity_id: uuid.UUID, payload: OpportunityUpdate, client=Depends(client_dependency), db=Depends(get_db)):
        row = crm.owned(db, Opportunity, client, opportunity_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is None:
                raise HTTPException(422, "Opportunity fields cannot be null")
            setattr(row, key, value)
        db.commit()
        return row_out(row)

    @router.get("/slots")
    def availability(day: date, resource: str = Query(max_length=80), client=Depends(client_dependency), db=Depends(get_db)):
        return crm.slots(db, client, day, resource)

    @router.post("/appointments", status_code=201)
    def appointment(payload: AppointmentIn, client=Depends(client_dependency), db=Depends(get_db)):
        row = crm.book(db, client, payload)
        db.commit()
        return row_out(row)

    @router.post("/appointments/{appointment_id}/cancel")
    def cancel(appointment_id: uuid.UUID, client=Depends(client_dependency), db=Depends(get_db)):
        row = crm.owned(db, Appointment, client, appointment_id)
        row.status = "cancelled"
        row.reminder_due_at = None
        if row.reminder_status == "pending":
            row.reminder_status = "cancelled"
        db.commit()
        return row_out(row)

    @router.put("/contacts/{contact_id}/consent")
    def consent(contact_id: uuid.UUID, payload: ConsentIn, client=Depends(client_dependency), db=Depends(get_db)):
        contact = crm.owned(db, Contact, client, contact_id)
        contact.whatsapp_opt_in_at = now_utc() if payload.consent else None
        contact.whatsapp_opt_in_source = payload.source
        db.commit()
        return row_out(contact)

    @router.get("/contacts/{contact_id}/conversations")
    def contact_conversations(contact_id: uuid.UUID, client=Depends(client_dependency), db=Depends(get_db)):
        crm.owned(db, Contact, client, contact_id)
        rows = db.scalars(select(Conversation).where(Conversation.client_id == client.id,
            Conversation.contact_id == contact_id, Conversation.channel == "whatsapp_cloud")
            .order_by(Conversation.updated_at.desc()).limit(30)).all()
        return [{"id": row.id, "title": row.title, "mode": row.mode, "status": row.status} for row in rows]

    @router.post("/webhook-events/{event_id}/retry", dependencies=edit)
    def retry_event(event_id: uuid.UUID, client=Depends(client_dependency), db=Depends(get_db)):
        event = db.scalar(select(KapsoWebhookEvent).join(WhatsAppCloudChannel).where(
            KapsoWebhookEvent.id == event_id, WhatsAppCloudChannel.client_id == client.id).with_for_update())
        if not event:
            raise HTTPException(404, "Event not found")
        if event.state != "failed":
            raise HTTPException(409, "Only failed processing can be retried; uncertain sends require delivery review")
        phase = event.payload.get("phase", "pending")
        if phase not in ("pending", "reply_due", "send_due"):
            raise HTTPException(409, "This event cannot be retried")
        event.state, event.attempts, event.available_at = phase, 0, now_utc()
        db.commit()
        return {"status": "queued"}

    return router


agency_router = workspace_router("/clients/{client_id}/crm", agency_client)
portal_router = workspace_router("/portal/{slug}/crm", _portal_client, [Depends(require_permission("crm.manage"))])
billing_router = APIRouter(prefix="/clients/{client_id}/billing", tags=["Manual billing"])


@billing_router.get("")
def billing(client=Depends(agency_client), db=Depends(get_db), period: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    return crm.billing_summary(db, client, period)


@billing_router.put("")
def configure_billing(payload: BillingIn, client=Depends(agency_client), db=Depends(get_db)):
    db.scalar(select(Client).where(Client.id == client.id).with_for_update().execution_options(populate_existing=True))
    if client.billing and payload.currency != client.billing.get("currency", "GTQ"):
        if db.scalar(select(ClientPayment.id).where(ClientPayment.client_id == client.id).limit(1)):
            raise HTTPException(409, "Currency cannot change after payments have been recorded")
    client.billing = payload.model_dump(mode="json")
    db.commit()
    return crm.billing_summary(db, client)


@billing_router.post("/payments", status_code=201)
def payment(payload: PaymentIn, client=Depends(agency_client), user=Depends(get_current_user), db=Depends(get_db)):
    db.scalar(select(Client).where(Client.id == client.id).with_for_update().execution_options(populate_existing=True))
    config = BillingIn.model_validate(client.billing or {})
    existing = db.scalar(select(ClientPayment).where(ClientPayment.client_id == client.id, ClientPayment.reference == payload.reference))
    if existing:
        if existing.amount_cents != payload.amount_cents or existing.period != payload.period:
            raise HTTPException(409, "This payment reference has already been recorded with different details")
        return row_out(existing)
    row = ClientPayment(client_id=client.id, recorded_by=user.id, currency=config.currency, **payload.model_dump())
    db.add(row)
    db.commit()
    return row_out(row)
