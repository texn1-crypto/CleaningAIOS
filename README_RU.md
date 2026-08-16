# CleaningAI OS

Production-oriented operating system for a cleaning business. The original MVP ZIP remains in the repository; its source is now unpacked into a normal, testable project structure.

## What works

- FastAPI Mission Control with PostgreSQL/SQLite, health and readiness checks;
- public multi-page cleaning-company website with service catalogue, preliminary price table, responsive layout, SEO endpoints, original ImageGen visuals and a privacy-aware lead form;
- shared records for tenders, leads/CRM, candidates, campaigns, finance and source data;
- linked business graph: clients, sites, contracts, employees, shifts, vacancies and complaints;
- persistent orchestrator queue and autonomous worker/scheduler loops;
- transactional Event Bus/outbox with idempotency, retries and dead-letter state;
- versioned Company Brain facts with confidence, source and expiry;
- deterministic Decision Engine, separate owner Approval Engine and auditable Agent Runtime runs;
- automatic event routing from Sales, Tenders, HR, Finance and Marketing records to their agents;
- AI CEO, Growth Officer, Research, Tender, Sales, Marketing, HR, Finance, Copywriter, Creative and Meta Brain agents;
- optional OpenAI-compatible Responses API advisor with strict JSON output, aggregate-only input and deterministic fallback;
- business goals/KPI progress, structured decisions, outcome measurement and three-scenario simulator;
- tender opportunity scoring and document registry with structured analysis;
- configurable HTTP JSON-feed tender collection and size-limited document download with SHA-256 verification;
- owner-approval gate for tender submissions, contracts, legal/financial commitments, final HR decisions and bulk outreach tasks;
- key-derived production RBAC, immutable audit events, decisions, tasks and agent heartbeats;
- CSV/XLSX lead and SPb/LO management-company import with source provenance, guarded website contact enrichment, documented consent evidence and an outreach queue with multiple mailboxes, real attachments, delivery journal, IMAP replies, suppression/unsubscribe, campaign deduplication, owner approval, per-mailbox limits and SMTP delivery;
- daily source-backed Cleaning News Agent plan with owner-reviewed text and generated images for Telegram, VK, Odnoklassniki and Instagram; Telegram, VK and Odnoklassniki use official publication APIs, while missing credentials and manual channels are reported and never imitated;
- a two-year 1B RUB annual revenue run-rate goal owned by Growth Officer, weekly cross-agent workstreams and a verified goal snapshot in every 30-minute owner report;
- unified inbound message inbox, content plan, staffing/reserve view, vacancy Telegram drafts, payment calendar and complaint/SLA control;
- public lead capture into CRM/inbox with consent, abuse protection, UTM attribution, deterministic hot-lead scoring, Sales tasks and owner-email notifications;
- public Telegram Lead Autopilot: `/start` and `/estimate` collect an explicitly consented cleaning request, qualify it deterministically, create a CRM contact plus Sales follow-up task, and notify the owner without sending customer PII to an AI provider;
- Russian marketing-provider registry, trackable hypotheses/experiments, manual external campaign binding, media queue and evidence-backed attribution analytics;
- masked company requisites and marketing invoices routed to Telegram owner approval with no automatic payment;
- least-privilege AI provider routing for reasoning, product improvements, images and video, with truthful credential/adapter states;
- backward-compatible Telegram commands plus Mission Control sections;
- natural-language Russian Telegram dialogue that maps ordinary phrases to read views or auditable agent tasks, without requiring slash commands;
- CRM-backed commercial-proposal PDF generation from a natural Russian Telegram request, with a downloadable draft, audit evidence and mandatory owner review before client delivery;
- DOCX/PDF proposal revision from a Telegram attachment: Copywriter edits the text locally, Creative applies the layout, and the owner receives both formats with no automatic client delivery;
- an Orchestrator evidence gate that rejects unsupported `done` results and creates a deduplicated improvement plus a linked AI CEO incident report;
- Request Analyst Agent that records real capability gaps and prepares a redacted Codex prompt with acceptance criteria and a mandatory test plan;
- Alembic migration, Docker Compose, CI, tests and rollback guide.

Agents produce operational analysis from the shared database. The core remains deterministic without an LLM. OpenAI Responses and native Claude Messages adapters can be configured independently. In `LLM_PROVIDER=auto`, Claude performs aggregate business synthesis, OpenAI evaluates request/capability gaps, and either can safely fall back to the other. Both receive only the minimum redacted/aggregate context, return schema-validated advisory output, have no application tools and may create only analysis/planning tasks. Recommendations that declare an owner decision are not queued, and the policy layer remains authoritative for every protected action. Tender collection similarly reports missing sources instead of inventing results.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs`. In development role headers are available for tests. In production set a strong `API_KEY` and pass `X-API-Key`; the role is derived from that key and cannot be raised with a header.

## Required production secrets

- `POSTGRES_PASSWORD`, `API_KEY`; optionally separate `MANAGER_API_KEY`, `OPERATOR_API_KEY`, `VIEWER_API_KEY`
- `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID` for Telegram
- optional `TELEGRAM_BOT_API_BASE_URL` for Telegram documents over 20 MB; without it the bot records a visible `credentials_required` blocker instead of ignoring the file
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` for delivery
- `UNSUBSCRIBE_SECRET` for independently rotatable signed unsubscribe links (falls back to `API_KEY`)
- `LLM_API_KEY` for OpenAI and/or `ANTHROPIC_API_KEY` for native Claude Messages API; `LLM_PROVIDER=auto` safely routes between them
- optional `WORKSPACE_AGENT_TRIGGER_ID` and `WORKSPACE_AGENT_ACCESS_TOKEN` for server-side handoff to a published ChatGPT Workspace Agent
- `TENDER_SOURCES` and per-source credentials/API keys for external tender ingestion
- `TENDER_MONITOR_INTERVAL_MINUTES` controls idempotent 24/7 polling of configured tender feeds; no synthetic tenders are created when sources are absent
- `COMPANY_LEGAL_NAME`, `COMPANY_INN`, company contacts/address and `PRIVACY_CONTACT_EMAIL` before enabling the public production lead form
- `OWNER_NOTIFICATION_EMAIL` plus SMTP for hot-lead alerts; optional Russian advertising account credentials listed in `.env.example`
- per-mailbox `SMTP_*` and `IMAP_*` environment secrets for outreach and inbound replies; secrets are referenced by name from `SenderMailbox` and are never returned by the API
- social administrator credentials listed in `.env.example`; Telegram, VK and Odnoklassniki publish only after exact owner approval, while Instagram remains manual/legal-review only
- `IMAGE_GENERATION_API_KEY` plus the explicit `IMAGE_GENERATION_ENABLED=true` owner switch for the Telegram AI image generator; `SOCIAL_IMAGE_GENERATION_ENABLED=true` additionally enables paid visuals for daily social posts; both default to disabled
- without a paid image key the worker uses the repository's original cleaning-photo pool, still checksum-binds every selected file to the owner preview

See [production deployment](docs/PRODUCTION.md) and [baseline failures](docs/BASELINE.md).

## CleaningAI OS 2.0 flow

Domain changes and their events are committed together. The worker publishes pending
events and creates tasks for the responsible domain agent. Before an agent runs, the
Decision Engine checks policy. Tender submission, legal/contract/financial actions,
final HR decisions and bulk outreach are blocked until the owner decides a dedicated
approval request. Every agent execution is recorded in `agent_runs`; durable company
facts live in `company_knowledge` instead of prompts or chat history.

For tenders, `POST /api/tenders/{id}/evaluate` performs evidence-based base and
conservative economics, working-capital and legal-risk checks. A viable result
creates a protected `tender_participation` task. Approval of that task permits only
package preparation; `tender_submission` remains a separate critical action. The
versioned operating policy is in `docs/TENDER_24_7_OPERATING_PROMPT.md`.

The operational APIs include `/api/events`, `/api/brain`, `/api/agent-runs`,
`/api/approvals`, `/api/entities`, `/api/company/graph`, `/api/goals`,
`/api/finance/site-economics`, `/api/simulations`, `/api/tenders/{id}/score`,
`/api/imports/leads`, `/api/inbox`, `/api/hr/staffing`, `/api/finance/payment-calendar`,
`/api/marketing/content`, `/api/operations/quality` and
`/api/outreach/campaigns/launch`. Apply migrations through `0011` before deploying.

Domain records now have guarded lifecycle transitions through `PATCH /api/records/{id}`.
CRM touches are stored through `/api/records/{id}/contacts`. Domain events use a
versioned envelope (`event_id`, schema version, actor, correlation and
causation) with durable per-consumer receipts. `/api/events` exposes delivery evidence to
manager/owner roles without changing the legacy numeric event `id`.
Task changes use a guarded state machine and append-only `/api/tasks/{id}/transitions`
ledger. PostgreSQL rejects direct updates or deletes of transition history.
`/api/modules/summary` returns one operational snapshot for Sales, Tenders, HR,
Finance and Marketing. Domain rules reject incomplete finance entries, active
tenders without deadlines and lost leads without a recorded reason.

Open `/` for the public company website, `/mission-control` for the internal dashboard and `/docs` for all operations.
`/api/integrations` truthfully reports which external credentials or source adapters
are still missing. External tender portals are never reported as active until their
source is configured; the LLM is never reported as configured without its key and
model. The deterministic operating system works without either integration.

See [architecture](docs/ARCHITECTURE.md) for the event flow and module boundaries.
See [website and Marketing OS](docs/MARKETING_SITE.md) for the lead, media, advertising-platform, invoice and credential flows.

## Общение с Telegram-ботом

### Публичный Lead Autopilot

Неавторизованный посетитель бота видит только безопасный мастер заявки. Команда
`/estimate`, кнопка «Рассчитать уборку» или фраза «нужна уборка» последовательно
запрашивает согласие на обработку данных, тип и площадь объекта, район, график,
срок запуска, имя и контакт. До согласия телефон и email не запрашиваются. Каждое
новое обращение идемпотентно записывается в CRM и Inbox, получает прозрачную
детерминированную оценку качества лида и отдельную задачу Sales Agent; владелец
получает Telegram-уведомление, а о горячем лиде — также email при настроенном SMTP.
Неавторизованный клиент не получает доступ к Mission Control, задачам, рассылкам,
тендерам или данным других клиентов.

Числовой диапазон цены показывается только из утверждённой владельцем матрицы
`LEAD_ESTIMATE_MIN_RUB_PER_SQM`, `LEAD_ESTIMATE_MAX_RUB_PER_SQM` и
`LEAD_ESTIMATE_MIN_ORDER_RUB`. Ставки задаются в рублях за м² за один выезд;
для регулярного графика рассчитывается ориентир за месяц. Нулевые значения
оставляют сбор и квалификацию заявок рабочими, но бот честно передаёт расчёт
специалисту. Любой диапазон помечается как предварительный, не является офертой и
требует обследования объекта. В production мастер не запрашивает персональные
данные, пока не заполнены `COMPANY_LEGAL_NAME` и контакт политики обработки данных.

### AI-генератор изображений

Владелец может написать обычной фразой «Создай изображение чистого холла
бизнес-центра без людей» или использовать `/image описание`. Запрос проходит через
Marketing Agent и durable-очередь. Worker вызывает OpenAI Images, принимает только
валидный PNG/JPEG в пределах лимита, сохраняет SHA-256 и отправляет готовый файл в
Telegram как фотографию. Повторная обработка одного Telegram message ID не создаёт
второй платный запрос. Изображение никогда не публикуется автоматически.

Передача email, телефона, банковского счёта или секрета в prompt блокируется до
внешнего API. Для активации задайте `IMAGE_GENERATION_API_KEY` и
`IMAGE_GENERATION_ENABLED=true` только в защищённой конфигурации сервера. При
отсутствующем или отклонённом ключе бот сообщает `credentials_required`; ключ нельзя
присылать в Telegram, чат, API payload или хранить в БД. Плановые соцсети используют
AI только при отдельном `SOCIAL_IMAGE_GENERATION_ENABLED=true`, иначе остаются на
оригинальном локальном медиапуле.

Владелец может писать боту обычным русским текстом: «покажи задачи», «что с
тендерами», «найди тендеры по уборке БЦ», «создай задачу связаться с клиентом» или
«проанализируй финансы». Фраза «подготовь коммерческое предложение для клиента
Название» создаёт настоящий PDF по CRM и возвращает владельцу проект документа.
Приложенный DOCX/PDF с просьбой улучшить КП проходит через Orchestrator, отдельных
агентов текста и дизайна и возвращается владельцу в обоих форматах. Облачный Bot
API ограничивает скачивание 20 МБ; для большего исходника нужен локальный Telegram
Bot API server и `TELEGRAM_BOT_API_BASE_URL`. Без него запрос остаётся видимой
заблокированной задачей с improvement ID и отчётом AI CEO.
Чтение данных выполняется сразу, а остальные деловые поручения
превращаются в задачи подходящего агента. Неизвестные поручения передаются
Orchestrator. Если формулировка неоднозначна или содержит опечатку, бот показывает
конкретную догадку с кнопками `✅ Да` и `❌ Нет`. `✅ Да` выполняет предложенное
безопасное действие (например, открывает мастер рассылки или создаёт задачу), а
`❌ Нет` отменяет его; устаревшую кнопку повторно использовать нельзя. Оплата,
договоры, подача заявки на тендер, окончательные кадровые
решения и массовые рассылки сохраняют обязательное подтверждение владельца.

## База управляющих компаний и согласованная email-рассылка

`POST /api/research/management-companies/import` принимает официальный или
проверенный CSV/XLSX-экспорт по Санкт-Петербургу и Ленинградской области. Каждая
запись хранит источник, имя и SHA-256 исходного файла, URL и время сбора;
повторный импорт обновляет запись по
ИНН, ОГРН, идентификатору источника или устойчивому хешу названия и региона.
Email не используется как идентификатор организации: один диспетчерский адрес
может обслуживать несколько УК/ТСЖ. Импорт сохраняет тип организации, несколько
email и телефонов, адрес, контактное лицо, обслуживаемые объекты, кандидат сайта
и ссылки на исходные строки, но игнорирует неподтверждённое поле согласия.
Если структурированный XLSX содержит лист `Для импорта`, система использует
только его: листы с исходными строками и непривязанными контактами остаются
provenance и никогда не становятся неявным списком получателей.

Research-задача с `collection=management_company_contacts` формирует проверяемый
отчёт о покрытии и может обходить только уже подтверждённые сайты. Кандидат сайта
остаётся гипотезой до проверки. Sales-задача с
`action=prepare_management_company_outreach` строит сегменты по типу и региону,
показывает число адресов с доказанным согласием и всегда создаёт `0` отправок.
Автоматический поиск официальных сайтов остаётся `adapter_required`, пока не
настроены `YANDEX_SEARCH_API_KEY` и `YANDEX_CLOUD_FOLDER_ID` и не пройдена
валидация поискового адаптера; поисковая выдача сама по себе не считается
доказательством официальности домена.
Сайт отдельной УК можно проверить
через `POST /api/research/management-companies/{id}/enrich`: сборщик соблюдает
`robots.txt`, ограничивает размер и число страниц, не ходит на локальные адреса и
сохраняет найденные телефоны/email вместе с provenance.

Публично указанный адрес не является автоматически подтверждённым согласием на
рекламную рассылку. Владелец фиксирует основание через
`PUT /api/outreach/consents`; доказательство хранится как SHA-256, а отзыв сразу
добавляет адрес в suppression. Массовая кампания отклоняется до проверки всех
согласий и затем требует отдельного owner approval, привязанного к точному списку
получателей, теме, тексту и хешам вложений. Это соответствует требованию
предварительного согласия из [статьи 18 закона «О рекламе»](https://www.consultant.ru/document/cons_doc_LAW_58968/f892dec1383709792452f18d36e7043306e2be0a/).

В Telegram документ с подписью «разошли по базе УК» создаёт только защищённую
задачу и показывает число допустимых получателей, тему, текст и кнопки решения.
Кнопка «📣 Рассылки» и команда `/outreach` открывают внутри бота живую панель:
готовность почтовых ящиков, число подтверждённых согласий и отписок, очередь,
лимиты, ожидающие подтверждения и последние кампании без раскрытия адресов
получателей. Там же есть пошаговая инструкция создания новой рассылки.
Если заказчики сами передали адреса и попросили получать письма, команда
`/mailing` запускает owner-only мастер и просит загрузить Excel XLSX/XLSM, Word
DOCX или PDF с текстовым слоем. Файл ограничен 10 МБ и разбирается локально без
LLM; сканированный PDF без распознаваемого текста отклоняется. Бот извлекает и
дедуплицирует до 1000 email, затем фиксирует основание согласия, тему и тело
письма, показывает точный preview и создаёт одну защищённую задачу. Внутри неё
получатели разбиваются на партии максимум по 100 (`100 + 100 + …`), а worker
обрабатывает очередь последовательно с учётом фактических SMTP-лимитов. Старый
вариант `/mailing client@example.com second@example.com` сохранён для обратной
совместимости. Email, файл и основание не передаются LLM; постановка писем в
очередь возможна только после отдельного owner approval.
После одобрения worker распределяет письма по активным ящикам с учётом их очереди
и лимитов; лимит одного ящика не блокирует остальные. IMAP worker дедуплицирует
ответы, сохраняет их в едином inbox и пересылает на `OWNER_NOTIFICATION_EMAIL`
через тот же исходящий ящик (с резервом на общий SMTP). В пересланном письме
`Reply-To` указывает на клиента, а запись считается пересланной только после
подтверждённой SMTP-отправки. Поддерживаются STARTTLS/587 и implicit TLS/465;
панель бота отдельно показывает готовность исходящей доставки, IMAP и пересылки.
Для Gmail каждый ящик создаётся владельцем вручную,
включается двухэтапная аутентификация и отдельный пароль приложения; Google не
позволяет надёжно автоматизировать создание аккаунта, CAPTCHA и телефонную
проверку. Лимиты приложения следует держать существенно ниже опубликованного
[лимита Gmail](https://support.google.com/mail/answer/22839) и увеличивать только
по фактической репутации домена и ответам адресатов.

## Социальные сети и цель 1 млрд ₽

Фраза в Telegram вроде «начните оформлять социальные сети ВК и Одноклассники»
маршрутизируется Marketing Agent и выполняется сразу: в общей базе создаются или
обновляются карточки `social_account_setup` с публичной ссылкой, состоянием
credentials и точным чек-листом владельца. Бот показывает фактическое число
созданных карточек и отдельно сообщает, что регистрация внешнего аккаунта,
телефонная проверка и CAPTCHA не выполнены автоматически. Пароль от личного
аккаунта системе не требуется. Ошибка адаптера сохраняется как `failed`, а не
выдаётся за успешно начатое оформление.

Scheduler ежедневно создаёт задачу Cleaning News Agent. Агент получает свежие
публикации из настраиваемого списка публичных отраслевых RSS-источников, отбрасывает
устаревшие и нерелевантные записи и не создаёт пост, если проверяемой новости нет.
Каждый текст содержит ссылку на первоисточник и адаптируется для Telegram, VK,
Одноклассников и Instagram. Для двух сюжетов создаются оригинальные визуалы через
официальный Images API; платная генерация запускается только при одновременном наличии
ключа и явного `SOCIAL_IMAGE_GENERATION_ENABLED=true`. После технической проверки
файла и checksum бот отправляет владельцу Telegram-альбом для визуального решения.
Если платный ключ отсутствует, используется собственная библиотека клининговых
фотографий проекта; происхождение выбранного файла сохраняется в metadata:
каждый кадр содержит финальное изображение, точный текст, площадку и время. Approval
привязан к SHA-256 и неизменяемому digest всего набора; любая правка текста, времени
или изображения после просмотра отклоняет решение и требует нового preview. Одобрение
переводит разрешённые каналы в `scheduled`. Worker публикует одобренный набор через
официальные Telegram Bot API, VK API и Odnoklassniki REST API, фиксирует внешний post ID и не повторяет
неоднозначный запрос после timeout без ручной сверки. Отсутствие официального токена
или прав администратора остаётся видимым `credentials_required/adapter_required`,
а не фиктивной публикацией. Кнопка `/social` показывает состояния подборок и каналов.
Instagram-материалы остаются черновиками `legal_review_required`: автоматическая
рекламная публикация заблокирована с учётом [72-ФЗ](https://publication.pravo.gov.ru/document/0001202504070018)
и официального [перечня Минюста](https://minjust.gov.ru/ru/documents/7822/).

Публичный сайт разделён на `/services`, страницы отдельных услуг, `/prices`,
`/about`, `/contacts` и `/journal`. Карточки клиентов и заявления о выполненных
проектах не публикуются без подтверждённых фактов. Ссылки на социальные сети
задаются через `SOCIAL_TELEGRAM_URL`, `SOCIAL_VK_URL`,
`SOCIAL_ODNOKLASSNIKI_URL` и `SOCIAL_INSTAGRAM_URL`; небезопасные URL скрываются.

Growth Officer владеет целью `annual_revenue_run_rate_rub = 1_000_000_000` с
горизонтом 24 месяца. Факт считается только по активным договорам как сумма
`monthly_revenue × 12`. Агент измеряет разрыв и темп, создаёт недельные задачи для
Sales, Marketing, Tender, Finance и HR и сохраняет общий KPI для AI CEO. Каждый
регулярный 30-минутный отчёт содержит факт run-rate, процент прогресса, разрыв до
цели, статус темпа и источник данных. Это целевой ориентир, а не гарантия
финансового результата; внешние расходы, цены, договоры и рассылки по-прежнему
требуют решения владельца.

## Request Analyst и контур улучшений

Каждая обычная фраза владельца дополнительно анализируется агентом
`request_analyst`. Запросы, которые уже поддерживаются, продолжают выполняться
обычным маршрутом. Отсутствующие credentials отмечаются как необходимость
настройки, а не как новая функция. Если бот может только сохранить поручение, но
не способен гарантировать реальный результат, создаётся запись в
`improvement_requests` со следующими полями:

- исходный текст с удалёнными токенами и паролями;
- причина неполной поддержки и недостающие возможности;
- готовый промт для Codex;
- критерии приёмки и обязательный тест-план;
- состояние handoff, реализации и тестовые доказательства.

Очередь доступна через `GET /api/improvements`, а в Telegram — кнопкой
«🛠 Улучшения» или фразой «покажи улучшения». Повтор одинакового запроса не
создаёт дубликат, а увеличивает его счётчик. Локальный Codex может периодически
забирать очередь; для полностью серверной передачи можно опционально подключить
опубликованный ChatGPT Workspace Agent. Без его credentials система честно
показывает `credentials_required` и сохраняет готовый handoff в PostgreSQL.

После пользовательского запуска Orchestrator проверяет наличие запрошенного файла
или другого evidence. Неполный результат не считается выполненным: создаётся одно
дедуплицированное улучшение, а AI CEO формирует связанный отчёт с причиной, handoff
и ответственной стороной.
