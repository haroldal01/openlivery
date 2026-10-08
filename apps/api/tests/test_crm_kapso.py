"""Regression checks for the customer-facing Kapso CRM path."""
import asyncio
import hashlib
import hmac
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import TestingSession
from test_whatsapp_cloud import _setup_channel, _webhook_payload
from app.config import get_settings
from app.models import Appointment, Client, Contact, Conversation, KapsoWebhookEvent, Message, Opportunity, WhatsAppCloudChannel, now_utc
from app.schemas_crm import AppointmentIn
from app.security import decrypt_secret, encrypt_secret
from app.services import crm, kapso, kapso_worker, whatsapp_cloud, whatsapp_inbound, whatsapp_templates
from app.services.ai import Completion


def configure(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "kapso_api_key", "server-only-key")
    monkeypatch.setattr(settings, "kapso_webhook_secret", "signed-events-secret")
    monkeypatch.setattr(settings, "frontend_url", "https://crm.example.com")


def test_hosted_setup_verifies_customer_and_keeps_key_server_side(authenticated_client, monkeypatch):
    client = authenticated_client
    customer, agent, _ = _setup_channel(client)
    configure(monkeypatch)
    number = {"phone_number_id": "222", "customer_id": "customer-1", "business_account_id": "waba-1",
        "display_phone_number": "+502 5309 4054", "status": "CONNECTED", "is_coexistence": True}
    async def platform(method, path, **kwargs):
        if path == "customers":
            if method == "GET":
                return {"data": []}
            assert kwargs["json"]["customer"]["external_customer_id"] == customer["id"]
            return {"data": {"id": "customer-1"}}
        if path.endswith("setup_links"):
            assert kwargs["json"]["setup_link"]["meta_billing_mode"] == "partner_managed"
            return {"data": {"id": "link-1", "url": "https://setup.kapso.ai/s/abc"}}
        if path == "whatsapp/phone_numbers":
            return {"data": [number]}
        if path.endswith("/funding"):
            return {"data": {"status": "funded", "paid_messages": {"paused": False}}}
        if method == "GET":
            return {"data": []}
        assert kwargs["json"]["whatsapp_webhook"]["kind"] == "meta"
        return {"data": {"id": "webhook-1"}}
    monkeypatch.setattr(kapso, "platform_request", platform)
    base = f"/api/clients/{customer['id']}/kapso"
    setup = client.post(base + "/setup-link", json={"agent_id": agent["id"]})
    assert setup.status_code == 200, setup.text
    identifier = setup.json()["channel_id"]
    # A number belonging to another Kapso customer is never accepted.
    number["customer_id"] = "another-customer"
    response = client.post(base + f"/channels/{identifier}/refresh", json={"phone_number_id": "222"})
    assert response.json()["status"] == "disconnected"
    number["customer_id"] = "customer-1"
    response = client.post(base + f"/channels/{identifier}/refresh", json={"phone_number_id": "222"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"
    assert response.json()["kapso_webhook_id"] == "webhook-1"
    assert response.json()["kapso_funding"]["status"] == "funded"
    assert "/api/public/kapso/" in response.json()["webhook_url"]
    assert "server-only-key" not in response.text
    with TestingSession() as db:
        assert decrypt_secret(db.get(WhatsAppCloudChannel, uuid.UUID(identifier)).encrypted_access_token) == "kapso:222"


def test_signed_webhook_lead_reply_deduplication_and_handoff(authenticated_client, monkeypatch):
    client = authenticated_client
    customer, agent, channel = _setup_channel(client)
    configure(monkeypatch)
    with TestingSession() as db:
        business = db.get(Client, uuid.UUID(customer["id"]))
        business.crm_settings = {"auto_leads": True}
        line = db.get(WhatsAppCloudChannel, uuid.UUID(channel["id"]))
        line.provider, line.status = "kapso", "connected"
        line.encrypted_access_token = encrypt_secret("kapso:111")
        db.commit()
    monkeypatch.setattr(whatsapp_inbound, "run_completion", AsyncMock(return_value=Completion("Welcome")))
    send = AsyncMock(return_value="outbound-1")
    monkeypatch.setattr(kapso_worker, "send_channel_message", send)
    monkeypatch.setattr(whatsapp_inbound, "_signal_read_and_typing", AsyncMock())
    monkeypatch.setattr(kapso_worker, "reply_delay_seconds", lambda agent: 0)
    payload = _webhook_payload([{"id": "inbound-1", "type": "text", "from": "50253094054", "text": {"body": "I need flowers"}}])
    raw = json.dumps(payload).encode()
    headers = {"x-webhook-signature": hmac.new(b"signed-events-secret", raw, hashlib.sha256).hexdigest()}
    url = f"/api/public/kapso/channels/{channel['id']}/webhook"
    assert client.post(url, content=raw).status_code == 401
    assert client.post(url, content=raw, headers=headers).status_code == 200
    assert client.post(url, content=raw, headers=headers).status_code == 200
    with TestingSession() as db:
        assert db.scalar(select(func.count(KapsoWebhookEvent.id))) == 1
        for _ in range(3):
            asyncio.run(kapso_worker.run_due(db))
        assert db.scalar(select(func.count(Message.id))) == 2
        assert db.scalar(select(func.count(Opportunity.id))) == 1
        conversation = db.scalar(select(Conversation))
        conversation.mode = "human"
        db.commit()
    assert send.await_count == 1
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "inbound-2"
    raw = json.dumps(payload).encode()
    headers["x-webhook-signature"] = hmac.new(b"signed-events-secret", raw, hashlib.sha256).hexdigest()
    assert client.post(url, content=raw, headers=headers).status_code == 200
    with TestingSession() as db:
        for _ in range(3):
            asyncio.run(kapso_worker.run_due(db))
        assert db.scalar(select(func.count(Message.id))) == 3
    assert send.await_count == 1
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999"
    raw = json.dumps(payload).encode()
    headers["x-webhook-signature"] = hmac.new(b"signed-events-secret", raw, hashlib.sha256).hexdigest()
    assert client.post(url, content=raw, headers=headers).status_code == 403
    raw = json.dumps({"entry": [None]}).encode()
    headers["x-webhook-signature"] = hmac.new(b"signed-events-secret", raw, hashlib.sha256).hexdigest()
    assert client.post(url, content=raw, headers=headers).status_code == 422


def test_booking_serializes_conflicts_and_checks_tenant_and_consent(authenticated_client):
    client = authenticated_client
    customer, agent, channel = _setup_channel(client)
    other = client.post("/api/clients", json={"name": "Other business"}).json()
    with TestingSession() as db:
        business = db.get(Client, uuid.UUID(customer["id"]))
        business.timezone = "America/Guatemala"
        business.crm_settings = {"appointments_enabled": True, "weekdays": list(range(7)), "resources": ["Desk"]}
        first = Contact(client_id=business.id, name="First", phone="50253094054")
        second = Contact(client_id=business.id, name="Second", phone="50255551111")
        stranger = Contact(client_id=uuid.UUID(other["id"]), name="Another business")
        db.add_all([first, second, stranger]); db.commit()
        ids = [first.id, second.id]
        stranger_id = stranger.id
    base = f"/api/clients/{customer['id']}/crm"
    day = (now_utc() + timedelta(days=2)).date().isoformat()
    slots = client.get(base + f"/slots?day={day}&resource=Desk").json()
    assert slots
    start = slots[0]["starts_at"]
    bad = {"title": "Consultation", "contact_id": str(stranger_id), "resource": "Desk", "starts_at": start}
    assert client.post(base + "/appointments", json=bad).status_code == 404
    bad["contact_id"] = str(ids[0]); bad["reminder"] = True
    assert client.post(base + "/appointments", json=bad).status_code == 422
    def reserve(contact_id):
        with TestingSession() as db:
            business = db.get(Client, uuid.UUID(customer["id"]))
            try:
                row = crm.book(db, business, AppointmentIn(title="Consultation", contact_id=contact_id,
                    resource="Desk", starts_at=start))
                db.commit()
                return str(row.id)
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ids))
    assert sum(value == 409 for value in results) == 1
    with TestingSession() as db:
        assert db.scalar(select(func.count(Appointment.id))) == 1


def test_manual_billing_quota_and_reminder_consent(authenticated_client, monkeypatch):
    client = authenticated_client
    customer, agent, channel = _setup_channel(client)
    base = f"/api/clients/{customer['id']}"
    period = now_utc().strftime("%Y-%m")
    response = client.put(base + "/billing", json={"plan_name": "Pilot", "monthly_cents": 50000,
        "setup_cents": 10000, "included_messages": 1, "overage_cents": 25, "message_limit": 1, "max_users": 1})
    assert response.status_code == 200, response.text
    assert response.json()["due_cents"] == 60000
    assert client.post(base + "/billing/payments", json={"amount_cents": 60000, "period": period, "reference": "Bank transfer"}).status_code == 201
    assert client.post(base + "/billing/payments", json={"amount_cents": 60000, "period": period, "reference": "Bank transfer"}).status_code == 201
    assert client.post(base + "/billing/payments", json={"amount_cents": 1, "period": period, "reference": "Bank transfer"}).status_code == 409
    assert len(client.get(base + "/billing").json()["payments"]) == 1
    assert client.get(base + "/billing").json()["balance_cents"] == 0
    with TestingSession() as db:
        line = db.get(WhatsAppCloudChannel, uuid.UUID(channel["id"]))
        line.provider, line.status = "kapso", "connected"
        business = db.get(Client, uuid.UUID(customer["id"]))
        business.crm_settings = {"reminder_text": "Your appointment"}
        contact = Contact(client_id=business.id, name="Visitor", phone="50253094054")
        db.add(contact); db.flush()
        conversation = Conversation(client_id=business.id, agency_id=business.agency_id, agent_id=uuid.UUID(agent["id"]),
            contact_id=contact.id, channel="whatsapp_cloud", whatsapp_cloud_channel_id=line.id,
            external_chat_id=contact.phone, mode="ai")
        db.add(conversation); db.flush()
        row = Appointment(client_id=business.id, contact_id=contact.id, conversation_id=conversation.id,
            title="Appointment", resource="General", starts_at=now_utc() + timedelta(hours=2), ends_at=now_utc() + timedelta(hours=3),
            reminder_due_at=now_utc() - timedelta(seconds=1), reminder_status="pending")
        db.add(row); db.commit()
        asyncio.run(crm.run_reminders(db))
        assert row.reminder_status == "skipped"
    crm.reserve_kapso_message("111")
    with pytest.raises(HTTPException) as error:
        crm.reserve_kapso_message("111")
    assert error.value.status_code == 409


def test_consent_tool_and_closed_window_reminder(authenticated_client, monkeypatch):
    customer, agent, channel = _setup_channel(authenticated_client)
    template = whatsapp_templates.normalize({"name": "appointment_reminder", "language": "es",
        "category": "UTILITY", "status": "APPROVED", "components": [{"type": "BODY", "text": "Reminder for {{1}}."}]})
    monkeypatch.setattr(whatsapp_templates, "list_templates", AsyncMock(return_value=[template]))
    send = AsyncMock(return_value="wamid.reminder")
    monkeypatch.setattr(whatsapp_templates, "send_template", send)
    with TestingSession() as db:
        business = db.get(Client, uuid.UUID(customer["id"]))
        business.crm_settings = {"appointments_enabled": True, "reminder_text": "Inside window only",
            "reminder_template": "appointment_reminder", "reminder_parameters": ["{name}"]}
        line = db.get(WhatsAppCloudChannel, uuid.UUID(channel["id"]))
        line.provider, line.status = "kapso", "connected"
        line.encrypted_access_token = encrypt_secret("kapso:111")
        contact = Contact(client_id=business.id, name="Visitor", phone="50253094054")
        db.add(contact); db.flush()
        conversation = Conversation(client_id=business.id, agency_id=business.agency_id, agent_id=uuid.UUID(agent["id"]),
            contact_id=contact.id, channel="whatsapp_cloud", whatsapp_cloud_channel_id=line.id,
            external_chat_id=contact.phone, mode="ai")
        db.add(conversation); db.flush()
        visitor = Message(conversation_id=conversation.id, role="user", sender_type="visitor",
            content="Yes, send me WhatsApp reminders.", created_at=now_utc() - timedelta(hours=25))
        db.add(visitor); db.commit()
        consent = next(spec for spec in crm.build_crm_specs(db, business, conversation) if spec.name == "record_whatsapp_consent")
        assert consent.handler({"consent": True, "quote": "Invented agreement"})[1] is True
        assert not contact.whatsapp_opt_in_at
        assert consent.handler({"consent": True, "quote": visitor.content})[1] is False
        assert contact.whatsapp_opt_in_at
        row = Appointment(client_id=business.id, contact_id=contact.id, conversation_id=conversation.id,
            title="Appointment", resource="General", starts_at=now_utc() + timedelta(hours=2), ends_at=now_utc() + timedelta(hours=3),
            reminder_due_at=now_utc() - timedelta(seconds=1), reminder_status="pending")
        db.add(row); db.commit()
        asyncio.run(crm.run_reminders(db))
        assert row.reminder_status == "sent"
        assert row.reminder_due_at > now_utc()
        assert db.get(Message, row.reminder_message_id).external_message_id == "wamid.reminder"
        assert send.await_args.kwargs["components"][0]["parameters"][0]["text"] == "Visitor"
        conversation.mode = "human"; db.commit()
        assert consent.handler({"consent": False, "quote": visitor.content})[1] is True


def test_portal_employee_scope_and_owner_controls(authenticated_client, monkeypatch):
    client = authenticated_client
    customer, agent, channel = _setup_channel(client)
    other = client.post("/api/clients", json={"name": "Another business"}).json()
    stranger = client.post(f"/api/clients/{other['id']}/crm/opportunities", json={"title": "Private sale"}).json()
    assert client.post(f"/api/clients/{customer['id']}/portal-users", json={"name": "Employee", "email": "employee@example.com", "password": "secure-portal", "role": "agent"}).status_code == 201
    assert client.patch(f"/api/clients/{customer['id']}/portal", json={"portal_enabled": True}).status_code == 200
    base = f"/api/portal/{customer['portal_slug']}"
    assert client.post(base + "/login", json={"email": "employee@example.com", "password": "secure-portal"}).status_code == 200
    assert client.get(base + "/crm").status_code == 200
    assert client.patch(base + f"/crm/opportunities/{stranger['id']}", json={"stage": "won"}).status_code == 404
    assert client.put(base + "/crm/settings", json={"auto_leads": True}).status_code == 403
    configure(monkeypatch)
    assert client.post(base + "/kapso/setup-link", json={"agent_id": agent["id"]}).status_code == 403
    # Existing authenticated sessions cannot reopen an inactive business.
    client.patch(f"/api/clients/{customer['id']}", json={"is_active": False})
    assert client.get(base + "/crm").status_code == 401


def test_kapso_media_uses_signed_download_and_blocks_other_hosts(authenticated_client, monkeypatch):
    configure(monkeypatch)
    requests = []
    async def request(method, url, token, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"download_url": "https://api.kapso.ai/meta/whatsapp/media_download?token=signed",
            "url": "https://lookaside.fbsbx.com/private", "mime_type": "image/png"}) if len(requests) == 1 else httpx.Response(200, content=b"image")
    real_request = whatsapp_cloud.graph_request
    monkeypatch.setattr(whatsapp_cloud, "graph_request", request)
    assert asyncio.run(whatsapp_cloud.fetch_media("kapso:111", "media-1")) == (b"image", "image/png")
    assert requests[0][1]["params"] == {"phone_number_id": "111"}
    assert requests[1][0].startswith("https://api.kapso.ai/")
    with pytest.raises(HTTPException):
        asyncio.run(real_request("GET", "https://attacker.example.com/file", "kapso:111"))
    with pytest.raises(HTTPException):
        asyncio.run(real_request("GET", "http://api.kapso.ai/file", "kapso:111"))
    _, agent, channel = _setup_channel(authenticated_client)
    assert authenticated_client.put("/api/whatsapp-cloud/channels/" + channel["id"],
        json={"agent_id": agent["id"], "access_token": "kapso:111"}).status_code == 422


def test_parallel_workers_claim_a_recovered_reply_once(authenticated_client, monkeypatch):
    customer, agent, channel = _setup_channel(authenticated_client)
    entered, release = Event(), Event()
    async def accept(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return "wamid.once"
    send = AsyncMock(side_effect=accept)
    monkeypatch.setattr(kapso_worker, "send_channel_message", send)
    with TestingSession() as db:
        conversation = Conversation(client_id=uuid.UUID(customer["id"]),
            agency_id=db.get(Client, uuid.UUID(customer["id"])).agency_id,
            agent_id=uuid.UUID(agent["id"]), channel="whatsapp_cloud", whatsapp_cloud_channel_id=uuid.UUID(channel["id"]),
            external_chat_id="50253094054", mode="ai")
        db.add(conversation); db.flush()
        message = Message(conversation_id=conversation.id, role="assistant", sender_type="ai", content="Welcome")
        db.add(message); db.flush()
        rows = [KapsoWebhookEvent(channel_id=uuid.UUID(channel["id"]), event_key=str(uuid.uuid4()),
            payload={"messages": [str(message.id)]}, state="send_due") for _ in range(2)]
        db.add_all(rows); db.commit()
        ids = [row.id for row in rows]
        message_id = message.id
    def deliver(identifier):
        with TestingSession() as db:
            asyncio.run(kapso_worker.deliver(db, db.get(KapsoWebhookEvent, identifier)))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(deliver, ids[0])
        try:
            assert entered.wait(5)
            executor.submit(deliver, ids[1]).result(timeout=5)
        finally:
            release.set()
        first.result(timeout=5)
    assert send.await_count == 1
    with TestingSession() as db:
        assert db.get(Message, message_id).external_message_id == "wamid.once"
