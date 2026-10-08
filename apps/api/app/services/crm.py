"""Business-scoped pipeline, booking and billing, shared by people and bots."""
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from ..models import Appointment, Client, ClientPayment, Contact, Conversation, Message, Opportunity, UsageRecord, now_utc
from ..schemas_crm import AppointmentIn, BillingIn, CRMSettings, OpportunityIn
from .tools.specs import ToolSpec


def settings_for(client):
    return CRMSettings.model_validate(client.crm_settings or {})


def owned(db, model, client, identifier):
    row = db.scalar(select(model).where(model.id == identifier, model.client_id == client.id))
    if not row:
        raise HTTPException(404, "Record not found in this business")
    return row


def check_links(db, client, contact_id, conversation_id):
    contact = owned(db, Contact, client, contact_id) if contact_id else None
    if conversation_id:
        conversation = owned(db, Conversation, client, conversation_id)
        if contact_id and conversation.contact_id != contact_id:
            raise HTTPException(422, "The conversation belongs to another contact")
    return contact


def capture_lead(db, client, conversation):
    if not settings_for(client).auto_leads or not conversation.contact_id:
        return
    contact = db.get(Contact, conversation.contact_id)
    if contact and contact.blocked_at:
        return
    if not db.scalar(select(Opportunity.id).where(Opportunity.conversation_id == conversation.id)):
        db.add(Opportunity(client_id=client.id, conversation_id=conversation.id,
            contact_id=conversation.contact_id, title=conversation.title, stage="new"))
        db.flush()


def create_opportunity(db, client, payload):
    check_links(db, client, payload.contact_id, payload.conversation_id)
    if payload.conversation_id:
        existing = db.scalar(select(Opportunity).where(Opportunity.client_id == client.id,
            Opportunity.conversation_id == payload.conversation_id))
        if existing:
            for key in ("title", "stage", "value_cents", "notes"):
                setattr(existing, key, getattr(payload, key))
            db.flush()
            return existing
    row = Opportunity(client_id=client.id, **payload.model_dump())
    db.add(row)
    db.flush()
    return row


def interval(client, config, starts_at):
    if not config.appointments_enabled:
        raise HTTPException(409, "Appointments are disabled")
    start = starts_at.astimezone(ZoneInfo(client.timezone))
    end = start + timedelta(minutes=config.slot_minutes)
    opening = datetime.combine(start.date(), time.fromisoformat(config.opens_at), start.tzinfo)
    closing = datetime.combine(start.date(), time.fromisoformat(config.closes_at), start.tzinfo)
    offset = (start - opening).total_seconds() / 60
    if (start.weekday() not in config.weekdays or start < opening or end > closing
            or offset % config.slot_minutes or start <= now_utc()):
        raise HTTPException(422, "Choose a future slot within business hours")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def overlaps(db, client, resource, start, end):
    return db.scalar(select(Appointment.id).where(Appointment.client_id == client.id,
        Appointment.resource == resource, Appointment.status == "confirmed",
        Appointment.starts_at < end, Appointment.ends_at > start))


def slots(db, client, day, resource):
    config = settings_for(client)
    if resource not in config.resources:
        raise HTTPException(422, "Choose a configured resource")
    if not config.appointments_enabled or day.weekday() not in config.weekdays:
        return []
    if day < now_utc().astimezone(ZoneInfo(client.timezone)).date() or day > date.today() + timedelta(days=366):
        return []
    start = datetime.combine(day, time.fromisoformat(config.opens_at), ZoneInfo(client.timezone))
    closing = datetime.combine(day, time.fromisoformat(config.closes_at), start.tzinfo)
    result = []
    while start + timedelta(minutes=config.slot_minutes) <= closing:
        end = start + timedelta(minutes=config.slot_minutes)
        if start > now_utc() and not overlaps(db, client, resource, start, end):
            result.append({"starts_at": start.isoformat(), "ends_at": end.isoformat()})
        start = end
    return result


def book(db, client, payload):
    # ponytail: one row lock per business serializes bookings; use resource
    # rows if concurrent booking volume ever makes this a bottleneck.
    db.scalar(select(Client).where(Client.id == client.id).with_for_update().execution_options(populate_existing=True))
    config = settings_for(client)
    contact = check_links(db, client, payload.contact_id, payload.conversation_id)
    if payload.resource not in config.resources:
        raise HTTPException(422, "Choose a configured resource")
    start, end = interval(client, config, payload.starts_at)
    existing = db.scalar(select(Appointment).where(Appointment.client_id == client.id,
        Appointment.contact_id == contact.id, Appointment.resource == payload.resource,
        Appointment.starts_at == start, Appointment.status == "confirmed"))
    if existing:
        return existing
    if overlaps(db, client, payload.resource, start, end):
        raise HTTPException(409, "This slot is already booked")
    if payload.reminder and (not contact.whatsapp_opt_in_at or not payload.conversation_id):
        raise HTTPException(422, "A reminder requires recorded consent and a WhatsApp conversation")
    if payload.reminder:
        conversation = owned(db, Conversation, client, payload.conversation_id)
        if conversation.channel != "whatsapp_cloud":
            raise HTTPException(422, "Appointment reminders require the official WhatsApp API")
        if not config.reminder_text and not config.reminder_template:
            raise HTTPException(422, "Configure reminder text or an approved template first")
    row = Appointment(client_id=client.id, contact_id=contact.id, conversation_id=payload.conversation_id,
        title=payload.title, resource=payload.resource, starts_at=start, ends_at=end,
        reminder_due_at=max(now_utc(), start - timedelta(minutes=config.reminder_minutes)) if payload.reminder else None,
        reminder_status="pending" if payload.reminder else "disabled")
    db.add(row)
    db.flush()
    return row


def billing_summary(db, client, period=None):
    config = BillingIn.model_validate(client.billing or {})
    zone = ZoneInfo(client.timezone)
    period = period or now_utc().astimezone(zone).strftime("%Y-%m")
    beginning = datetime.fromisoformat(period + "-01").replace(tzinfo=zone)
    ending = (beginning.replace(day=28) + timedelta(days=4)).replace(day=1)
    messages = db.scalar(select(func.count(Message.id)).join(Conversation).where(
        Conversation.client_id == client.id, Conversation.channel != "playground", Message.kind == "message",
        Message.role == "assistant", Message.external_message_id.is_not(None),
        Message.created_at >= beginning, Message.created_at < ending)) or 0
    ai_cost = db.scalar(select(func.sum(UsageRecord.cost_usd)).where(UsageRecord.agent_id.in_(
        select(Conversation.agent_id).where(Conversation.client_id == client.id)),
        UsageRecord.created_at >= beginning, UsageRecord.created_at < ending)) or 0
    overage = max(0, messages - config.included_messages)
    # ponytail: current-plan estimates; versioned invoices when automated billing is added.
    due = config.monthly_cents + overage * config.overage_cents
    if period < config.starts_on.strftime("%Y-%m"):
        due = 0
    if period == config.starts_on.strftime("%Y-%m"):
        due += config.setup_cents
    payments = db.scalars(select(ClientPayment).where(ClientPayment.client_id == client.id,
        ClientPayment.period == period).order_by(ClientPayment.created_at.desc())).all()
    paid = sum(p.amount_cents for p in payments if p.currency == config.currency)
    return {"config": config.model_dump(mode="json"), "period": period, "messages": messages,
        "overage_messages": overage, "due_cents": due, "paid_cents": paid, "balance_cents": due - paid,
        "ai_cost_usd": float(ai_cost), "payments": payments,
        "quota_reserved": client.message_quota_used if client.message_quota_period == period else 0}


def require_automation(db, client):
    if not client.is_active:
        raise HTTPException(409, "The business is inactive")
    config = client.billing or {}
    if config.get("status") == "suspended":
        raise HTTPException(409, "Messaging is suspended for this business")
    if config.get("message_limit") and billing_summary(db, client)["messages"] >= config["message_limit"]:
        raise HTTPException(409, "The monthly outbound message limit has been reached")


def build_crm_specs(db, client, conversation):
    if not conversation.contact_id or not (settings_for(client).auto_leads or settings_for(client).appointments_enabled):
        return []
    config = settings_for(client)
    snapshot = db.scalar(select(Message.id).where(Message.conversation_id == conversation.id, Message.kind == "message")
        .order_by(Message.created_at.desc()).limit(1))

    def require_current_turn():
        db.refresh(conversation)
        current = db.scalar(select(Message.id).where(Message.conversation_id == conversation.id, Message.kind == "message")
            .order_by(Message.created_at.desc()).limit(1))
        if conversation.mode != "ai" or current != snapshot:
            raise HTTPException(409, "The conversation changed; wait for the next turn")

    properties = {"title": {"type": "string"}, "notes": {"type": "string"},
        "stage": {"type": "string", "enum": ["new", "qualified", "proposal", "won", "lost"]}}

    def qualify(args):
        try:
            require_current_turn()
            payload = OpportunityIn.model_validate({**args, "contact_id": conversation.contact_id,
                "conversation_id": conversation.id})
            row = create_opportunity(db, client, payload)
            return json.dumps({"id": str(row.id), "stage": row.stage}), False
        except (ValidationError, HTTPException) as exc:
            return str(exc), True

    def availability(args):
        try:
            return json.dumps(slots(db, client, date.fromisoformat(args["date"]), args["resource"])), False
        except (KeyError, ValueError, HTTPException) as exc:
            return str(exc), True

    def booking(args):
        try:
            require_current_turn()
            payload = AppointmentIn.model_validate({**args, "contact_id": conversation.contact_id,
                "conversation_id": conversation.id})
            row = book(db, client, payload)
            # Persist before telling the model the booking succeeded.
            db.commit()
            return json.dumps({"id": str(row.id), "starts_at": row.starts_at.isoformat(), "status": row.status}), False
        except (ValidationError, HTTPException) as exc:
            return str(exc), True

    def consent(args):
        try:
            require_current_turn()
            last = db.get(Message, snapshot)
            quote = args.get("quote", "")
            if (not last or last.sender_type != "visitor" or not isinstance(quote, str)
                    or not quote.strip() or quote.strip() != last.content.strip() or type(args.get("consent")) is not bool):
                raise HTTPException(422, "Quote the latest visitor message exactly; never infer consent")
            contact = owned(db, Contact, client, conversation.contact_id)
            contact.whatsapp_opt_in_at = now_utc() if args["consent"] else None
            contact.whatsapp_opt_in_source = f"WhatsApp {last.external_message_id or last.id}: {quote}"[:240]
            db.commit()
            return json.dumps({"consent": args["consent"]}), False
        except HTTPException as exc:
            return str(exc.detail), True

    specs = []
    if config.auto_leads:
        specs.append(ToolSpec("qualify_lead", "Save the customer's stated need and sales stage. Do not mark won without an explicit sale.",
            {"type": "object", "properties": properties, "required": ["title", "notes", "stage"]}, handler=qualify))
    if config.appointments_enabled:
        specs += [ToolSpec("appointment_slots", f"Read actual availability. Business timezone: {client.timezone}. Resources: {', '.join(config.resources)}. Never invent availability.",
            {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}, "resource": {"type": "string", "enum": config.resources}}, "required": ["date", "resource"]}, handler=availability),
            ToolSpec("book_appointment", "Book only after the customer explicitly confirms a returned slot. Set reminder only when prior recorded WhatsApp consent exists. Never claim success if this tool fails.",
                {"type": "object", "properties": {"title": {"type": "string"}, "resource": {"type": "string", "enum": config.resources}, "starts_at": {"type": "string", "description": "ISO timestamp including timezone from appointment_slots"}, "reminder": {"type": "boolean"}}, "required": ["title", "resource", "starts_at"]}, handler=booking)]
    if conversation.channel == "whatsapp_cloud":
        specs.append(ToolSpec("record_whatsapp_consent", "Record an explicit agreement or withdrawal for WhatsApp reminders and follow-ups. Ask separately: agreeing to an appointment or sending an inquiry is not consent. Quote the latest customer message exactly. If unclear, ask and do not record consent.",
            {"type": "object", "properties": {"consent": {"type": "boolean"}, "quote": {"type": "string"}},
                "required": ["consent", "quote"]}, handler=consent))
    return specs


async def run_reminders(db, limit=20):
    """Claim a reminder before sending. Ambiguous sends require human review."""
    from .reply_window import last_inbound_at
    from .whatsapp import send_channel_message
    from .whatsapp_templates import list_templates, rendered_text, send_components, send_template, template_credentials, window_is_open
    for _ in range(limit):
        row = db.scalar(select(Appointment).where(Appointment.reminder_status == "pending",
            Appointment.reminder_due_at <= now_utc()).order_by(Appointment.reminder_due_at)
            .with_for_update(skip_locked=True).limit(1))
        if not row:
            break
        client = db.get(Client, row.client_id)
        contact = db.get(Contact, row.contact_id) if row.contact_id else None
        conversation = db.get(Conversation, row.conversation_id) if row.conversation_id else None
        if (row.status != "confirmed" or row.starts_at <= now_utc() or not contact
                or not contact.whatsapp_opt_in_at or contact.blocked_at or not conversation
                or conversation.mode != "ai" or conversation.phone_pause_until is not None or not client.is_active):
            row.reminder_status = "skipped"
            row.reminder_error = "Cancelled, expired, consent withdrawn, blocked, or a person has taken over"
            db.commit()
            continue
        try:
            require_automation(db, client)
            config = settings_for(client)
            channel = conversation.whatsapp_cloud_channel
            if not channel or not channel.is_enabled or channel.status != "connected":
                raise HTTPException(409, "The WhatsApp line is disconnected")
            values = {"name": contact.name or contact.phone or "", "appointment": row.title,
                "date": row.starts_at.astimezone(ZoneInfo(client.timezone)).strftime("%Y-%m-%d %H:%M")}
            def render(value):
                for key, replacement in values.items():
                    value = value.replace("{" + key + "}", replacement)
                return value
            template = None
            if not window_is_open(last_inbound_at(db, conversation)) or not config.reminder_text:
                if not config.reminder_template:
                    raise HTTPException(409, "The reply window is closed and no reminder template is configured")
                token, waba = template_credentials(db, client, channel)
                template = next((item for item in await list_templates(token, waba)
                    if item["name"] == config.reminder_template and item["language"] == config.reminder_language
                    and item["status"] == "APPROVED" and item["category"] == "UTILITY"), None)
                if template is None:
                    raise HTTPException(409, "The reminder template must be approved as UTILITY")
                body_values = [render(value) for value in config.reminder_parameters]
                components = send_components(template, body_values=body_values)
                content = rendered_text(template, body_values=body_values)
            else:
                content = render(config.reminder_text)
            db.refresh(row)
            db.refresh(conversation)
            db.refresh(contact)
            if row.status != "confirmed" or conversation.mode != "ai" or not contact.whatsapp_opt_in_at:
                row.reminder_status = "skipped"
                db.commit()
                continue
            message = Message(conversation_id=conversation.id, role="assistant", sender_type="ai",
                sender_name="Appointment reminder", content=content, delivery_status="pending")
            db.add(message)
            db.flush()
            row.reminder_message_id = message.id
            row.reminder_status = "sending"
            # In this state, the next due time is the interrupted-send deadline.
            row.reminder_due_at = now_utc() + timedelta(minutes=10)
            db.commit()
            if template:
                external_id = await send_template(token, channel.phone_number_id, conversation.external_chat_id,
                    name=template["name"], language=template["language"], components=components)
            else:
                external_id = await send_channel_message(db, conversation, content)
            if not external_id:
                raise HTTPException(502, "No WhatsApp message ID was returned")
            message.external_message_id = external_id
            message.delivery_status = "sent"
            row.reminder_status = "sent"
            row.reminder_error = None
            db.commit()
        except Exception as exc:
            sending = row.reminder_status == "sending"
            db.rollback()
            row = db.get(Appointment, row.id)
            row.reminder_status = "uncertain" if sending else "failed"
            row.reminder_error = (str(exc.detail) if isinstance(exc, HTTPException) else "Reminder processing failed")[:400]
            db.commit()
    # A process interrupted after the send began cannot know whether Kapso
    # accepted it. Surface it for review; never automatically send it twice.
    stale = db.scalars(select(Appointment).where(Appointment.reminder_status == "sending",
        Appointment.reminder_due_at <= now_utc())).all()
    for row in stale:
        row.reminder_status = "uncertain"
        row.reminder_error = "Interrupted send; check WhatsApp before sending again"
    db.commit()


def reserve_kapso_message(phone_number_id):
    """Reserve the monthly quota atomically before every Kapso send.

    A failed or ambiguous attempt consumes a reservation. This conservative
    ceiling prevents retries and concurrent workers from exceeding the cap.
    Billing uses accepted message IDs, never reservations.
    """
    from ..database import new_session
    from ..models import WhatsAppCloudChannel
    with new_session() as db:
        channel = db.scalar(select(WhatsAppCloudChannel).where(
            WhatsAppCloudChannel.phone_number_id == phone_number_id, WhatsAppCloudChannel.provider == "kapso"))
        if not channel or not channel.is_enabled or channel.status != "connected":
            raise HTTPException(409, "The Kapso line is disconnected")
        client = db.scalar(select(Client).where(Client.id == channel.client_id).with_for_update())
        if not client.is_active or (client.billing or {}).get("status") == "suspended":
            raise HTTPException(409, "Messaging is suspended for this business")
        period = now_utc().astimezone(ZoneInfo(client.timezone)).strftime("%Y-%m")
        if client.message_quota_period != period:
            client.message_quota_period, client.message_quota_used = period, 0
        cap = (client.billing or {}).get("message_limit")
        if cap and client.message_quota_used >= cap:
            raise HTTPException(409, "The monthly Kapso message limit has been reached")
        client.message_quota_used += 1
        db.commit()


def require_user_capacity(db, client):
    from ..models import PortalUser
    db.scalar(select(Client).where(Client.id == client.id).with_for_update().execution_options(populate_existing=True))
    maximum = (client.billing or {}).get("max_users")
    count = db.scalar(select(func.count(PortalUser.id)).where(PortalUser.client_id == client.id, PortalUser.is_active.is_(True))) or 0
    if maximum and count >= maximum:
        raise HTTPException(409, "The plan's active user limit has been reached")
