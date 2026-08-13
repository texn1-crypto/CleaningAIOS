from __future__ import annotations

import email
import imaplib
import logging
import os
from email.header import decode_header, make_header
from email.policy import default
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import InboxMessage, SenderMailbox
from .notifications import queue_owner_notification


log = logging.getLogger("cleaningai.inbound_mail")


def _plain_body(message: email.message.EmailMessage) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")).lower():
                try:
                    return str(part.get_content())[:20_000]
                except (LookupError, UnicodeError):
                    continue
        return ""
    try:
        return str(message.get_content())[:20_000]
    except (LookupError, UnicodeError):
        return ""


def collect_mailbox_replies(db: Session, mailbox: SenderMailbox, *, limit: int = 20) -> dict:
    secret = os.environ.get(mailbox.imap_secret_ref, "") if mailbox.imap_secret_ref else ""
    username = mailbox.imap_username or mailbox.username or mailbox.address
    if not all([mailbox.inbound_enabled, mailbox.imap_host, username, secret]):
        return {"status": "credentials_required", "mailbox_id": mailbox.id, "received": 0}
    received = duplicates = 0
    with imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=20) as client:
        client.login(username, secret)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Unable to select IMAP inbox")
        status, data = client.uid("search", None, f"UID {mailbox.last_imap_uid + 1}:*")
        if status != "OK":
            raise RuntimeError("Unable to search IMAP inbox")
        uids = [int(value) for value in (data[0] or b"").split() if value.isdigit()]
        for uid in uids[-limit:]:
            status, fetched = client.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            message = email.message_from_bytes(fetched[0][1], policy=default)
            sender = parseaddr(str(message.get("From", "")))[1].lower()
            message_id = str(message.get("Message-ID") or f"imap:{mailbox.id}:{uid}")[:255]
            existing = db.scalar(
                select(InboxMessage.id).where(
                    InboxMessage.channel == "email",
                    InboxMessage.external_id == message_id,
                )
            )
            if existing:
                duplicates += 1
                mailbox.last_imap_uid = max(mailbox.last_imap_uid, uid)
                continue
            subject = str(make_header(decode_header(str(message.get("Subject", "")))))[:255]
            body = _plain_body(message)
            row = InboxMessage(
                channel="email",
                external_id=message_id,
                sender=sender,
                recipient=mailbox.address,
                subject=subject,
                body=body,
                data={"mailbox_id": mailbox.id, "imap_uid": uid, "forwarded_to_owner": True},
            )
            db.add(row)
            db.flush()
            queue_owner_notification(
                db,
                idempotency_key=f"inbound-email:{mailbox.id}:{uid}",
                channel="email",
                resource_type="inbox_message",
                resource_id=str(row.id),
                subject=f"Ответ на рассылку: {subject or '(без темы)'}",
                body=f"От: {sender}\nНа ящик: {mailbox.address}\n\n{body}",
                data={"inbox_message_id": row.id, "mailbox_id": mailbox.id, "imap_uid": uid},
            )
            mailbox.last_imap_uid = max(mailbox.last_imap_uid, uid)
            received += 1
        client.logout()
    db.flush()
    return {"status": "completed", "mailbox_id": mailbox.id, "received": received, "duplicates": duplicates}


def collect_inbound_replies(db: Session) -> dict:
    mailboxes = db.scalars(
        select(SenderMailbox).where(
            SenderMailbox.active.is_(True), SenderMailbox.inbound_enabled.is_(True)
        ).order_by(SenderMailbox.id)
    ).all()
    received = 0
    credentials_required = 0
    failed = 0
    for mailbox in mailboxes:
        try:
            with db.begin_nested():
                result = collect_mailbox_replies(db, mailbox)
            received += result["received"]
            credentials_required += result["status"] == "credentials_required"
        except Exception as exc:
            failed += 1
            log.warning("inbound mailbox %s failed: %s", mailbox.id, type(exc).__name__)
    db.commit()
    return {
        "mailboxes": len(mailboxes),
        "received": received,
        "credentials_required": credentials_required,
        "failed": failed,
    }
