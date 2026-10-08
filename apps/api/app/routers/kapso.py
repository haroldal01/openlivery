"""Agency and business-owner access to hosted WhatsApp onboarding."""
import hashlib
import hmac
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import get_db
from ..deps import get_current_user
from ..models import Agent, Client, KapsoWebhookEvent, WhatsAppCloudChannel, new_uuid, now_utc
from ..services import kapso
from .portal import _portal_client, require_permission
from .whatsapp_cloud import _client_agent, _public_channel

router = APIRouter(tags=["Kapso"])
public_router = APIRouter(prefix="/public/kapso", tags=["Kapso public"])


class SetupIn(BaseModel):
    agent_id: uuid.UUID
    channel_id: uuid.UUID | None = None
    label: str = Field(default="", max_length=80)
    language: str = Field(default="es", pattern="^(en|es)$")


class RefreshIn(BaseModel):
    phone_number_id: str | None = Field(default=None, pattern=r"^\d{1,80}$")


class ImportIn(BaseModel):
    phone_number_id: str = Field(pattern=r"^[0-9]{1,80}$")
    agent_id: uuid.UUID
    label: str = Field(default="", max_length=80)


def agency_client(client_id: uuid.UUID, db=Depends(get_db), user=Depends(get_current_user)):
    client = db.scalar(select(Client).where(Client.id == client_id, Client.agency_id == user.agency_id))
    if not client:
        raise HTTPException(404, "Client not found")
    return client


@router.get("/clients/{client_id}/kapso/phone-numbers")
async def agency_numbers(page: int = Query(default=1, ge=1), client=Depends(agency_client), db=Depends(get_db)):
    result = await kapso.platform_request("GET", "whatsapp/phone_numbers", params={"page": page, "per_page": 100})
    assigned = {line.phone_number_id: line for line in db.scalars(select(WhatsAppCloudChannel)
        .where(WhatsAppCloudChannel.phone_number_id != "")).all()}
    numbers = []
    for number in result.get("data", []):
        identifier = str(number.get("phone_number_id") or number.get("id") or "")
        if not identifier.isascii() or not identifier.isdigit() or len(identifier) > 80:
            continue
        line = assigned.get(identifier)
        if line and (line.client_id != client.id or line.agency_id != client.agency_id or line.provider != "kapso"):
            continue
        # Return display fields only, never provider credentials or raw payloads.
        numbers.append({"phone_number_id": identifier, "display_phone_number": number.get("display_phone_number"),
            "display_name": number.get("verified_name") or number.get("display_name") or number.get("name"),
            "status": number.get("status"), "linked": bool(line)})
    pages = int((result.get("meta") or {}).get("total_pages") or 1)
    return {"data": numbers, "next_page": page + 1 if page < pages else None}


@router.post("/clients/{client_id}/kapso/import")
async def agency_import(payload: ImportIn, client=Depends(agency_client), db=Depends(get_db), user=Depends(get_current_user)):
    settings = kapso.require_configured()
    if not settings.frontend_url.startswith("https://"):
        raise HTTPException(409, "Kapso webhooks require a public HTTPS FRONTEND_URL")
    if not client.is_active:
        raise HTTPException(409, "The business is inactive")
    agent = _client_agent(db, user, client.id, payload.agent_id)
    number = (await kapso.platform_request("GET", f"whatsapp/phone_numbers/{payload.phone_number_id}"))["data"]
    if str(number.get("phone_number_id") or number.get("id")) != payload.phone_number_id:
        raise HTTPException(502, "Kapso returned a different phone number")
    channel = db.scalar(select(WhatsAppCloudChannel).where(WhatsAppCloudChannel.phone_number_id == payload.phone_number_id)
        .with_for_update())
    if channel and (channel.client_id != client.id or channel.agency_id != user.agency_id or channel.provider != "kapso"):
        raise HTTPException(409, "This number already belongs to another line")
    if not channel:
        # Keep the provider customer at line level: project numbers can share
        # a Kapso customer while belonging to different local businesses.
        channel = WhatsAppCloudChannel(agency_id=user.agency_id, client_id=client.id, agent_id=agent.id,
            provider="kapso", phone_number_id=payload.phone_number_id,
            coexistence_sync={"kapso_import_customer_id": number.get("customer_id")})
        db.add(channel)
    channel.agent_id = agent.id
    if payload.label:
        channel.label = payload.label
    try:
        # Save the stable line ID before registering a remote webhook. A failed
        # provider call can then be retried without leaving an orphan callback.
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "This number was just linked; refresh the list before retrying") from exc
    try:
        await kapso.refresh_channel(db, channel)
    except HTTPException as exc:
        channel.status = "error"
        channel.last_error = str(exc.detail)
        db.commit()
        raise
    return _public_channel(channel)


async def create_setup(db, client, payload, return_path):
    kapso.require_configured()
    if not client.is_active:
        raise HTTPException(409, "The business is inactive")
    # The row lock serializes customer creation and setup for this business.
    db.scalar(select(Client).where(Client.id == client.id).with_for_update().execution_options(populate_existing=True))
    agent = db.scalar(select(Agent).where(Agent.id == payload.agent_id, Agent.client_id == client.id,
        Agent.agency_id == client.agency_id, Agent.deleted_at.is_(None)))
    if not agent:
        raise HTTPException(422, "Choose an agent of this business")
    if payload.channel_id:
        channel = db.scalar(select(WhatsAppCloudChannel).where(WhatsAppCloudChannel.id == payload.channel_id,
            WhatsAppCloudChannel.client_id == client.id, WhatsAppCloudChannel.provider == "kapso"))
        if not channel:
            raise HTTPException(404, "Kapso line not found")
    else:
        channel = WhatsAppCloudChannel(agency_id=client.agency_id, client_id=client.id,
            agent_id=agent.id, provider="kapso", label=payload.label or None)
        db.add(channel)
        db.flush()
    channel.agent_id = agent.id
    return await kapso.setup_link(db, client, channel, return_path, payload.language)


@router.post("/clients/{client_id}/kapso/setup-link")
async def agency_setup(payload: SetupIn, client=Depends(agency_client), db=Depends(get_db)):
    return await create_setup(db, client, payload, f"/clients/{client.id}/channels/whatsapp-cloud",)


@router.post("/portal/{slug}/kapso/setup-link", dependencies=[Depends(require_permission("crm.manage"))])
async def portal_setup(slug: str, payload: SetupIn, client=Depends(_portal_client), db=Depends(get_db)):
    return await create_setup(db, client, payload, f"/portal/{slug}/crm")


async def refresh_owned(db, client, channel_id, payload):
    channel = db.scalar(select(WhatsAppCloudChannel).where(WhatsAppCloudChannel.id == channel_id,
        WhatsAppCloudChannel.client_id == client.id, WhatsAppCloudChannel.provider == "kapso"))
    if not channel:
        raise HTTPException(404, "Kapso line not found")
    await kapso.refresh_channel(db, channel, payload.phone_number_id)
    return _public_channel(channel)


@router.post("/clients/{client_id}/kapso/channels/{channel_id}/refresh")
async def agency_refresh(channel_id: uuid.UUID, payload: RefreshIn, client=Depends(agency_client), db=Depends(get_db)):
    return await refresh_owned(db, client, channel_id, payload)


@router.post("/portal/{slug}/kapso/channels/{channel_id}/refresh", dependencies=[Depends(require_permission("crm.manage"))])
async def portal_refresh(channel_id: uuid.UUID, payload: RefreshIn, client=Depends(_portal_client), db=Depends(get_db)):
    return await refresh_owned(db, client, channel_id, payload)


@public_router.post("/channels/{channel_id}/webhook")
async def webhook(channel_id: uuid.UUID, request: Request, db=Depends(get_db)):
    secret = get_settings().kapso_webhook_secret
    if not secret:
        raise HTTPException(503, "Kapso webhooks are not configured")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413, "Webhook too large")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("x-webhook-signature", ""), expected):
        raise HTTPException(401, "Invalid webhook signature")
    channel = db.get(WhatsAppCloudChannel, channel_id)
    if not channel or channel.provider != "kapso":
        raise HTTPException(404, "Kapso line not found")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entry"), list):
        raise HTTPException(422, "Expected a raw Meta webhook")
    # Phone-number webhooks are bound to exactly one line. Never use the
    # legacy Meta sibling routing for a Kapso customer.
    for entry in payload["entry"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
            raise HTTPException(422, "Invalid webhook entry")
        if channel.waba_id and str(entry.get("id") or "") != channel.waba_id:
            raise HTTPException(403, "Webhook account does not match this line")
        for change in entry.get("changes", []):
            if not isinstance(change, dict) or not isinstance(change.get("value"), dict):
                raise HTTPException(422, "Invalid webhook change")
            value = change["value"]
            metadata = value.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise HTTPException(422, "Invalid webhook metadata")
            for collection in ("messages", "statuses", "contacts"):
                items = value.get(collection, [])
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise HTTPException(422, "Invalid webhook collection")
            number = metadata.get("phone_number_id")
            if change.get("field") == "messages" and number != channel.phone_number_id:
                raise HTTPException(403, "Webhook number does not match this line")
    db.execute(insert(KapsoWebhookEvent).values(id=new_uuid(), channel_id=channel.id,
        event_key=hashlib.sha256(raw).hexdigest(), payload=payload, state="pending", attempts=0,
        available_at=now_utc(), created_at=now_utc()).on_conflict_do_nothing(constraint="uq_kapso_event"))
    db.commit()
    return {"status": "accepted"}
