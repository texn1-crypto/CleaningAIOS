# Tender Autopilot

Tender Autopilot развивается внутри CleaningAIOS и использует существующие
PostgreSQL, Task workflow, transactional outbox, audit и Approval Engine. Это не
отдельный сервис и не параллельная CRM.

## Карта существующей системы

| Слой | Реализация | Что переиспользуется для Tender Autopilot |
|---|---|---|
| Каналы | FastAPI, Telegram Control Center, Mission Control | Единые API, RBAC и карточки подтверждения |
| Workflow | `Task`, guarded transitions, worker, scheduler | Очередь, retry, идемпотентность и видимость ошибок |
| Данные | PostgreSQL/SQLAlchemy/Alembic | Тендеры, документы, snapshots и история решений |
| Интеллект | Agent Runtime, vendor-neutral AI router, Company Brain | Извлечение и независимый review только как advisory |
| Решения | deterministic policy, Approval Engine | Участие и подача остаются разными approvals |
| Интеграции | JSON feed boundary, safe HTTP downloader | Основа для официальных procurement providers |
| Наблюдаемость | structured logs, health/readiness, audit/outbox | Корреляция, incident evidence и replay trail |

## Реализованный вертикальный срез

`POST /api/tenders/{record_id}/decision-snapshots` принимает типизированные:

- требования и qualification checks;
- ссылки на конкретные документы, checksum и locator;
- подтверждённую котировку поставщика и срок её действия;
- денежные входы и policy-пороги.

Сервис повторно связывает evidence с документами данного тендера, рассчитывает
base/conservative economics через `Decimal`, working capital и stop price,
применяет fail-closed правила и сохраняет неизменяемый decision passport. Только
snapshot со статусом `ready_for_owner_review` создаёт существующую задачу
`tender_participation`; автоматическая подача всегда запрещена.

`GET /api/tenders/{record_id}/decision-snapshots` возвращает историю паспортов.
Повтор идентичного входа возвращает существующий snapshot и не создаёт вторую
задачу.

## Честная граница готовности

Срез production-quality для ручного/fixture структурированного ввода и проверки
решения. Автоматические OCR/extraction, официальные ЕИС/ЭТП adapters, supplier
RFQ, подача, ЭЦП, autobid, платежи и post-win execution ещё не реализованы.
Интерфейс не должен называть их подключёнными или завершёнными.

Документы:

- [Gap analysis](GAP_ANALYSIS.md)
- [Target architecture](ARCHITECTURE.md)
- [Domain model](DOMAIN_MODEL.md)
- [State machine](STATE_MACHINE.md)
- [Economics](ECONOMICS_ENGINE.md)
- [Security](SECURITY.md)
- [Roadmap](ROADMAP.md)
- [ADR-0001](ADRs/0001-incremental-relational-core.md)
