"""Durable raw-Meta intake and replies, using the existing WhatsApp pipeline."""
import uuid
from datetime import timedelta

from sqlalchemy import select

from ..models import Conversation, KapsoWebhookEvent, Message, MessageAttachment, WhatsAppCloudChannel, now_utc
from ..security import decrypt_secret
from .whatsapp_cloud import fetch_media
from .whatsapp_inbound import process_inbound, _reply_with_ai, reply_delay_seconds, send_reply_attachments
from .whatsapp import send_channel_message


async def intake(db, event):
    from ..routers.whatsapp_cloud_webhook import apply_incoming_reaction, parse_message, record_status
    from .whatsapp_coexistence import FIELDS, accept_change
    from .whatsapp_identity import contact_names
    channel = db.get(WhatsAppCloudChannel, event.channel_id)
    conversations = set()
    for entry in event.payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            if change.get("field") in FIELDS:
                accept_change(db, channel, change["field"], value, waba_id=str(entry.get("id") or ""))
                continue
            if change.get("field") != "messages":
                continue
            for status in value.get("statuses", []):
                record_status(db, channel, status)
            names = contact_names(value.get("contacts", []))
            for raw in value.get("messages", []):
                if raw.get("type") == "reaction":
                    apply_incoming_reaction(db, channel, raw)
                    continue
                inbound = parse_message(raw, names)
                if inbound is None:
                    continue
                if inbound.media_kind:
                    media_id = (raw.get(inbound.media_kind) or {}).get("id")
                    if media_id:
                        inbound.media_bytes, inbound.media_mime = await fetch_media(
                            decrypt_secret(channel.encrypted_access_token), media_id)
                result = await process_inbound(db, channel, inbound, conversation_channel="whatsapp_cloud",
                    channel_fk_field="whatsapp_cloud_channel_id", defer_reply=True)
                if result.conversation_id:
                    conversations.add(str(result.conversation_id))
    event.payload = {"conversations": sorted(conversations)}
    event.state = "reply_due" if conversations else "done"
    event.available_at = now_utc() + timedelta(seconds=reply_delay_seconds(channel.agent))
    db.commit()
    from .notifications import notify_needs_human
    for identifier in conversations:
        conversation = db.get(Conversation, uuid.UUID(identifier))
        if conversation.mode == "human" and not (conversation.contact and conversation.contact.blocked_at):
            await notify_needs_human(db, conversation, "New WhatsApp message")


async def reply(db, event):
    channel = db.get(WhatsAppCloudChannel, event.channel_id)
    later = db.scalars(select(KapsoWebhookEvent).where(KapsoWebhookEvent.channel_id == channel.id,
        KapsoWebhookEvent.state == "reply_due", KapsoWebhookEvent.id != event.id,
        KapsoWebhookEvent.available_at > now_utc())).all()
    burst = set(event.payload.get("conversations", []))
    due = [other.available_at for other in later if burst.intersection(other.payload.get("conversations", []))]
    if due:
        event.state = "reply_due"
        event.available_at = max(due)
        db.commit()
        return
    pending = []
    attachments = []
    for identifier in event.payload.get("conversations", []):
        conversation = db.get(Conversation, uuid.UUID(identifier))
        if (not conversation or conversation.mode != "ai" or conversation.status != "open"
                or conversation.phone_pause_until or not channel.is_enabled
                or (conversation.contact and conversation.contact.blocked_at)):
            continue
        last = db.scalar(select(Message).where(Message.conversation_id == conversation.id,
            Message.kind == "message", Message.is_historical.is_(False))
            .order_by(Message.created_at.desc()).limit(1))
        if not last:
            continue
        # A restarted worker reuses a generated reply whose send never began.
        if last.role == "assistant":
            visitor = db.scalar(select(Message).where(Message.conversation_id == conversation.id,
                Message.sender_type == "visitor").order_by(Message.created_at.desc()).limit(1))
            recovery = db.scalars(select(Message).where(Message.conversation_id == conversation.id,
                Message.sender_type == "ai", Message.external_message_id.is_(None), Message.delivery_status.is_(None),
                Message.created_at > visitor.created_at).order_by(Message.created_at)).all() if visitor else []
            for message in recovery:
                if db.scalar(select(MessageAttachment.id).where(MessageAttachment.message_id == message.id).limit(1)):
                    attachments.append(str(message.id))
                elif message.content.strip():
                    pending.append(str(message.id))
            continue
        result = await _reply_with_ai(db, channel, conversation, last.llm_content or last.content,
            expected_last_message_id=last.id)
        if result.outbound_message_id:
            pending.append(str(result.outbound_message_id))
        attachments.extend(result.attachment_message_ids)
        if channel.last_error:
            raise ValueError("Agent could not generate a reply; review the channel error")
    event.payload = {"messages": pending, "attachments": attachments}
    event.state = "send_due" if pending or attachments else "done"
    event.available_at = now_utc()
    db.commit()


async def deliver(db, event):
    # The send phase is persisted before the request. A process crash or
    # timeout after acceptance is surfaced as uncertain, never blindly retried.
    event.state = "sending"
    event.available_at = now_utc() + timedelta(minutes=5)
    db.commit()
    for identifier in event.payload.get("messages", []):
        message = db.scalar(select(Message).where(Message.id == uuid.UUID(identifier))
            .with_for_update().execution_options(populate_existing=True))
        conversation = db.get(Conversation, message.conversation_id) if message else None
        if not conversation or message.external_message_id or message.delivery_status is not None:
            db.commit()
            continue
        escalated = any(call.get("name") == "escalate_to_human" and not call.get("is_error") for call in message.tool_calls or [])
        if (conversation.mode != "ai" or conversation.phone_pause_until) and not escalated:
            message.delivery_status = "failed"
            message.delivery_error = "An operator took over before the reply was sent"
            db.commit()
            continue
        quoted = db.get(Message, message.quoted_message_id) if message.quoted_message_id else None
        # Several burst events may recover the same reply. Claim the message,
        # not just the event, before releasing its lock for the remote request.
        message.delivery_status = "pending"
        db.commit()
        external_id = await send_channel_message(db, conversation, message.content,
            quoted_external_id=quoted.external_message_id if quoted else None)
        if not external_id:
            raise ValueError("Kapso did not return a message ID")
        message.external_message_id = external_id
        message.delivery_status = "sent"
        db.commit()
    for identifier in event.payload.get("attachments", []):
        message = db.scalar(select(Message).where(Message.id == uuid.UUID(identifier))
            .with_for_update().execution_options(populate_existing=True))
        if message and not message.external_message_id and message.delivery_status is None:
            conversation = db.get(Conversation, message.conversation_id)
            if conversation.mode != "ai" or conversation.phone_pause_until:
                db.commit()
                continue
            message.delivery_status = "pending"
            db.commit()
            await send_reply_attachments(db, conversation, [identifier])
            if not message.external_message_id:
                raise ValueError("The attachment was not confirmed by WhatsApp")
            message.delivery_status = "sent"
            db.commit()
        else:
            db.commit()
    event.state = "done"
    db.commit()


async def run_due(db, limit=20):
    for _ in range(limit):
        event = db.scalar(select(KapsoWebhookEvent).where(
            KapsoWebhookEvent.state.in_(["pending", "reply_due", "send_due", "processing", "sending"]),
            KapsoWebhookEvent.available_at <= now_utc()).order_by(KapsoWebhookEvent.available_at)
            .with_for_update(skip_locked=True).limit(1))
        if not event:
            break
        if event.state == "sending":
            event.state = "uncertain"
            event.last_error = "Interrupted send; check WhatsApp before sending again"
            db.commit()
            continue
        phase = event.payload.get("phase") if event.state == "processing" else event.state
        event.state = "processing"
        event.payload = {**event.payload, "phase": phase}
        event.available_at = now_utc() + timedelta(minutes=15)
        event.attempts += 1
        db.commit()
        try:
            if phase == "pending":
                await intake(db, event)
            elif phase == "reply_due":
                await reply(db, event)
            else:
                await deliver(db, event)
        except Exception:
            db.rollback()
            event = db.get(KapsoWebhookEvent, event.id)
            uncertain = event.state == "sending"
            event.state = "uncertain" if uncertain else ("failed" if event.attempts >= 5 else phase)
            event.last_error = "Delivery is uncertain; check WhatsApp" if uncertain else "Processing failed; inspect API logs and channel errors"
            event.available_at = now_utc() + timedelta(seconds=30 * event.attempts)
            db.commit()
            import logging
            logging.getLogger(__name__).exception("Kapso event %s failed", event.id)
