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

`POST /api/tender-sources/collect` принимает настроенные HTTP(S) JSON feeds.
Каждая принятая карточка канонизируется, получает SHA-256 версии и сохраняет в
PostgreSQL append-only историю наблюдений вместе с заявленным источником и revision.
Точный повтор учитывается как `unchanged`, не создаёт новую версию и не публикует
повторное outbox-событие; изменение добавляет следующую версию, сохраняя предыдущий
snapshot. Bearer token остаётся только HTTP-заголовком и не попадает в карточку или
event payload. Это общий проверяемый feed contract, а не заявление о наличии
официального адаптера ЕИС/ЭТП.

Для generic feed закупка идентифицируется парой `(source URL, external_id)`.
Частичные уникальные индексы PostgreSQL разрешают одинаковый provider-local ID у
разных источников, но запрещают дубликат внутри одного источника. Уникальность
остальных типов `BusinessRecord` не ослаблена. URL пока является provider key только
для общего контракта; официальный adapter должен предоставить стабильный provider ID.

Каждая попытка по настроенному источнику завершается отдельной PostgreSQL-записью
`TenderSourceRun`: начало/окончание, HTTP status, количество увиденных, созданных,
изменённых и неизменных карточек, итог и только класс ошибки. Ошибка источника
откатывает его незавершённые изменения через savepoint. Транзакционный outbox получает
`tender.source_collection_completed`, а manager-only
`GET /api/tender-sources/runs` возвращает журнал без query, userinfo, fragment и текста
исключения. Это внутренний receipt запуска, а не подтверждение ЕИС/ЭТП о внешнем
действии и не реализация provider cursor или контроля полноты выдачи.

`GET /api/tender-sources/freshness` детерминированно сравнивает последний успешный
receipt каждого настроенного источника с `TENDER_SOURCE_FRESHNESS_SLO_MINUTES`.
Статусы `unobserved`, `never_succeeded`, `latest_failed` и `stale` попадают в
существующий System Admin как дедуплицированные технические инциденты и закрываются
только после актуального успешного receipt. Это внутренний last-success SLO; portal
cursor, полнота выдачи и внешнее подтверждение provider по-прежнему отсутствуют.

`POST /api/tender-documents/{document_id}/download` сохраняет каждый новый набор
байтов по checksum-addressed пути и добавляет его в append-only историю загрузок
документа. Повтор тех же байтов возвращает существующую версию и не создаёт второе
outbox-событие; изменённые байты получают новую версию, а предыдущий файл остаётся
доступен для проверки старых evidence. Если уже существующий файл по ожидаемому пути
не совпадает с SHA-256, загрузка завершается fail-closed без перезаписи.

`POST /api/tenders/{record_id}/prequalification-snapshots` выполняет отдельный
FAST DISQUALIFICATION до supplier work. Сервис требует фиксированный набор из 14
hard-constraint checks (лицензии, опыт, география, сроки, capacity, капитал,
обеспечение, субъектность, аккредитация, национальный режим, техническая
совместимость, risk policy и конфликт интересов), связывает каждый известный факт
с документом и точным checksum и сохраняет неизменяемый PostgreSQL snapshot.
Отсутствующий, `unknown` или не подтверждённый evidence факт даёт
`needs_verification`; доказанное несоответствие — `ineligible`; только полный
набор подтверждённых checks получает `eligible` и `supplier_discovery_allowed`.
Повтор точного входа идемпотентен, история доступна через manager/viewer API и
фиксируется в audit/outbox. Сам endpoint не ищет поставщиков и не отправляет RFQ.

`POST /api/tenders/{record_id}/supplier-quote-snapshots` сохраняет ручную или
импортированную котировку только после точного `eligible` prequalification hash.
Snapshot содержит идентификатор поставщика, SKU/производителя, точные денежные и
количественные значения, НДС, остаток/локацию, срок поставки, доставку, условия
оплаты, срок действия, product/certificate/reliability статусы, source и
checksum-bound evidence. Просроченная, неизвестная или неподтверждённая
котировка остаётся `needs_verification`, а доказанный mismatch/blocked supplier/
нехватка количества получает `rejected`; только `verified` разрешает передать
её в economics. Snapshot append-only и идемпотентен, история доступна через
соответствующий `GET`. Endpoint не ищет поставщика, не отправляет RFQ и не делает
заказ.

`POST /api/tenders/{record_id}/decision-snapshots` принимает типизированные:

- требования и qualification checks;
- evidence-bound product compliance matrix с required/offered/match по каждому
  параметру;
- ссылки на конкретные документы, checksum и locator;
- подтверждённую котировку поставщика и срок её действия;
- денежные входы и policy-пороги.

Опциональный `prequalification_snapshot_hash` связывает расчёт с точным
неизменяемым prequalification snapshot. Ссылка принимается только для того же
тендера, только со статусом `eligible` и только если qualification checks полностью
совпадают; stale, чужой или изменённый набор отклоняется. Inline checks сохранены
для обратной совместимости, а новый snapshot-bound путь явно маркируется в результате.

Опциональный `supplier_quote_snapshot_hash` аналогично привязывает economics к
точному `verified` quote того же тендера и того же prequalification snapshot;
inline quote обязан совпасть с сохранённой канонической котировкой. Старый inline
путь остаётся доступен для обратной совместимости.

Для подтверждённого совпадения параметра нужны evidence-ссылки как минимум на
два разных документа: требование заказчика и характеристику предложения. Сервис повторно
связывает evidence с документами данного тендера, рассчитывает
base/conservative economics через `Decimal`, working capital и stop price,
запрещает обязательное product mismatch, оставляет UNKNOWN как
`needs_verification`, применяет остальные fail-closed правила и сохраняет
неизменяемый decision passport. Только
snapshot со статусом `ready_for_owner_review` создаёт существующую задачу
`tender_participation`; автоматическая подача всегда запрещена.

`GET /api/tenders/{record_id}/decision-snapshots` возвращает историю паспортов.
Повтор идентичного входа возвращает существующий snapshot и не создаёт вторую
задачу.

`POST /api/tender-documents/{document_id}/requirements/extract` выполняет
локальное детерминированное извлечение кандидатов требований из проверенного
PDF, DOCX, TXT или Markdown в защищённом хранилище. Каждый кандидат связан с
checksum документа, locator и excerpt, сохраняется в `TenderDocument.analysis`
как `extracted` и всегда получает `unknown / needs_verification`. Содержимое
документа считается недоверенным: оно не отправляется внешней AI-модели, не
получает tools и не может автоматически разрешить участие или подачу. Повтор
для того же checksum и версии extractor идемпотентен.

`POST /api/tender-documents/{document_id}/product-specification/extract`
аналогично выделяет пары «параметр — значение» из документа требований или
спецификации поставщика. Роль документа берётся только из явной классификации,
а неизвестная роль остаётся `unknown`. Кандидаты сохраняют checksum, locator и
excerpt, но всегда получают `match_status=unknown` и требуют ручной проверки:
extractor не объявляет товары соответствующими и не создаёт разрешение на
участие. Повтор для той же версии, checksum и роли идемпотентен.

`POST /api/tender-documents/{document_id}/product-specification/review`
принимает решение менеджера по полному и точному набору извлечённых кандидатов.
Запрос привязан к checksum документа, версии extractor и `candidate_hash`; stale,
неполный или дублирующий набор отклоняется. Принятые и отклонённые значения,
коррекции, actor, timestamp и `review_hash` сохраняются в analysis документа,
публикуются в audit/outbox и повторно не дублируются. Даже проверенный extractor
не выполняет автоматическое сопоставление, участие или подачу.

`POST /api/tenders/{record_id}/product-comparison/drafts` собирает проверенные
характеристики из документов требований и предложения в единый междокументный
черновик. Входы повторно связываются с актуальными checksum и `review_hash`, а
история черновиков сохраняется в PostgreSQL внутри канонической карточки тендера.
Одинаковый набор входов идемпотентен; событие фиксируется в audit/outbox. Сигналы
`exact`, `different`, `missing_required`, `missing_offered` и `ambiguous` помогают
менеджеру, но каждый `match_status` остаётся `unknown / needs_verification` и не
может автоматически разрешить участие или подачу.

`POST /api/tenders/{record_id}/product-comparison/reviews` принимает решение
менеджера по точному набору строк последнего черновика. Review привязан к
`draft_hash`, checksum и review hash исходных документов; stale, неполный или
дублирующий набор отклоняется. Для missing/ambiguous evidence разрешён только
`unknown`, поэтому такой review остаётся `needs_verification`. История и
`review_hash` сохраняются в PostgreSQL, событие — в audit/outbox, а точный повтор
идемпотентен. Review не разрешает участие, подачу или иное внешнее действие.

`POST /api/tenders/{record_id}/decision-snapshots` также принимает
`product_comparison_review_hash`. В этом режиме строки product compliance
формируются только из актуального integrity-checked review; смешивание с ручной
матрицей запрещено. Review hash входит в канонический input snapshot. Неполные
или неоднозначные строки не превращаются в ложные факты, а добавляют
`product_comparison_review:needs_verification`. Это по-прежнему лишь паспорт
решения: участие и подача требуют отдельных owner approvals.

## Честная граница готовности

Срез production-quality для общего HTTP(S) JSON feed и ручного/fixture структурированного ввода,
локального выделения кандидатов требований и характеристик товара, проверки
исходных фактов, evidence-bound fast prequalification и supplier quote snapshot,
fail-closed
междокументного черновика, привязанного к нему
решения менеджера и review-bound decision snapshot. OCR/сложный table extraction,
официальные ЕИС/ЭТП adapters и provider cursors, malware/archive sandbox, supplier
discovery/RFQ, подача, ЭЦП, autobid, платежи и
post-win execution ещё не реализованы.
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

## Матрица требований 1–325

Полный нумерованный перечень мастер-промпта хранится в
`app/data/tender_l6_requirements.json`. Каждый пункт имеет приоритет, честный
статус и ссылки на проверяемое evidence. `unverified` никогда не считается
выполненным, а `partial` не засчитывается как готовность L6.

Manager API:

- `GET /api/tender-autopilot/master-requirements/summary` — покрытие, открытые P0
  и gates первого release/L6;
- `GET /api/tender-autopilot/master-requirements` — полный список с фильтрами
  `status` и `priority`.

Пункт 0 отслеживается отдельно как north-star метрика, поэтому реестр содержит
ровно требования 1–325, не смешивая метрику с функциональным backlog.
