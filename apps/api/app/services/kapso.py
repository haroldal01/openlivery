"""Hosted Kapso onboarding. Project credentials never leave the server."""
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from ..config import get_settings
from ..models import Client, WhatsAppCloudChannel, now_utc
from ..security import encrypt_secret


def require_configured():
    settings = get_settings()
    if not settings.kapso_api_key or not settings.kapso_webhook_secret:
        raise HTTPException(409, "Configure KAPSO_API_KEY and KAPSO_WEBHOOK_SECRET on the server first")
    return settings


async def platform_request(method, path, **kwargs):
    settings = require_configured()
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.request(method, settings.kapso_base_url.rstrip('/') + '/platform/v1/' + path,
                headers={"X-API-Key": settings.kapso_api_key}, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not reach Kapso") from exc
    if response.is_error:
        # Provider error bodies can contain credentials or customer details.
        raise HTTPException(502, f"Kapso rejected the operation (HTTP {response.status_code})")
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(502, "Kapso returned an invalid response") from exc


async def setup_link(db, client, channel, return_path, language):
    settings = require_configured()
    imported = "kapso_import_customer_id" in (channel.coexistence_sync or {})
    customer_id = (channel.coexistence_sync or {}).get("kapso_import_customer_id") if imported else client.kapso_customer_id
    if imported and not customer_id:
        raise HTTPException(409, "Reconnect this project number in Kapso, then verify the connection here")
    if not customer_id:
        customers = await platform_request("GET", "customers", params={"external_customer_id": str(client.id)})
        matches = [item for item in customers.get("data", []) if item.get("external_customer_id") == str(client.id)]
        if len(matches) > 1:
            raise HTTPException(409, "Multiple Kapso customers match this business; reconcile them before setup")
        if matches:
            client.kapso_customer_id = matches[0]["id"]
        else:
            result = await platform_request("POST", "customers", json={"customer": {
                "name": client.name, "external_customer_id": str(client.id)}})
            client.kapso_customer_id = result["data"]["id"]
        db.commit()
        customer_id = client.kapso_customer_id
    origin = settings.frontend_url.rstrip('/')
    if return_path.startswith("/portal/") and client.portal_domain and client.portal_domain_verified:
        origin = 'https://' + client.portal_domain
    options = {
        "meta_billing_mode": "partner_managed", "language": language,
        "allowed_connection_types": ["coexistence", "dedicated"],
        "success_redirect_url": origin + return_path,
        "failure_redirect_url": origin + return_path,
    }
    if channel.phone_number_id and channel.phone_number:
        options["reconnect_phone_number"] = '+' + ''.join(character for character in channel.phone_number if character.isdigit())
        options["allowed_connection_types"] = ["coexistence" if channel.coexistence else "dedicated"]
    result = await platform_request("POST", f"customers/{quote(customer_id, safe='')}/setup_links", json={"setup_link": options})
    data = result["data"]
    url = urlsplit(data["url"])
    if url.scheme != "https" or url.hostname not in {"setup.kapso.ai", "app.kapso.ai"}:
        raise HTTPException(502, "Kapso returned an unexpected setup URL")
    channel.kapso_setup_link_id = data["id"]
    db.commit()
    return {"url": data["url"], "expires_at": data.get("expires_at"), "channel_id": channel.id}


async def refresh_channel(db, channel, phone_number_id=None):
    client = db.get(Client, channel.client_id)
    if "kapso_import_customer_id" in (channel.coexistence_sync or {}):
        # Agency imports pin one project number and its remote customer. Portal
        # refreshes must never select another number from a shared customer.
        if phone_number_id and phone_number_id != channel.phone_number_id:
            raise HTTPException(409, "An imported line cannot switch phone numbers")
        number = (await platform_request("GET", f"whatsapp/phone_numbers/{channel.phone_number_id}"))["data"]
        if (str(number.get("phone_number_id") or number.get("id")) != channel.phone_number_id
                or number.get("customer_id") != channel.coexistence_sync["kapso_import_customer_id"]):
            channel.status = "error"
            channel.encrypted_access_token = None
            channel.kapso_webhook_id = None
            channel.last_error = "The number's Kapso ownership changed; the agency must review this line"
            db.commit()
            raise HTTPException(409, channel.last_error)
        numbers = [number]
    else:
        if not client.kapso_customer_id:
            raise HTTPException(409, "Create a WhatsApp setup link first")
        params = {"customer_id": client.kapso_customer_id, "per_page": 100}
        if phone_number_id or channel.phone_number_id:
            params["phone_number_id"] = phone_number_id or channel.phone_number_id
        result = await platform_request("GET", "whatsapp/phone_numbers", params=params)
        numbers = [number for number in result.get("data", []) if number.get("customer_id") == client.kapso_customer_id
            and (not params.get("phone_number_id") or str(number.get("phone_number_id") or number.get("id")) == params["phone_number_id"])]
    if not numbers:
        channel.status = "disconnected"
        db.commit()
        return channel
    if len(numbers) != 1:
        raise HTTPException(409, "Choose the phone number returned by the setup link")
    number = numbers[0]
    number_id = str(number.get("phone_number_id") or number["id"])
    if not number_id.isdigit():
        raise HTTPException(502, "Kapso returned an invalid phone number ID")
    duplicate = db.scalar(select(WhatsAppCloudChannel.id).where(
        WhatsAppCloudChannel.phone_number_id == number_id, WhatsAppCloudChannel.id != channel.id))
    if duplicate:
        raise HTTPException(409, "This number already belongs to another line")
    if channel.phone_number_id != number_id:
        channel.kapso_webhook_id = None
    channel.phone_number_id = number_id
    channel.waba_id = number.get("business_account_id")
    channel.phone_number = number.get("display_phone_number")
    channel.display_name = number.get("verified_name") or number.get("display_name")
    channel.coexistence = bool(number.get("is_coexistence"))
    channel.quality_rating = number.get("quality_rating")
    channel.messaging_limit = str(number.get("whatsapp_business_manager_messaging_limit") or "") or None
    connected = number.get("status") == "CONNECTED" and number.get("inbound_processing_enabled", True)
    # Messaging is enabled only after the signed webhook is reconciled.
    channel.status = "disconnected"
    # An encrypted routing marker reuses the Graph client without duplicating
    # the project key in customer rows or sending it through the browser.
    channel.encrypted_access_token = encrypt_secret("kapso:" + number_id)
    channel.updated_at = now_utc()
    channel.last_error = None
    db.commit()
    if connected:
        callback = require_configured().frontend_url.rstrip('/') + f"/api/public/kapso/channels/{channel.id}/webhook"
        if not callback.startswith("https://"):
            raise HTTPException(409, "Kapso webhooks require a public HTTPS FRONTEND_URL")
        # Reconcile first: a timeout after creation must not create duplicates.
        path = f"whatsapp/phone_numbers/{number_id}/webhooks"
        webhooks = await platform_request("GET", path)
        if any(item.get("kind") == "meta" and item.get("url") != callback for item in webhooks.get("data", [])):
            raise HTTPException(409, "This number already forwards Meta events to another application. Review its webhook in Kapso before connecting it here")
        webhook = next((item for item in webhooks.get("data", []) if item.get("url") == callback and item.get("kind") == "meta"), None)
        if webhook is None:
            webhook = (await platform_request("POST", path, json={"whatsapp_webhook": {
                "kind": "meta", "url": callback, "secret_key": require_configured().kapso_webhook_secret,
                "active": True}}))["data"]
        else:
            await platform_request("PATCH", path + '/' + quote(webhook["id"], safe=''), json={"whatsapp_webhook": {
                "active": True, "secret_key": require_configured().kapso_webhook_secret}})
        channel.kapso_webhook_id = webhook["id"]
        channel.status = "connected"
        channel.last_connected_at = now_utc()
        db.commit()
    if channel.waba_id:
        try:
            funding = (await platform_request("GET", f"whatsapp/accounts/{quote(channel.waba_id, safe='')}/funding"))["data"]
            snapshot = {"status": funding.get("status", "unknown"), "reason": funding.get("reason"),
                "paid_messages_paused": (funding.get("paid_messages") or {}).get("paused"),
                "last_checked_at": funding.get("last_checked_at")}
        except HTTPException:
            snapshot = {"status": "unknown"}
        channel.coexistence_sync = {**(channel.coexistence_sync or {}), "kapso_funding": snapshot}
        db.commit()
    return channel
