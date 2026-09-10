# Gap analysis относительно Tender Autopilot L6

Статусы отражают код репозитория, а не заявленное намерение.

| Capability | Статус | Evidence / следующий пробел |
|---|---|---|
| PostgreSQL, migrations, RBAC, audit | EXISTS | Общий production-контур CleaningAIOS |
| Task workflow, retry, outbox, receipts | EXISTS | Нужны procurement-specific compensation и DLQ UI |
| Tender ingest из HTTP JSON feeds | PARTIAL | Нет официальных ЕИС/ЭТП provider adapters и amendment stream |
| Нормализованная карточка тендера | PARTIAL | Core хранится в `BusinessRecord.data`; нужен отдельный relational procurement model |
| Безопасная загрузка документов | PARTIAL | Есть SSRF/size/hash/storage boundary и MIME guard для локального PDF/DOCX extraction; нет malware scan, archive sandbox и OCR |
| Requirement extraction | PARTIAL | Локальный deterministic extractor сохраняет evidence-bound кандидаты как UNKNOWN/NEEDS_VERIFICATION; reviewer workflow, OCR и полный typed graph не подключены |
| Company qualification | PARTIAL | Typed checks входят в snapshot; company digital twin неполон |
| Supplier quotes | PARTIAL | Typed current quote и evidence есть; RFQ, reservations и backup suppliers отсутствуют |
| Product compliance matcher | PARTIAL | Decision snapshot хранит required/offered/match/confidence/evidence и fail-closed обязательные UNKNOWN/mismatch; локальный extractor и checksum-bound manager review формируют verified/rejected facts. Manager-only API сохраняет идемпотентную историю междокументных draft-сравнений, привязанную к checksum/review hash, но оставляет каждый match UNKNOWN. Решение менеджера по draft и knowledge graph отсутствуют |
| Decimal economics | PARTIAL | Новый snapshot считает Decimal base/conservative/stop/capital; legacy evaluator всё ещё `float` |
| Explainable decision passport | EXISTS | Append-only snapshot, input/economics hashes, factors и approval card |
| Immutable decision history | EXISTS | PostgreSQL trigger запрещает UPDATE/DELETE snapshot |
| Owner participation approval | EXISTS | Существующий Approval Engine, task bound to snapshot hash |
| Submission approval | EXISTS | Policy gate существует; реального platform executor нет |
| Application builder | PARTIAL | Checklist есть в legacy flow; templates/readiness graph неполны |
| Official submission adapters | MISSING | Feature должен оставаться OFF |
| Signing/МЧД/ЭЦП | MISSING | Требуется local privileged bridge и отдельный threat model |
| Auction simulator/autobid | PARTIAL | Decision snapshot моделирует owner-provided expected discount и fail-closed invariant `expected_bid >= stop_price`; event/replay corpus и autobid controller отсутствуют |
| Payment/bank integration | MISSING | Только approval/manual paths; private bank credentials запрещены AI |
| Contract execution/actual profit | PARTIAL | Общие operations/finance records есть; procurement lifecycle не связан end-to-end |
| Learning/backtest/champion-challenger | PARTIAL | Общий replay/evals есть; procurement golden dataset и calibration отсутствуют |
| Multi-provider AI router | PARTIAL | OpenAI/Claude/Gemini contracts есть; procurement benchmark отсутствует |
| Kill switches/capability flags | PARTIAL | Persisted owner-only global external-actions kill switch exists; per-capability and per-tender controls remain open |
| Evidence freshness/conflicts | PARTIAL | Quote/deadline freshness и checksum binding есть; общий conflict registry отсутствует |
| Security corpus | PARTIAL | SSRF, approval and path tests есть; ZIP bomb/malware/document prompt injection corpus отсутствует |

## Наиболее опасные неверные предположения

- `approved` не означает `submitted`, `signed` или `paid`.
- AI confidence не заменяет evidence.
- Публичная цена поставщика не является действующей коммерческой офертой.
- Нажатие кнопки на ЭТП не является подтверждением подачи без external receipt.
- Высокий прогноз прибыли не является фактической прибылью.
