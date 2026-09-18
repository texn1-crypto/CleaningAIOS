from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import inbound_mail
from app.db import Base


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_concurrent_inbound_poll_returns_without_touching_imap(monkeypatch):
    monkeypatch.setattr(inbound_mail, "_acquire_poll_lock", lambda db: False)
    monkeypatch.setattr(
        inbound_mail,
        "collect_mailbox_replies",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("concurrent worker must not touch IMAP")
        ),
    )

    with _session_factory()() as db:
        result = inbound_mail.collect_inbound_replies(db)

    assert result == {
        "status": "already_running",
        "mailboxes": 0,
        "received": 0,
        "credentials_required": 0,
        "failed": 0,
    }
