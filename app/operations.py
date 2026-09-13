from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, BusinessGoal, BusinessRecord, OperatingEntity, Task
from .task_state import record_task_created


def validate_entity(db: Session, entity_type: str, parent_id: int | None, data: dict[str, Any]) -> None:
    parent = db.get(OperatingEntity, parent_id) if parent_id else None
    expected = {"site": "client", "contract": "site", "shift": "site", "complaint": "site"}
    if entity_type in expected and (not parent or parent.entity_type != expected[entity_type]):
        raise HTTPException(422, f"{entity_type} requires parent entity of type {expected[entity_type]}")
    if entity_type == "employee" and data.get("hourly_rate") is not None:
        try:
            if float(data["hourly_rate"]) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(422, "employee data.hourly_rate must be non-negative")
    if entity_type == "contract" and data.get("monthly_revenue") is None:
        raise HTTPException(422, "contract requires data.monthly_revenue")


def entity_view(row: OperatingEntity) -> dict[str, Any]:
    return {"id": row.id, "entity_type": row.entity_type, "name": row.name, "status": row.status, "parent_id": row.parent_id, "owner": row.owner, "data": row.data, "started_at": row.started_at, "ended_at": row.ended_at}


def business_graph(db: Session) -> dict[str, Any]:
    rows = db.scalars(select(OperatingEntity).order_by(OperatingEntity.id)).all()
    children: dict[int | None, list[OperatingEntity]] = {}
    for row in rows:
        children.setdefault(row.parent_id, []).append(row)

    def node(row: OperatingEntity) -> dict[str, Any]:
        return {**entity_view(row), "children": [node(child) for child in children.get(row.id, [])]}

    return {"roots": [node(row) for row in children.get(None, [])], "unlinked": [entity_view(x) for x in rows if x.parent_id is None and x.entity_type not in {"client", "employee", "vacancy"}]}


def site_economics(db: Session, site_id: int | None = None) -> list[dict[str, Any]]:
    sites_query = select(OperatingEntity).where(OperatingEntity.entity_type == "site")
    if site_id:
        sites_query = sites_query.where(OperatingEntity.id == site_id)
    sites = db.scalars(sites_query).all()
    entities = db.scalars(select(OperatingEntity)).all()
    result = []
    for site in sites:
        linked = [x for x in entities if x.parent_id == site.id]
        revenue = sum(float(x.data.get("monthly_revenue", 0) or 0) for x in linked if x.entity_type == "contract" and x.status == "active")
        payroll = sum(float(x.data.get("payroll_cost", 0) or 0) for x in linked if x.entity_type == "shift")
        materials = float(site.data.get("materials_cost", 0) or 0)
        logistics = float(site.data.get("logistics_cost", 0) or 0)
        penalties = float(site.data.get("penalties", 0) or 0)
        other = float(site.data.get("other_costs", 0) or 0)
        costs = payroll + materials + logistics + penalties + other
        profit = revenue - costs
        margin = round(profit / revenue * 100, 2) if revenue else 0
        complaints = sum(x.entity_type == "complaint" and x.status not in {"resolved", "closed"} for x in linked)
        result.append({"site_id": site.id, "site": site.name, "revenue": revenue, "payroll": payroll, "materials": materials, "logistics": logistics, "penalties": penalties, "other_costs": other, "profit": profit, "margin_percent": margin, "open_complaints": complaints})
    return result


def simulate_site(db: Session, site_id: int | None, revenue_change_percent: float, payroll_change_percent: float, materials_change_percent: float, penalty_change: float) -> dict[str, Any]:
    current_rows = site_economics(db, site_id)
    if site_id and not current_rows:
        raise HTTPException(404, "Site not found")
    current = {
        "revenue": sum(x["revenue"] for x in current_rows),
        "payroll": sum(x["payroll"] for x in current_rows),
        "materials": sum(x["materials"] for x in current_rows),
        "other": sum(x["logistics"] + x["other_costs"] for x in current_rows),
        "penalties": sum(x["penalties"] for x in current_rows),
    }

    def scenario(multiplier: float) -> dict[str, float]:
        revenue = current["revenue"] * (1 + revenue_change_percent * multiplier / 100)
        payroll = current["payroll"] * (1 + payroll_change_percent * multiplier / 100)
        materials = current["materials"] * (1 + materials_change_percent * multiplier / 100)
        penalties = max(0, current["penalties"] + penalty_change * multiplier)
        profit = revenue - payroll - materials - current["other"] - penalties
        return {"revenue": round(revenue, 2), "costs": round(payroll + materials + current["other"] + penalties, 2), "profit": round(profit, 2), "margin_percent": round(profit / revenue * 100, 2) if revenue else 0}

    return {"current": scenario(0), "conservative": scenario(0.7), "base": scenario(1), "optimistic": scenario(1.3), "assumptions": {"revenue_change_percent": revenue_change_percent, "payroll_change_percent": payroll_change_percent, "materials_change_percent": materials_change_percent, "penalty_change": penalty_change}}


def score_tender(data: dict[str, Any]) -> dict[str, Any]:
    factors = {
        "margin": (float(data.get("expected_margin", 0)), 25),
        "fit": (float(data.get("company_fit", 0)), 20),
        "competition": (100 - float(data.get("competition_risk", 50)), 15),
        "contract_risk": (100 - float(data.get("contract_risk", 50)), 15),
        "logistics": (float(data.get("logistics_fit", 0)), 10),
        "staffing": (float(data.get("staffing_fit", 0)), 10),
        "strategic_value": (float(data.get("strategic_value", 0)), 5),
    }
    breakdown = {name: round(max(0, min(100, value)) * weight / 100, 2) for name, (value, weight) in factors.items()}
    score = round(sum(breakdown.values()), 2)
    return {"score": score, "breakdown": breakdown, "recommendation": "prepare" if score >= 80 else "review" if score >= 60 else "skip"}


def goal_progress(row: BusinessGoal) -> dict[str, Any]:
    span = row.target - row.baseline
    progress = 100 if span == 0 and row.current >= row.target else (row.current - row.baseline) / span * 100 if span else 0
    return {"id": row.id, "title": row.title, "status": row.status, "owner": row.owner, "metric": row.metric, "baseline": row.baseline, "target": row.target, "current": row.current, "unit": row.unit, "progress_percent": round(max(0, min(100, progress)), 2), "deadline_at": row.deadline_at, "strategy": row.strategy}


def create_ceo_actions(db: Session) -> list[Task]:
    tasks: list[Task] = []
    economics = site_economics(db)
    for site in economics:
        if site["revenue"] and site["margin_percent"] < 15:
            tasks.append(Task(title=f"Recover margin at {site['site']}", agent_type="finance", priority="high", payload={"site_id": site["site_id"], "reason": "margin_below_15", "metrics": site}))
        if site["open_complaints"] >= 3:
            tasks.append(Task(title=f"Resolve complaint risk at {site['site']}", agent_type="hr", priority="high", payload={"site_id": site["site_id"], "reason": "complaints_threshold", "metrics": site}))
    overdue = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "payment", BusinessRecord.status == "overdue")).all()
    if overdue:
        tasks.append(Task(title="Recover overdue customer payments", agent_type="finance", priority="high", payload={"payment_ids": [x.id for x in overdue], "reason": "overdue_payments"}))
    unique: list[Task] = []
    for task in tasks:
        exists = db.scalar(select(Task.id).where(Task.title == task.title, Task.status.in_(["open", "queued", "running", "blocked"])))
        if not exists:
            db.add(task); unique.append(task)
    db.flush()
    for task in unique:
        record_task_created(db, task, actor="ceo", reason="deterministic_ceo_action")
    return unique


CEO_STRATEGY_VERSION = "2026.09-growth-v2"

CEO_GROWTH_MISSION = (
    "Привлекать квалифицированные лиды и превращать их в подтверждённую "
    "risk-adjusted прибыль, не ухудшая качество, законность и устойчивость бизнеса."
)

# Every registered role owns a measurable contribution to the same commercial
# funnel. Supporting roles are intentionally tied to profit through reliability,
# capacity or decision quality instead of being given artificial sales work.
CEO_AGENT_GROWTH_STRATEGIES: dict[str, dict[str, str]] = {
    "ceo": {
        "lead_stage": "portfolio_allocation",
        "profit_lever": "Капитал и внимание направляются в доказанно прибыльные сегменты.",
        "hypothesis": "Еженедельное ранжирование по ожидаемой risk-adjusted прибыли уменьшит потери фокуса.",
    },
    "orchestrator": {
        "lead_stage": "end_to_end_handoff",
        "profit_lever": "Сокращение времени и потерь между обнаружением лида, оценкой и следующим действием.",
        "hypothesis": "Контракт handoff с evidence и дедупликацией повысит долю лидов без потерянного следующего шага.",
    },
    "research": {
        "lead_stage": "demand_discovery",
        "profit_lever": "Больше проверяемых сигналов спроса в сегментах с подходящей экономикой.",
        "hypothesis": "Ранжирование официальных источников по выходу квалифицированных возможностей повысит полезность обхода.",
    },
    "tender": {
        "lead_stage": "tender_qualification",
        "profit_lever": "Отбор тендеров с выполнимыми условиями и положительной ожидаемой маржой.",
        "hypothesis": "Fail-closed decision passport до подготовки заявки уменьшит затраты на заведомо слабые тендеры.",
    },
    "sales": {
        "lead_stage": "qualification_and_conversion",
        "profit_lever": "Рост конверсии квалифицированных лидов в маржинальные договоры.",
        "hypothesis": "Next-best-action по причине потери и потенциалу маржи повысит подтверждённую ценность воронки.",
    },
    "marketing": {
        "lead_stage": "awareness_and_acquisition",
        "profit_lever": "Рост квалифицированных заявок из каналов с измеряемой стоимостью привлечения.",
        "hypothesis": "Один атрибутируемый эксперимент за цикл быстрее выявит каналы с положительной экономикой.",
    },
    "hr": {
        "lead_stage": "delivery_capacity",
        "profit_lever": "Готовность персонала позволяет принимать прибыльные контракты без срыва качества.",
        "hypothesis": "Прогноз дефицита смен до коммерческого предложения снизит риск штрафов и отказов от контрактов.",
    },
    "finance": {
        "lead_stage": "unit_economics_gate",
        "profit_lever": "Защита маржи, оборотного капитала и лимита допустимого риска.",
        "hypothesis": "Единый margin gate до предложения отсечёт выручку, которая разрушает денежный поток.",
    },
    "growth_officer": {
        "lead_stage": "segment_and_region_expansion",
        "profit_lever": "Масштабирование только воспроизводимых сегментов с положительной экономикой.",
        "hypothesis": "Портфель малых сегментных экспериментов выявит следующий рычаг роста дешевле широкого запуска.",
    },
    "meta_brain": {
        "lead_stage": "decision_quality_optimization",
        "profit_lever": "Повышение доли решений агентов, которые приводят к измеримому коммерческому результату.",
        "hypothesis": "Evals по outcome, а не по активности, сократят повторяющиеся ошибки и бесполезные задачи.",
    },
    "evolution_researcher": {
        "lead_stage": "capability_improvement",
        "profit_lever": "Внедрение только тех практик, чей ожидаемый эффект выше стоимости и риска изменения.",
        "hypothesis": "Проверка одной практики по первичным источникам и eval до внедрения повысит ROI разработки.",
    },
    "lead_scout": {
        "lead_stage": "public_lead_discovery",
        "profit_lever": "Прирост проверенных организаций из разрешённых публичных источников.",
        "hypothesis": "Оценка источников по доле новых дедуплицированных компаний повысит выход полезных лидов.",
    },
    "lead_coordinator": {
        "lead_stage": "lead_normalization_and_routing",
        "profit_lever": "Единая база без дублей и с provenance ускоряет квалификацию и безопасный handoff.",
        "hypothesis": "Контроль обязательных полей и дедупликации до маршрутизации уменьшит повторную ручную работу.",
    },
    "management_lead_scout": {
        "lead_stage": "management_company_discovery",
        "profit_lever": "Покрытие УК и ТСЖ с регулярной потребностью в клининге.",
        "hypothesis": "Сегментация по региону и масштабу объектов повысит долю релевантных УК и ТСЖ.",
    },
    "commercial_lead_scout": {
        "lead_stage": "commercial_property_discovery",
        "profit_lever": "Выявление БЦ, складов и ритейла с повторяемым объёмом уборки.",
        "hypothesis": "Приоритет объектов с регулярной эксплуатационной нагрузкой повысит ожидаемый LTV лида.",
    },
    "tender_lead_scout": {
        "lead_stage": "tender_signal_discovery",
        "profit_lever": "Раннее выявление закупочного спроса до истечения времени на качественную оценку.",
        "hypothesis": "Фильтр географии, сроков и предмета закупки повысит долю тендеров, пригодных для decision passport.",
    },
    "social_lead_scout": {
        "lead_stage": "public_demand_signal_discovery",
        "profit_lever": "Выявление открытых сигналов потребности без сбора частных данных и спама.",
        "hypothesis": "Фильтр явного коммерческого намерения повысит точность социальных сигналов спроса.",
    },
    "system_admin": {
        "lead_stage": "revenue_system_continuity",
        "profit_lever": "Доступность сбора, воронки и отчётов предотвращает потерю лидов и времени продаж.",
        "hypothesis": "Fingerprint инцидента и проверка восстановления снизят повторяемость revenue-impacting сбоев.",
    },
    "request_analyst": {
        "lead_stage": "owner_intent_to_execution",
        "profit_lever": "Полное превращение коммерческого намерения владельца в проверяемый результат.",
        "hypothesis": "Классификация execution gap до создания improvement уменьшит дубли и ускорит полезные изменения.",
    },
    "copywriter": {
        "lead_stage": "message_conversion",
        "profit_lever": "Повышение конверсии утверждённых коммерческих материалов без самовольной отправки.",
        "hypothesis": "Версионирование оффера по сегменту и возражению позволит измерять вклад текста в конверсию.",
    },
    "creative": {
        "lead_stage": "visual_conversion",
        "profit_lever": "Повторно используемые визуальные материалы ускоряют запуск проверяемых кампаний.",
        "hypothesis": "Единые шаблоны с visual QA снизят стоимость производства и улучшат согласованность материалов.",
    },
}

CEO_SELF_IMPROVEMENT_PROTOCOL = {
    "cycle": ["baseline", "hypothesis", "safe_experiment", "evidence", "keep_revise_or_stop"],
    "evidence_rule": "Решение меняется только по сохранённым outcome/evidence, а не по числу созданных задач.",
    "experiment_rule": "За цикл меняется одна проверяемая переменная; внешний эффект требует действующих guardrails.",
    "stop_rule": "Остановить или пересмотреть гипотезу при подтверждённом вреде, отрицательной экономике или повторном сбое.",
}


CEO_DEVELOPMENT_BACKLOG = (
    {
        "title": "CEO · Развитие сайта: аудит конверсии и контента",
        "agent_type": "marketing",
        "scope": "website",
        "horizon": "growth_12_months",
        "objective": "Превратить сайт в измеримый источник квалифицированных заявок.",
        "deliverable": "Аудит воронки сайта, контент-гипотезы и следующий безопасный эксперимент.",
        "success_metric": "qualified_website_leads",
    },
    {
        "title": "CEO · Продажи: анализ воронки и следующих действий",
        "agent_type": "sales",
        "scope": "sales",
        "horizon": "revenue_36_months",
        "objective": "Расти через прибыльные повторяемые продажи клининга.",
        "deliverable": "Проверенная воронка, причины потерь и следующие действия без автоматической рассылки.",
        "success_metric": "profitable_contract_pipeline_rub",
    },
    {
        "title": "CEO · Реклама: анализ каналов и маркетинговых гипотез",
        "agent_type": "marketing",
        "scope": "marketing",
        "horizon": "growth_12_months",
        "objective": "Находить масштабируемые каналы с положительной экономикой.",
        "deliverable": "Ранжированный список каналов и один измеримый эксперимент; расходы только после approval.",
        "success_metric": "qualified_leads_per_ruble",
    },
    {
        "title": "CEO · Система: анализ качества агентов и процессов",
        "agent_type": "meta_brain",
        "scope": "system",
        "horizon": "platform_36_months",
        "objective": "Повышать доказуемое качество решений агентов.",
        "deliverable": "Анализ evals, ошибок, использования ролей и одна проверяемая рекомендация.",
        "success_metric": "verified_agent_outcome_rate_percent",
    },
    {
        "title": "CEO · Оркестратор: надёжность сквозных процессов",
        "agent_type": "orchestrator",
        "scope": "workflow",
        "horizon": "platform_12_months",
        "objective": "Сделать сквозные процессы воспроизводимыми и наблюдаемыми.",
        "deliverable": "Проверка очереди, времени выполнения, идемпотентности и handoff между ролями.",
        "success_metric": "workflow_success_rate_percent",
    },
    {
        "title": "CEO · Исследования: источники тендеров и рынка",
        "agent_type": "research",
        "scope": "market_research",
        "horizon": "revenue_36_months",
        "objective": "Расширять подтверждённое знание о рынке и спросе.",
        "deliverable": "Проверка подключённых источников, качества данных и пробелов покрытия.",
        "success_metric": "verified_sources_and_opportunities",
    },
    {
        "title": "CEO · Тендеры: готовность Tender Autopilot",
        "agent_type": "tender",
        "scope": "tender_autopilot",
        "horizon": "revenue_36_months",
        "objective": "Сокращать время до безопасного решения об участии в прибыльном тендере.",
        "deliverable": "Проверка decision passport, документов, рисков и следующего пробела vertical slice.",
        "success_metric": "verified_profitable_tender_decisions",
    },
    {
        "title": "CEO · HR: готовность ресурсов к новым контрактам",
        "agent_type": "hr",
        "scope": "workforce",
        "horizon": "operations_24_months",
        "objective": "Обеспечивать рост без дефицита смен и снижения качества.",
        "deliverable": "Разрыв потребности и доступности персонала с планом подготовки; найм остаётся под approval.",
        "success_metric": "staffing_readiness_percent",
    },
    {
        "title": "CEO · Финансы: прибыльность и оборотный капитал",
        "agent_type": "finance",
        "scope": "finance",
        "horizon": "revenue_36_months",
        "objective": "Защищать маржу и денежный поток при масштабировании.",
        "deliverable": "Проверка маржи объектов, дебиторки и потребности в капитале без платежей.",
        "success_metric": "risk_adjusted_net_profit_rub",
    },
    {
        "title": "CEO · Стратегия: контроль портфеля развития",
        "agent_type": "ceo",
        "scope": "strategy",
        "horizon": "company_60_months",
        "objective": "Связывать работу всех ролей с прибыльными контрактами и устойчивостью системы.",
        "deliverable": "Проверка покрытия ролей, результатов, рисков и приоритетов следующего цикла.",
        "success_metric": "portfolio_verified_outcome_rate_percent",
    },
    {
        "title": "CEO · Рост: маршрут к целевой выручке",
        "agent_type": "growth_officer",
        "scope": "growth",
        "horizon": "company_60_months",
        "objective": "Построить поэтапный рост по регионам, сегментам и продуктам.",
        "deliverable": "Разрыв до целей, главные ограничения и следующий безопасный рычаг роста.",
        "success_metric": "annual_revenue_run_rate_rub",
    },
    {
        "title": "CEO · Эволюция: проверенные инженерные практики",
        "agent_type": "evolution_researcher",
        "scope": "product_evolution",
        "horizon": "platform_36_months",
        "objective": "Развивать систему по проверяемым первичным источникам без слепого копирования.",
        "deliverable": "Одна приоритизированная рекомендация с источником, риском и тест-планом.",
        "success_metric": "implemented_evidence_backed_improvements",
    },
    {
        "title": "CEO · Лиды: качество публично найденных компаний",
        "agent_type": "lead_scout",
        "scope": "lead_discovery",
        "horizon": "growth_12_months",
        "objective": "Находить релевантные организации из разрешённых публичных источников.",
        "deliverable": "Проверка качества, источников и регионального покрытия без автоматического контакта.",
        "success_metric": "verified_new_organization_leads",
    },
    {
        "title": "CEO · Координатор лидов: единая база и дедупликация",
        "agent_type": "lead_coordinator",
        "scope": "lead_coordination",
        "horizon": "growth_12_months",
        "objective": "Объединять результаты разведки в одну достоверную воронку.",
        "deliverable": "Контроль покрытия сегментов, дублей, provenance и доставки отчётов.",
        "success_metric": "deduplicated_verified_leads",
    },
    {
        "title": "CEO · УК и ТСЖ: разведка организаций",
        "agent_type": "management_lead_scout",
        "scope": "management_companies",
        "horizon": "growth_12_months",
        "objective": "Расширять базу УК и ТСЖ в целевых регионах.",
        "deliverable": "Проверка покрытия источников и качества организационных контактов.",
        "success_metric": "verified_management_company_leads",
    },
    {
        "title": "CEO · Коммерческая недвижимость: разведка объектов",
        "agent_type": "commercial_lead_scout",
        "scope": "commercial_property",
        "horizon": "growth_12_months",
        "objective": "Находить организации с регулярной потребностью в клининге.",
        "deliverable": "Проверка сегментов БЦ, складов, ритейла и публичных источников.",
        "success_metric": "verified_commercial_property_leads",
    },
    {
        "title": "CEO · Тендерные лиды: разведка спроса",
        "agent_type": "tender_lead_scout",
        "scope": "tender_leads",
        "horizon": "growth_12_months",
        "objective": "Выявлять проверяемый спрос на клининг до подготовки заявки.",
        "deliverable": "Проверка тендерных сигналов, источников и соответствия географии.",
        "success_metric": "verified_tender_leads",
    },
    {
        "title": "CEO · Социальные сигналы: разведка спроса",
        "agent_type": "social_lead_scout",
        "scope": "social_leads",
        "horizon": "growth_12_months",
        "objective": "Находить публичные сигналы потребности без сбора частных данных.",
        "deliverable": "Проверка разрешённых каналов, качества сигналов и источников.",
        "success_metric": "verified_public_demand_signals",
    },
    {
        "title": "CEO · Системный администратор: надёжность 24/7",
        "agent_type": "system_admin",
        "scope": "reliability",
        "horizon": "platform_12_months",
        "objective": "Сокращать время обнаружения, передачи и подтверждения исправления сбоев.",
        "deliverable": "Проверка health, зависших задач, ошибок доставки и improvement handoff.",
        "success_metric": "mean_time_to_verified_recovery_minutes",
    },
    {
        "title": "CEO · Аналитик запросов: невыполненные намерения владельца",
        "agent_type": "request_analyst",
        "scope": "request_quality",
        "horizon": "platform_12_months",
        "objective": "Снижать долю запросов, выполненных не полностью.",
        "deliverable": "Проверка классификаций, execution gaps и дедупликации улучшений.",
        "success_metric": "fully_completed_owner_requests_percent",
    },
    {
        "title": "CEO · Копирайтер: библиотека конверсионных материалов",
        "agent_type": "copywriter",
        "scope": "copy",
        "horizon": "growth_12_months",
        "objective": "Повышать качество коммерческих материалов без несанкционированной отправки.",
        "deliverable": "Проверка полноты шаблонов, доказательств и потребности в следующем материале.",
        "success_metric": "approved_content_conversion_rate_percent",
    },
    {
        "title": "CEO · Creative: масштабируемая визуальная система",
        "agent_type": "creative",
        "scope": "creative",
        "horizon": "growth_12_months",
        "objective": "Создавать единый визуальный стандарт для сайта, КП и соцсетей.",
        "deliverable": "Проверка готовности шаблонов, ассетов, хешей и visual-review evidence.",
        "success_metric": "approved_reusable_visual_assets",
    },
)


def execute_ceo_strategy_checkpoint(
    db: Session,
    *,
    task: Task,
) -> dict[str, Any]:
    """Produce a deterministic growth checkpoint with a closed learning loop."""
    previous = db.scalars(
        select(Task)
        .where(Task.agent_type == task.agent_type, Task.id != task.id)
        .order_by(Task.id.desc())
        .limit(100)
    ).all()
    previous_checkpoints = [
        row
        for row in previous
        if (row.payload or {}).get("origin") == "ceo_continuous_backlog"
    ]
    terminal_candidates = [
        row
        for row in previous
        if row.status in {"done", "failed", "blocked"}
        and (row.payload or {}).get("origin") != "ceo_continuous_backlog"
    ][:20]
    approval_waiting = [
        row.id
        for row in terminal_candidates
        if row.status == "blocked"
        and (row.result or {}).get("approval_id")
        and (row.result or {}).get("reason") == "owner_approval_required"
    ]
    terminal = [row for row in terminal_candidates if row.id not in approval_waiting]
    failed = [row.id for row in terminal if row.status == "failed"]
    blocked = [row.id for row in terminal if row.status == "blocked"]
    completed = [row.id for row in terminal if row.status == "done"]
    measured = len(terminal)
    completion_rate = round(len(completed) * 100 / measured, 2) if measured else None
    if failed or blocked:
        state = "at_risk"
        strategy_decision = "repair_before_next_experiment"
    elif approval_waiting and not measured:
        state = "waiting_owner_approval"
        strategy_decision = "hold_for_owner_approval"
    elif not measured:
        state = "baseline"
        strategy_decision = "establish_baseline"
    elif completion_rate is not None and completion_rate >= 80:
        state = "operating"
        strategy_decision = "keep_and_test_next_hypothesis"
    else:
        state = "needs_revision"
        strategy_decision = "revise_one_variable"
    payload = task.payload or {}
    previous_decision = None
    if previous_checkpoints:
        previous_decision = (previous_checkpoints[0].result or {}).get("strategy_decision")
    next_experiment = str(payload.get("deliverable") or "Выполнить следующий проверяемый шаг.")
    return {
        "status": state,
        "strategy_version": payload.get("strategy_version"),
        "business_mission": payload.get("business_mission"),
        "scope": payload.get("scope"),
        "horizon": payload.get("horizon"),
        "objective": payload.get("objective"),
        "deliverable": payload.get("deliverable"),
        "success_metric": payload.get("success_metric"),
        "lead_stage": payload.get("lead_stage"),
        "profit_lever": payload.get("profit_lever"),
        "strategy_hypothesis": payload.get("strategy_hypothesis"),
        "strategy_decision": strategy_decision,
        "previous_strategy_decision": previous_decision,
        "completed_task_count": len(completed),
        "failed_task_count": len(failed),
        "blocked_task_count": len(blocked),
        "owner_approval_waiting_task_count": len(approval_waiting),
        "measured_task_count": measured,
        "completion_rate_percent": completion_rate,
        "next_action": (
            "Передать подтверждённые сбои системному администратору и дождаться повторной проверки."
            if state == "at_risk"
            else next_experiment
        ),
        "self_improvement": {
            "protocol": payload.get("self_improvement_protocol"),
            "observation_task_ids": [row.id for row in terminal],
            "owner_approval_waiting_task_ids": approval_waiting,
            "baseline_completion_rate_percent": completion_rate,
            "hypothesis": payload.get("strategy_hypothesis"),
            "decision": strategy_decision,
            "next_safe_experiment": next_experiment,
        },
        "external_actions_executed": False,
        "evidence": [{
            "type": "ceo_strategy_checkpoint",
            "agent_type": task.agent_type,
            "inspected_task_ids": [row.id for row in terminal],
            "completed": len(completed),
            "failed": len(failed),
            "blocked": len(blocked),
            "owner_approval_waiting": len(approval_waiting),
            "completion_rate_percent": completion_rate,
            "strategy_decision": strategy_decision,
            "lead_stage": payload.get("lead_stage"),
            "profit_lever": payload.get("profit_lever"),
        }],
    }


def review_ceo_strategy_portfolio(
    db: Session,
    *,
    cycle_key: str,
) -> dict[str, Any]:
    """Verify portfolio coverage and hand technical risks to system administration."""
    expected_agents = sorted({str(item["agent_type"]) for item in CEO_DEVELOPMENT_BACKLOG})
    expected_titles = {str(item["title"]): str(item["agent_type"]) for item in CEO_DEVELOPMENT_BACKLOG}
    portfolio_tasks = [
        row
        for row in db.scalars(select(Task).order_by(Task.id.desc())).all()
        if (row.payload or {}).get("origin") == "ceo_continuous_backlog"
        and (row.payload or {}).get("strategy_version") == CEO_STRATEGY_VERSION
    ]
    latest_by_title: dict[str, Task] = {}
    for row in portfolio_tasks:
        latest_by_title.setdefault(row.title, row)
    missing_titles = sorted(set(expected_titles) - set(latest_by_title))
    missing = sorted({expected_titles[title] for title in missing_titles})
    def has_growth_evidence(row: Task) -> bool:
        result = row.result or {}
        learning = result.get("self_improvement")
        return bool(
            result.get("evidence")
            and result.get("lead_stage")
            and result.get("profit_lever")
            and result.get("strategy_decision")
            and isinstance(learning, dict)
            and learning.get("hypothesis")
            and learning.get("decision")
            and learning.get("next_safe_experiment")
        )

    at_risk_rows = [
        row
        for title, row in latest_by_title.items()
        if title in expected_titles
        and (
            row.status in {"failed", "blocked"}
            or (
                row.status == "done"
                and (
                    not has_growth_evidence(row)
                    or (row.result or {}).get("status") == "at_risk"
                )
            )
        )
    ]
    at_risk = sorted({row.agent_type for row in at_risk_rows})
    pending = sorted({
        row.agent_type
        for title, row in latest_by_title.items()
        if title in expected_titles and row.status in {"open", "queued", "running"}
    })
    completed = sorted({
        agent
        for agent in expected_agents
        if all(
            latest_by_title[title].status == "done"
            and has_growth_evidence(latest_by_title[title])
            and (latest_by_title[title].result or {}).get("status") != "at_risk"
            for title, expected_agent in expected_titles.items()
            if expected_agent == agent and title in latest_by_title
        )
        and agent not in missing
    })
    sysadmin_task: Task | None = None
    if missing or at_risk:
        risk_material = "|".join(
            [*missing_titles, *[str(row.id) for row in sorted(at_risk_rows, key=lambda item: item.id)]]
        )
        risk_key = hashlib.sha256(risk_material.encode()).hexdigest()[:16]
        title = f"CEO → System Admin · Стратегический портфель · {risk_key}"
        sysadmin_task = db.scalar(select(Task).where(Task.title == title))
        if sysadmin_task is None:
            sysadmin_task = Task(
                title=title,
                agent_type="system_admin",
                status="queued",
                priority="critical",
                max_attempts=3,
                payload={
                    "action": "system_admin_audit",
                    "source": "ceo_strategy_supervision",
                    "notify_owner": True,
                    "strategy_version": CEO_STRATEGY_VERSION,
                    "missing_agent_types": missing,
                    "at_risk_agent_types": at_risk,
                    "notification_idempotency_key": f"ceo-strategy-risk:{risk_key}:telegram",
                },
            )
            db.add(sysadmin_task)
            db.flush()
            record_task_created(
                db,
                sysadmin_task,
                actor="ceo",
                reason="strategy_risk_handoff",
            )
    return {
        "status": (
            "at_risk"
            if missing or at_risk
            else "pending"
            if pending
            else "verified"
        ),
        "strategy_version": CEO_STRATEGY_VERSION,
        "expected_agent_types": expected_agents,
        "covered_agent_types": sorted({row.agent_type for row in latest_by_title.values()}),
        "missing_agent_types": missing,
        "at_risk_agent_types": at_risk,
        "pending_agent_types": pending,
        "verified_completed_agent_types": completed,
        "system_admin_task_id": sysadmin_task.id if sysadmin_task else None,
        "external_actions_executed": False,
        "evidence": [{
            "type": "ceo_portfolio_verification",
            "cycle_key": cycle_key,
            "covered_agent_types": len({row.agent_type for row in latest_by_title.values()}),
            "expected_agent_types": len(expected_agents),
            "covered_lanes": len(set(expected_titles) & set(latest_by_title)),
            "expected_lanes": len(expected_titles),
            "at_risk": len(at_risk),
            "missing": len(missing),
            "pending": len(pending),
        }],
    }


def maintain_ceo_development_backlog(
    db: Session,
    *,
    now: datetime | None = None,
    cadence_hours: int = 24,
) -> list[Task]:
    """Keep a safe, finite and recurring CEO development backlog."""
    current_time = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cadence = max(1, min(int(cadence_hours), 7 * 24))
    created: list[Task] = []
    for template in CEO_DEVELOPMENT_BACKLOG:
        strategy = CEO_AGENT_GROWTH_STRATEGIES[str(template["agent_type"])]
        latest = db.scalar(
            select(Task).where(Task.title == template["title"]).order_by(Task.id.desc())
        )
        payload = {
            "action": "ceo_strategic_checkpoint",
            "scope": template["scope"],
            "strategy_version": CEO_STRATEGY_VERSION,
            "horizon": template["horizon"],
            "objective": template["objective"],
            "deliverable": template["deliverable"],
            "success_metric": template["success_metric"],
            "business_mission": CEO_GROWTH_MISSION,
            "lead_stage": strategy["lead_stage"],
            "profit_lever": strategy["profit_lever"],
            "strategy_hypothesis": strategy["hypothesis"],
            "self_improvement_protocol": CEO_SELF_IMPROVEMENT_PROTOCOL,
            "origin": "ceo_continuous_backlog",
            "advisory_only": True,
            "external_actions_require_owner_approval": True,
            "failure_handoff": "system_admin",
        }
        if latest and latest.status in {"open", "queued"}:
            previous_payload = latest.payload or {}
            if (
                previous_payload.get("origin") == "ceo_continuous_backlog"
                and previous_payload.get("strategy_version") != CEO_STRATEGY_VERSION
            ):
                previous_version = str(previous_payload.get("strategy_version") or "legacy")
                latest.description = str(template["objective"])
                latest.payload = payload
                latest.run_after = min(latest.run_after, current_time)
                db.add(
                    AuditLog(
                        actor="ceo",
                        action="strategy.task_upgraded",
                        resource_type="task",
                        resource_id=str(latest.id),
                        details={
                            "from_version": previous_version,
                            "to_version": CEO_STRATEGY_VERSION,
                        },
                    )
                )
                created.append(latest)
            continue
        if latest and latest.status in {"running", "blocked", "failed"}:
            continue
        run_after = current_time
        if latest:
            run_after = max(current_time, latest.run_after + timedelta(hours=cadence))
        task = Task(
            title=template["title"],
            description=str(template["objective"]),
            agent_type=template["agent_type"],
            status="queued",
            priority="normal",
            run_after=run_after,
            max_attempts=3,
            payload=payload,
        )
        db.add(task)
        db.flush()
        record_task_created(db, task, actor="ceo", reason="recurring_development_backlog")
        created.append(task)
    return created


def parse_lead_import(filename: str, content: bytes) -> list[dict[str, Any]]:
    lower = filename.lower()
    if lower.endswith(".csv"):
        text = content.decode("utf-8-sig")
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    if lower.endswith(".xlsx"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise HTTPException(503, "XLSX import requires openpyxl") from exc
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            preferred = next(
                (
                    sheet
                    for sheet in workbook.worksheets
                    if " ".join(sheet.title.lower().replace("ё", "е").split())
                    in {"для импорта", "import", "import data", "данные для импорта"}
                ),
                workbook.active,
            )
            values = list(preferred.iter_rows(values_only=True))
        finally:
            workbook.close()
        if not values:
            return []

        company_headers = {"company", "name", "title", "organization", "наименование", "организация"}
        region_headers = {"region", "регион", "subject"}
        contact_headers = {"email", "emails", "e-mail", "phone", "phones", "телефон", "электронная почта"}

        def normalized(row: tuple[Any, ...]) -> set[str]:
            return {" ".join(str(value or "").strip().lower().replace("ё", "е").split()) for value in row}

        header_index = 0
        for index, row in enumerate(values[:25]):
            headers = normalized(row)
            if headers & company_headers and headers & region_headers and headers & contact_headers:
                header_index = index
                break
        headers = [str(value or "").strip() for value in values[header_index]]
        if not any(headers):
            return []
        return [
            {headers[i]: value for i, value in enumerate(row) if i < len(headers) and headers[i]}
            for row in values[header_index + 1 :]
            if any(value not in (None, "") for value in row)
        ]
    raise HTTPException(422, "Only CSV and XLSX files are supported")
