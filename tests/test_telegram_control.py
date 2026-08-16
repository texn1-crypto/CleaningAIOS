import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import httpx
from sqlalchemy import func, select


def _configure_owner(monkeypatch, *, user_id: int = 91001, chat_id: int = 92001):
    from app.config import settings

    monkeypatch.setattr(settings, "owner_telegram_id", str(user_id))
    monkeypatch.setattr(settings, "owner_telegram_chat_id", str(chat_id))
    monkeypatch.setattr(settings, "telegram_callback_secret", "telegram-test-secret")
    return user_id, chat_id


def _blocked_task(client, title: str):
    task = client.post(
        "/api/tasks",
        json={
            "title": title,
            "agent_type": "tender",
            "payload": {"action_kind": "tender_submission"},
        },
    ).json()
    blocked = client.post(f"/api/tasks/{task['id']}/run").json()
    assert blocked["status"] == "blocked"
    return task, blocked["result"]["approval_id"]


def _card(client, approval_id: int, user_id: int, chat_id: int):
    response = client.post(
        f"/api/telegram/control/approvals/{approval_id}/card",
        json={"user_id": user_id, "chat_id": chat_id, "minimum_role": "owner"},
    )
    assert response.status_code == 200
    return response.json()


def test_telegram_approval_list_excludes_expired_pending_rows(client, monkeypatch):
    from app.db import SessionLocal
    from app.models import ApprovalRequest

    owner_user, owner_chat = _configure_owner(monkeypatch)
    current = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        expired = ApprovalRequest(
            action_kind="social_publication",
            resource_type="test_batch",
            resource_id="expired-list-card",
            status="pending",
            rationale="Expired test approval",
            expires_at=current - timedelta(minutes=1),
        )
        actionable = ApprovalRequest(
            action_kind="social_publication",
            resource_type="test_batch",
            resource_id="actionable-list-card",
            status="pending",
            rationale="Actionable test approval",
            expires_at=current + timedelta(hours=1),
        )
        db.add_all([expired, actionable])
        db.commit()
        expired_id = expired.id
        actionable_id = actionable.id

    response = client.post(
        "/api/telegram/control/approvals",
        json={"user_id": owner_user, "chat_id": owner_chat, "minimum_role": "owner"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert actionable_id in {row["id"] for row in items}
    assert expired_id not in {row["id"] for row in items}
    assert all(
        set(row["callbacks"]) == {"approve", "reject", "request_changes"}
        for row in items
    )


def test_telegram_identity_is_exact_deny_by_default_and_pseudonymous(client, monkeypatch):
    from app.db import SessionLocal
    from app.models import AuditLog

    owner_user, owner_chat = _configure_owner(monkeypatch)
    denied_user, denied_chat = 93001, 94001
    denied = client.post(
        "/api/telegram/control/authorize",
        json={"user_id": denied_user, "chat_id": denied_chat, "minimum_role": "viewer"},
    )
    assert denied.status_code == 200
    assert denied.json() == {
        "authorized": False,
        "role": None,
        "subject": None,
        "reason": "identity_not_bound",
    }

    bound = client.put(
        "/api/telegram/control/identities",
        json={"user_id": denied_user, "chat_id": denied_chat, "role": "viewer"},
    )
    assert bound.status_code == 200
    assert bound.json()["subject"] == f"telegram:{denied_user}:{denied_chat}"
    assert client.post(
        "/api/telegram/control/authorize",
        json={"user_id": denied_user, "chat_id": denied_chat, "minimum_role": "viewer"},
    ).json()["authorized"] is True
    insufficient = client.post(
        "/api/telegram/control/authorize",
        json={"user_id": denied_user, "chat_id": denied_chat, "minimum_role": "operator"},
    ).json()
    assert insufficient["authorized"] is False
    assert insufficient["reason"] == "role_not_allowed"

    # The configured owner is bound to one exact private chat, not just a user ID.
    assert client.post(
        "/api/telegram/control/authorize",
        json={"user_id": owner_user, "chat_id": owner_chat + 1, "minimum_role": "owner"},
    ).json()["authorized"] is False

    with SessionLocal() as db:
        rows = db.scalars(
            select(AuditLog)
            .where(AuditLog.action.in_(["telegram.access_denied", "telegram.identity_bound"]))
            .order_by(AuditLog.id.desc())
            .limit(10)
        ).all()
        assert rows
        assert all("user_id" not in row.details and "chat_id" not in row.details for row in rows)
        assert all(
            row.resource_id != f"telegram:{denied_user}:{denied_chat}"
            for row in rows
        )
        assert all(
            row.actor != f"telegram:{denied_user}:{denied_chat}"
            for row in rows
        )


def test_non_owner_telegram_role_cannot_decide_approval(client, monkeypatch):
    from app.db import SessionLocal
    from app.models import ApprovalDecisionRecord

    owner_user, owner_chat = _configure_owner(monkeypatch)
    manager_user, manager_chat = 95001, 96001
    assert client.put(
        "/api/telegram/control/identities",
        json={"user_id": manager_user, "chat_id": manager_chat, "role": "manager"},
    ).status_code == 200
    _, approval_id = _blocked_task(client, "Telegram manager forbidden approval")
    token = _card(client, approval_id, owner_user, owner_chat)["callbacks"]["approve"]

    response = client.post(
        "/api/telegram/control/approval-decision",
        json={
            "user_id": manager_user,
            "chat_id": manager_chat,
            "callback_token": token,
        },
    )
    assert response.status_code == 403
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count())
            .select_from(ApprovalDecisionRecord)
            .where(ApprovalDecisionRecord.approval_id == approval_id)
        ) == 0


def test_telegram_update_persists_owner_decision_and_resumes_once(client, monkeypatch):
    from app import bot
    from app.db import SessionLocal
    from app.models import ApprovalDecisionRecord, ApprovalRequest, AuditLog, Task, TaskTransition

    owner_user, owner_chat = _configure_owner(monkeypatch)
    task, approval_id = _blocked_task(client, "Telegram end-to-end approval")
    card = _card(client, approval_id, owner_user, owner_chat)
    assert card["risk"] == "critical"
    assert card["rationale"]
    assert card["requested_by"]
    assert card["expires_at"]
    assert set(card["callbacks"]) == {"approve", "reject", "request_changes"}
    assert all(len(value.encode()) <= 64 for value in card["callbacks"].values())

    async def in_process_api(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    class Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append((value, kwargs))

    class Query:
        data = card["callbacks"]["approve"]

        def __init__(self):
            self.answered = False

        async def answer(self):
            self.answered = True

    class User:
        id = owner_user

    class Chat:
        id = owner_chat

    class Update:
        effective_message = Message()
        effective_user = User()
        effective_chat = Chat()
        callback_query = Query()

    class Context:
        user_data = {}

    monkeypatch.setattr(bot, "api", in_process_api)
    update = Update()
    asyncio.run(bot.callback(update, Context()))
    assert update.callback_query.answered is True
    assert "Решение записано" in update.effective_message.replies[-1][0]
    asyncio.run(bot.callback(update, Context()))
    assert "повторное действие не выполнялось" in update.effective_message.replies[-1][0]

    with SessionLocal() as db:
        approval = db.get(ApprovalRequest, approval_id)
        decision = db.scalar(
            select(ApprovalDecisionRecord).where(
                ApprovalDecisionRecord.approval_id == approval_id
            )
        )
        assert approval.status == "approved"
        assert decision.action == "approve"
        assert decision.channel == "telegram"
        assert decision.actor == "telegram-owner"
        assert db.get(Task, task["id"]).status == "queued"
        assert db.scalar(
            select(func.count())
            .select_from(TaskTransition)
            .where(
                TaskTransition.transition_key
                == f"task:{task['id']}:approval:{approval_id}:queued"
            )
        ) == 1
        assert db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "approval.approved",
                AuditLog.resource_id == str(task["id"]),
            )
        ) == 1


def test_forged_callback_is_rejected_without_secret_or_pii_in_logs(
    client, monkeypatch, caplog
):
    from app.db import SessionLocal
    from app.models import ApprovalDecisionRecord, AuditLog

    owner_user, owner_chat = _configure_owner(monkeypatch)
    _, approval_id = _blocked_task(client, "Forged Telegram callback")
    token = _card(client, approval_id, owner_user, owner_chat)["callbacks"]["approve"]
    replacement = "A" if token[-1] != "A" else "B"
    forged = token[:-1] + replacement

    caplog.set_level(logging.INFO)
    response = client.post(
        "/api/telegram/control/approval-decision",
        json={
            "user_id": owner_user,
            "chat_id": owner_chat,
            "callback_token": forged,
        },
    )
    assert response.status_code == 403
    assert forged not in caplog.text
    assert "telegram-test-secret" not in caplog.text
    assert str(owner_user) not in caplog.text
    assert str(owner_chat) not in caplog.text
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count())
            .select_from(ApprovalDecisionRecord)
            .where(ApprovalDecisionRecord.approval_id == approval_id)
        ) == 0
        rejection = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == "telegram.callback_rejected")
            .order_by(AuditLog.id.desc())
        )
        assert rejection.details == {"reason": "invalid_signature_or_format"}
        assert rejection.resource_id == ""


def test_expired_and_stale_signed_callbacks_are_rejected(client, monkeypatch):
    from app.db import SessionLocal
    from app.models import ApprovalDecisionRecord, ApprovalRequest
    from app.telegram_control import issue_callback_token

    owner_user, owner_chat = _configure_owner(monkeypatch)
    _, expired_id = _blocked_task(client, "Expired signed Telegram callback")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        approval = db.get(ApprovalRequest, expired_id)
        approval.expires_at = now - timedelta(seconds=1)
        token = issue_callback_token(approval, "approve", now=now - timedelta(hours=2))
        db.commit()
    expired = client.post(
        "/api/telegram/control/approval-decision",
        json={
            "user_id": owner_user,
            "chat_id": owner_chat,
            "callback_token": token,
        },
    )
    assert expired.status_code == 409

    _, stale_id = _blocked_task(client, "Stale signed Telegram callback")
    stale_token = _card(client, stale_id, owner_user, owner_chat)["callbacks"]["reject"]
    with SessionLocal() as db:
        approval = db.get(ApprovalRequest, stale_id)
        approval.decision_version += 1
        db.commit()
    stale = client.post(
        "/api/telegram/control/approval-decision",
        json={
            "user_id": owner_user,
            "chat_id": owner_chat,
            "callback_token": stale_token,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "Approval callback is stale"
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count())
            .select_from(ApprovalDecisionRecord)
            .where(ApprovalDecisionRecord.approval_id.in_([expired_id, stale_id]))
        ) == 0


def test_concurrent_approve_reject_has_one_terminal_decision(client):
    from app.approval_service import ApprovalConflict, decide_approval
    from app.db import SessionLocal
    from app.models import ApprovalDecisionRecord
    from app.security import Principal

    _, approval_id = _blocked_task(client, "Concurrent Telegram approval race")
    barrier = Barrier(2)

    def attempt(action):
        with SessionLocal() as db:
            barrier.wait()
            try:
                result = decide_approval(
                    db,
                    approval_id=approval_id,
                    action=action,
                    note=f"Concurrent {action}",
                    actor=Principal(subject="telegram-owner", role="owner"),
                    channel="telegram",
                    expected_version=1,
                )
                db.commit()
                return result["status"]
            except ApprovalConflict:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["approve", "reject"]))
    assert outcomes.count("conflict") == 1
    assert len(set(outcomes) & {"approved", "rejected"}) == 1
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count())
            .select_from(ApprovalDecisionRecord)
            .where(ApprovalDecisionRecord.approval_id == approval_id)
        ) == 1


def test_telegram_runtime_uses_polling_not_an_unverified_webhook():
    import inspect

    from app import bot

    source = inspect.getsource(bot.main)
    assert "run_polling" in source
    assert "run_webhook" not in source
    assert "drop_pending_updates=True" in source
