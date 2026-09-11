# Gap analysis относительно Tender Autopilot L6

Статусы отражают код репозитория, а не заявленное намерение.

| Capability | Статус | Evidence / следующий пробел |
|---|---|---|
| PostgreSQL, migrations, RBAC, audit | EXISTS | Общий production-контур CleaningAIOS |
| Task workflow, retry, outbox, receipts | EXISTS | Нужны procurement-specific compensation и DLQ UI |
| Tender ingest из HTTP JSON feeds | PARTIAL | Общий HTTP(S) JSON contract сохраняет канонические append-only версии, revision и replay semantics. Опциональный `tender-page-v1` возобновляет same-origin next page из protected checkpoint только после успешной транзакции, хеширует provider acknowledgement/cursor в append-only run receipt и фиксирует `partial/complete/unknown`; manager view не раскрывает opaque cursor, query/userinfo или raw exception. Last-success SLO и дедуплицированный System Admin incident подключены. Нет официальных ЕИС/ЭТП adapters, стабильных provider keys и проверенных portal-specific completeness contracts |
| Нормализованная карточка тендера | PARTIAL | Generic feed identity теперь database-enforced парой source/provider + external ID, а core и feed-version provenance хранятся в `BusinessRecord.data`; нужен отдельный relational procurement/amendment graph и стабильные provider keys официальных adapters |
| Безопасная загрузка документов | PARTIAL | Есть SSRF/size/hash/storage boundary, checksum-addressed append-only byte versions, replay/conflict checks и MIME guard для локального PDF/DOCX extraction; нет malware scan, archive sandbox и OCR |
| Requirement extraction | PARTIAL | Локальный deterministic extractor сохраняет evidence-bound кандидаты как UNKNOWN/NEEDS_VERIFICATION; reviewer workflow, OCR и полный typed graph не подключены |
| Fast prequalification | PARTIAL | Неизменяемый snapshot требует 14 фиксированных hard-constraint checks, exact checksum evidence, выдаёт explainable `eligible/ineligible/needs_verification`, может связываться с integrity-valid Company Digital Twin и decision snapshot. Автоматическое получение доказательств и реальный supplier-discovery executor ещё отсутствуют |
| Company qualification | PARTIAL | Append-only Company Digital Twin хранит masked legal identity, business limits, фиксированные capability facts и document hash/issuer/issue/expiry/status; просрочка и UNKNOWN fail closed, а exact snapshot может быть привязан к prequalification. Directors/authorized staff/MChD/signature/insurance/contract and penalty history, automated registry verification and conflict refresh remain missing |
| Supplier quotes | PARTIAL | Manager API сохраняет неизменяемую, идемпотентную котировку с typed supplier/product/price/stock/delivery/freshness набором, exact document evidence и eligible-prequalification binding. Только `verified` quote hash можно привязать к economics; inline путь сохранён для совместимости. Discovery, RFQ, parsing внешнего ответа, reservations и backup suppliers отсутствуют |
| Product compliance matcher | PARTIAL | Decision snapshot хранит required/offered/match/confidence/evidence и fail-closed обязательные UNKNOWN/mismatch; локальный extractor и checksum-bound manager review формируют verified/rejected facts. Manager-only API сохраняет draft и точный review, привязанные к checksum/review/draft hash, и формирует из актуального review канонический snapshot input; missing/ambiguous evidence остаётся UNKNOWN. Полный knowledge graph отсутствует |
| Decimal economics | PARTIAL | Новый snapshot считает Decimal base/conservative/stop/capital; legacy evaluator всё ещё `float` |
| Explainable decision passport | EXISTS | Append-only snapshot, input/economics hashes, factors и approval card |
| Immutable decision history | EXISTS | PostgreSQL trigger запрещает UPDATE/DELETE snapshot |
| Owner participation approval | EXISTS | Существующий Approval Engine, task bound to snapshot hash |
| Submission approval | EXISTS | Policy gate существует; реального platform executor нет |
| Application builder | PARTIAL | Versioned decision snapshot now persists a deterministic seven-gate application checklist, exact blocking/verification item codes, completeness percent and checklist hash in the approval card. It never grants participation/submission. Templates, generated package, four-eyes review and portal readiness graph remain missing |
| Official submission adapters | MISSING | Feature должен оставаться OFF |
| Signing/МЧД/ЭЦП | MISSING | Требуется local privileged bridge и отдельный threat model |
| Auction simulator/autobid | PARTIAL | Decision snapshot моделирует owner-provided expected discount и fail-closed invariant `expected_bid >= stop_price`; event/replay corpus и autobid controller отсутствуют |
| Payment/bank integration | MISSING | Только approval/manual paths; private bank credentials запрещены AI |
| Contract execution/actual profit | PARTIAL | Общие operations/finance records есть; procurement lifecycle не связан end-to-end |
| Learning/backtest/champion-challenger | PARTIAL | Общий replay/evals есть; procurement golden dataset и calibration отсутствуют |
| Multi-provider AI router | PARTIAL | OpenAI/Claude/Gemini contracts есть; procurement benchmark отсутствует |
| Kill switches/capability flags | PARTIAL | Persisted owner-only global external-actions kill switch exists. Every protected Task/Orchestrator action has an explicit persisted capability flag; missing/disabled fails closed before approval. Record-bound participation/submission also has an isolated versioned per-tender stop with manager visibility, owner-only idempotent changes and audit/outbox evidence. Decision Engine checks global → tender → capability; direct SMTP bulk-outreach and social publication share the applicable global/capability layers. Future direct protected adapters still need hard-stop-near-execution evidence |
| Evidence freshness/conflicts | PARTIAL | Quote/deadline freshness и checksum binding есть; общий conflict registry отсутствует |
| Security corpus | PARTIAL | SSRF, approval and path tests есть; ZIP bomb/malware/document prompt injection corpus отсутствует |
| A4 reference scenario | PARTIAL | Reproducible structured fixture for a school order of exactly 1000 A4 packs at NMCK 1,000,000 RUB covers evidence, all hard prequalification checks, two immutable quotes, product match, economics, auction forecast, stop price, risk, checklist and approval card. Real download/extraction, multi-agent review, submission/auction and post-win tail remain open |

## Наиболее опасные неверные предположения

- `approved` не означает `submitted`, `signed` или `paid`.
- AI confidence не заменяет evidence.
- Публичная цена поставщика не является действующей коммерческой офертой.
- Нажатие кнопки на ЭТП не является подтверждением подачи без external receipt.
- Высокий прогноз прибыли не является фактической прибылью.
