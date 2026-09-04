from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import AgentRun
from app.observability import agent_utilization_snapshot, role_balance_recommendations


def test_agent_observability_reports_per_agent_slo_and_prometheus_metrics(client):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        db.add_all(
            [
                AgentRun(
                    agent_type="observability_test",
                    status="succeeded",
                    cost=0.25,
                    started_at=now - timedelta(seconds=10),
                    finished_at=now - timedelta(seconds=8),
                ),
                AgentRun(
                    agent_type="observability_test",
                    status="succeeded",
                    cost=0.5,
                    started_at=now - timedelta(seconds=20),
                    finished_at=now - timedelta(seconds=11),
                ),
                AgentRun(
                    agent_type="observability_test",
                    status="failed",
                    started_at=now - timedelta(seconds=30),
                    finished_at=now - timedelta(seconds=26),
                    error="synthetic failure",
                ),
                AgentRun(
                    agent_type="observability_stale_test",
                    status="running",
                    started_at=now - timedelta(minutes=31),
                ),
            ]
        )
        db.commit()

    response = client.get("/api/observability/agents?window_hours=24")
    assert response.status_code == 200
    result = response.json()
    observed = next(item for item in result["per_agent"] if item["agent_type"] == "observability_test")
    assert observed == {
        "agent_type": "observability_test",
        "completed": 3,
        "succeeded": 2,
        "failed": 1,
        "success_rate_percent": 66.67,
        "p95_duration_seconds": 9.0,
        "cost": 0.75,
    }
    assert result["runs"]["stale_running"] >= 1
    assert result["slo"]["stale_runs"]["status"] == "missed"
    assert result["slo"]["overall_status"] == "degraded"
    assert set(result["workflow"]) == {"tasks", "events", "pending_approvals"}

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    assert (
        'cleaningai_agent_runs{agent_type="observability_test",status="succeeded"} 2'
        in metrics.text
    )
    assert "cleaningai_agent_stale_runs" in metrics.text
    assert "synthetic failure" not in metrics.text


def test_agent_observability_reports_insufficient_data_honestly(client):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.observability import agent_observability_snapshot

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        result = agent_observability_snapshot(db, window_hours=1)

    assert result["window_hours"] == 1
    assert result["sample"]["max_runs"] == 10_000
    assert result["runs"]["success_rate_percent"] is None
    assert result["slo"]["success_rate"]["status"] == "insufficient_data"
    assert result["slo"]["p95_duration"]["status"] == "insufficient_data"
    assert result["slo"]["stale_runs"]["status"] == "met"
    assert result["slo"]["overall_status"] == "insufficient_data"


def test_agent_utilization_is_deterministic_aggregate_only_and_includes_idle_roles():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    now = datetime(2026, 9, 4, 12, 0, 0)
    with factory() as db:
        db.add_all(
            [
                AgentRun(
                    agent_type="sales",
                    status="succeeded",
                    input={"recipient": "private@example.test", "secret": "never-export"},
                    output={"message": "private output"},
                    started_at=now - timedelta(minutes=10),
                    finished_at=now - timedelta(minutes=9),
                ),
                AgentRun(
                    agent_type="sales",
                    status="failed",
                    error="SMTP password leaked sentinel",
                    started_at=now - timedelta(minutes=20),
                    finished_at=now - timedelta(minutes=19),
                ),
                AgentRun(
                    agent_type="marketing",
                    status="succeeded",
                    started_at=now - timedelta(minutes=30),
                    finished_at=now - timedelta(minutes=29),
                ),
                AgentRun(
                    agent_type="sales",
                    status="succeeded",
                    started_at=now - timedelta(minutes=61),
                    finished_at=now - timedelta(minutes=60),
                ),
            ]
        )
        db.commit()
        result = agent_utilization_snapshot(
            db,
            registered_agents=["sales", "marketing", "finance"],
            period_minutes=60,
            now=now,
        )

    by_agent = {item["agent_type"]: item for item in result["per_agent"]}
    assert result["totals"] == {
        "requests": 3,
        "succeeded": 2,
        "failed": 1,
        "running": 0,
        "idle_agents": 1,
    }
    assert by_agent["sales"] == {
        "agent_type": "sales",
        "request_count": 2,
        "requests_per_hour": 2.0,
        "succeeded": 1,
        "failed": 1,
        "running": 0,
        "failure_rate_percent": 50.0,
        "idle": False,
    }
    assert by_agent["finance"]["idle"] is True
    assert result["failure_hotspots"][0]["agent_type"] == "sales"
    assert "finance" in role_balance_recommendations(result)[1]
    serialized = repr(result)
    assert "private@example.test" not in serialized
    assert "never-export" not in serialized
    assert "SMTP password" not in serialized


def test_prometheus_metrics_require_authentication_in_production(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    response = client.get("/metrics")
    assert response.status_code == 401
