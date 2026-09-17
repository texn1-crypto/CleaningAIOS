# CleaningAIOS autonomy stack, 2026

Дата проверки источников: **2026-09-17**. Внешние факты ниже проверены по официальным
репозиториям, документации и политикам поставщиков. Это архитектурное исследование,
не подтверждение готовой production-интеграции и не юридическое заключение.

## Итоговая рекомендация

CleaningAIOS не нужно заменять новым «автономным фреймворком». В текущем репозитории
уже есть правильный управляющий контур: PostgreSQL как system of record, `Task` и
защищённые переходы, transactional outbox/consumer receipts, Agent Runtime,
детерминированные approvals, consent/suppression и аудит. Эти границы закреплены в
[ARCHITECTURE.md](../ARCHITECTURE.md), [AGENT_TOOL_POLICY.md](../AGENT_TOOL_POLICY.md)
и [AGENTS.md](../../AGENTS.md). Стек Python 3.12, FastAPI, SQLAlchemy, PostgreSQL 16 и
Docker Compose подтверждается [requirements.txt](../../requirements.txt),
[Dockerfile](../../Dockerfile) и [docker-compose.yml](../../docker-compose.yml).

Рекомендуемый контур:

1. **Оставить CleaningAIOS единственным оркестратором бизнес-состояния.** При
   необходимости подключать LangGraph только внутри одного `AgentRun` для сложного,
   ограниченного графа рассуждений. Не создавать второй реестр задач, approvals или
   CRM.
2. **Не выдавать 60 агентам прямой произвольный интернет.** Добавить один
   policy-controlled egress broker: search, HTTP crawl и browser-read как отдельные
   узкие capability. Каждая capability получает allowlist роли, лимит времени,
   запросов, размера, стоимости и домена, SSRF/redirect-защиту и аудит.
3. **Статические публичные источники читать Scrapy, динамические страницы —
   изолированным Crawl4AI/Playwright worker.** CAPTCHA никогда не обходить: задача
   останавливается в `human_required` либо переключается на официальный API.
4. **CRM развивать в существующем PostgreSQL.** Если операторам нужен более богатый
   интерфейс, пилотировать Twenty как внешний workbench через REST/GraphQL/webhooks,
   сохраняя CleaningAIOS system of record. Не давать Twenty прямой доступ к таблицам
   CleaningAIOS и не делать dual-write.
5. **Voice: LiveKit Agents + SIP carrier, первый carrier-adapter можно сделать для
   Twilio там, где он доступен и подходит по условиям.** Все dial-команды проходят
   собственный consent/DNC/suppression/calling-hours/approval gate CleaningAIOS.
   Запись по умолчанию выключена и включается только после требуемого уведомления и
   согласия.
6. **Автообновления: CI → неизменяемый image digest → staging → smoke/evals →
   production → наблюдение/rollback.** Dependabot или Renovate создаёт PR, но не
   меняет production напрямую. Watchtower для production не использовать.
7. **«Самообучение» означает append-only знания и проверяемые improvement proposals,**
   а не самостоятельное редактирование кода, ослабление тестов или расширение
   собственных прав.

## Совместимость и решение build vs integrate

| Область | Рекомендуемое решение | Совместимость с Python/FastAPI/PostgreSQL/Docker | Решение |
|---|---|---|---|
| Бизнес-оркестрация | Текущие `Task`, Event Bus, Agent Runtime и approvals | Нативная | **Build/extend current** |
| Сложный граф внутри одной задачи | LangGraph 1.x | Python 3.10+, PostgreSQL checkpointer, async | **Integrate selectively** |
| 60 ролей | Реестр ролей + bounded worker pools, не 60 постоянных процессов | Нативная | **Build on current** |
| CRM | Текущие `BusinessRecord`/contacts/events; Twenty только как UI/workbench | Twenty: Docker, PostgreSQL/Redis, REST/GraphQL/webhooks | **Build first; optional integrate** |
| Статический crawl | Scrapy 2.19 | Python, отдельный worker/container | **Integrate** |
| JS/browser-read | Crawl4AI 0.9.3 sidecar на Playwright; прямой Playwright для узких адаптеров | Python client/REST, Docker | **Integrate sidecar** |
| Поисковое обнаружение | Текущий провайдерный search adapter; SearXNG опционально | HTTP JSON API, Docker | **Integrate, not sole source** |
| Knowledge retrieval | Текущий Company Brain; pgvector только после eval | PostgreSQL 16, SQLAlchemy/Psycopg 3 | **Build incrementally** |
| Voice-agent runtime | LiveKit Agents + LiveKit SIP | Python, Docker/Kubernetes, SIP | **Integrate** |
| PSTN carrier | Twilio adapter или региональный SIP carrier | HTTPS/webhooks/SIP | **Integrate behind interface** |
| Dependency updates | GitHub Dependabot; Renovate при более сложной политике | PR-based, Docker/GitHub Actions/Python | **Integrate** |
| Production deploy | Усилить текущий SHA-gated deploy pipeline | Нативная | **Build/extend current** |

## 1. Многоагентная оркестрация

### Кандидаты

**LangGraph.** Официальный репозиторий описывает durable execution,
human-in-the-loop, memory и stateful long-running workflows; библиотека MIT и
поддерживает Python 3.10–3.13. Production persistence поддерживает
`PostgresSaver`, а custom workflows позволяют смешивать детерминированные узлы,
ветвления, циклы, параллельные ветки и агентов:
[репозиторий](https://github.com/langchain-ai/langgraph/blob/main/README.md),
[persistence/PostgresSaver](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx),
[custom workflows](https://github.com/langchain-ai/docs/blob/main/src/oss/langchain/multi-agent/custom-workflow.mdx).

Fit высокий, но LangGraph не должен становиться вторым workflow system of record.
Допустимая граница: один CleaningAIOS `Task` создаёт один `AgentRun`; LangGraph
исполняет bounded subgraph и возвращает структурированный результат/evidence;
изменение доменных данных и любой внешний эффект по-прежнему выполняет CleaningAIOS.
Его checkpoints хранят техническое состояние subgraph, а не дублируют статусы `Task`.

**OpenAI Agents SDK.** Python SDK поддерживает agents-as-tools, handoffs,
guardrails, sessions, human-in-the-loop и tracing:
[официальный обзор](https://github.com/openai/openai-agents-python/blob/main/docs/index.md),
[оркестрация](https://github.com/openai/openai-agents-python/blob/main/docs/agents.md),
[guardrails](https://github.com/openai/openai-agents-python/blob/main/docs/guardrails.md).
SDK полезен для OpenAI-specific пилотов и voice pipeline, но не как основа всей ОС:
CleaningAIOS уже владеет loop, tool dispatch и state. Кроме того, встроенный tracing
по умолчанию включён и может содержать model/tool inputs и outputs, поэтому для
клиентских данных нужен явный запрет sensitive payload либо локальный trace
processor:
[tracing](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md).

**Microsoft Agent Framework.** Актуальная Python-линия 1.18.0 имеет sequential и
concurrent orchestration, checkpoints, HITL, A2A/MCP и много провайдеров:
[Python changelog](https://github.com/microsoft/agent-framework/blob/main/python/CHANGELOG.md),
[orchestration patterns](https://github.com/microsoft/agent-framework/blob/main/python/packages/orchestrations/README.md),
[checkpoint docs](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints).
Fit технически хороший, но внедрение целого framework дублирует текущие runtime,
checkpoints и approval semantics. Рассматривать только отдельным пилотом, если A2A
между внешними системами станет реальным требованием.

**AutoGen не выбирать для нового контура.** Microsoft пометил проект maintenance
mode и рекомендует новым пользователям Agent Framework:
[официальный README](https://github.com/microsoft/autogen).

### Модель для 60 агентов

Шестьдесят агентов — это 60 версионируемых ролей и capability profiles, а не 60
контейнеров, постоянных LLM-сессий или независимых баз данных.

- CEO и Meta Brain ранжируют портфель и читают агрегаты, но не получают write-tools.
- Orchestrator выбирает одну роль по структурированному route decision.
- Worker pools разделяются по классу нагрузки: deterministic, LLM, research/browser,
  documents, voice. У каждого есть bounded concurrency и backpressure.
- Любой fan-out получает parent correlation ID, дедупликационный ключ, deadline,
  token/cost budget и максимум дочерних задач.
- Завершение требует evidence; retry не создаёт второй внешний эффект.
- Новый агент регистрируется только с owner, входным/выходным schema, allowlist
  capabilities, SLO, eval и stop condition.

Это расширяет текущую архитектуру и не требует нового multi-agent фреймворка.

## 2. CRM

### Рекомендация: текущий PostgreSQL как master CRM

В репозитории уже реализованы лиды, контакты, consent/suppression, pipeline summary,
CRM-backed proposals и idempotent inbound intake; актуальные ограничения перечислены
в [CAPABILITY_AUDIT.md](../CAPABILITY_AUDIT.md). Поэтому правильнее закрыть пробелы
в cross-channel deduplication, call history, next-best-action и deep links, чем
создавать второй master.

Системный отчёт должен формировать стабильную ссылку вида
`{CRM_PUBLIC_BASE_URL}/objects/people?...` или ссылку на внутреннюю карточку лида,
но URL обязан быть конфигурацией, а не значением, которое администратор присылает
вручную при каждом отчёте.

### Twenty как дополнительный workbench

Twenty self-hosted запускается через Docker Compose; проект предоставляет REST,
GraphQL, webhooks и app framework:
[README/self-hosting](https://github.com/twentyhq/twenty/blob/main/README.md),
[extend/API/webhooks](https://docs.twenty.com/developers/extend/extend),
[webhooks](https://docs.twenty.com/developers/extend/webhooks),
[PostgreSQL/Redis compose](https://github.com/twentyhq/twenty/blob/main/packages/twenty-docker/docker-compose.dev.yml).
Большая часть сервера лицензирована AGPLv3, часть SDK имеет MIT/enterprise terms;
перед изменением/распространением нужен review точных файлов и модели использования:
[лицензия](https://github.com/twentyhq/twenty/blob/main/LICENSE).

Интеграционный контракт:

- CleaningAIOS публикует outbox event с `lead_id`, version и checksum;
- CRM adapter upsert-ит запись в Twenty по immutable external ID;
- webhook Twenty принимается с deduplication key и превращается в обычную команду
  CleaningAIOS после RBAC/policy validation;
- sync cursor и mapping ID хранятся в PostgreSQL;
- конфликт не разрешается «последняя запись победила», а попадает в reconciliation;
- personal data не копируется в agent prompts или общий audit payload.

Не использовать одну PostgreSQL-схему для двух продуктов и не писать одновременно
в обе базы из business service.

### Odoo как альтернатива

Odoo 19 написан на Python, использует PostgreSQL и имеет официальный Docker image:
[репозиторий](https://github.com/odoo/odoo),
[PostgreSQL requirements](https://www.odoo.com/documentation/19.0/administration/on_premise/source.html),
[официальный Docker guide](https://github.com/docker-library/docs/blob/master/odoo/README.md).
Он оправдан, если требуется единая ERP с бухгалтерией, HR и операциями. Для текущей
задачи это тяжёлая миграция. В Odoo 19 внешний JSON-2 API доступен только на Custom
pricing plan, что важно проверить до выбора:
[external API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html).

## 3. Интернет, browser automation и crawling

### Рекомендуемый read-only egress plane

```text
Agent Task
  -> policy/capability gate
  -> Search discovery
  -> URL policy + SSRF/redirect validation
  -> Scrapy HTTP fetch OR Crawl4AI/Playwright browser worker
  -> bounded extraction + provenance + checksum
  -> append-only Company Brain / lead evidence
  -> structured result to AgentRun
```

Прямые `httpx`, shell, browser или arbitrary MCP endpoints внутри агента запрещены.
Broker до каждого hop повторно проверяет `https`, DNS/IP, redirect target, порт,
userinfo и private/link-local/metadata ranges; ограничивает bytes, MIME, redirects,
время, глубину, число URL, частоту на домен и общий budget. HTML, PDF и tool output
считаются untrusted data, никогда инструкциями.

### Компоненты

**Scrapy 2.19** — основной статический crawler. Это Python/BSD framework; встроенный
`RobotsTxtMiddleware` умеет применять robots rules, а middleware/setting слой удобно
фиксирует timeouts и per-domain policy:
[репозиторий](https://github.com/scrapy/scrapy),
[robots middleware](https://docs.scrapy.org/en/master/topics/downloader-middleware.html),
[settings](https://docs.scrapy.org/en/latest/topics/settings.html).
В CleaningAIOS `ROBOTSTXT_OBEY` должен быть неизменяемой server-side политикой, а не
опцией model request.

**Crawl4AI 0.9.3** — предпочтительный динамический sidecar, потому что текущий
deploy script уже предусматривает профиль `crawl4ai`. Релиз 0.9 сделал Docker API
secure-by-default: authentication on, loopback default, declarative hooks вместо
передаваемого Python-кода и bounded jobs:
[релиз/hardening](https://github.com/unclecode/crawl4ai/blob/main/CHANGELOG.md),
[Docker guide](https://github.com/unclecode/crawl4ai/blob/main/deploy/docker/README.md),
[migration/security defaults](https://github.com/unclecode/crawl4ai/blob/main/deploy/docker/MIGRATION.md).
Нужно держать exact version/digest, API на внутренней сети, auth,
read-only filesystem, non-root, Chromium sandbox/seccomp, resource limits и
`check_robots_txt=true`. Hooks, arbitrary JS, proxy config, custom headers/cookies и
file URLs не выдаются агентам.

Текущий снимок [docker-compose.yml](../../docker-compose.yml) уже содержит
`crawl4ai` optional profile, exact multi-platform image digest, отдельную сеть,
bearer token и health check, а CleaningAIOS — bounded read-only adapter, robots
enforcement и regression tests. До production всё ещё нужны container hardening и
server-side end-to-end smoke с реальными credentials; наличие Compose-сервиса само
по себе этого не доказывает.

**Playwright Python 1.63** использовать под Crawl4AI либо в узком portal-adapter.
Он поддерживает Chromium/Firefox/WebKit и auto-waiting:
[репозиторий](https://github.com/microsoft/playwright-python),
[auto-waiting](https://playwright.dev/python/docs/actionability).
Официальная Docker-документация прямо рекомендует отдельного пользователя и seccomp
для crawling untrusted sites и предупреждает, что root отключает Chromium sandbox:
[Playwright Docker](https://playwright.dev/python/docs/docker). Поэтому browser не
следует устанавливать в основной FastAPI/worker image.

**Crawlee Python 1.10** — допустимая альтернатива, если потребуется отдельная
autoscaled crawl queue. Он объединяет PlaywrightCrawler, queues, concurrency и
per-domain throttling с обработкой `429`, `Retry-After` и `crawl-delay`:
[репозиторий](https://github.com/apify/crawlee-python),
[request throttling](https://crawlee.dev/python/docs/guides/request-throttling).
Сейчас это частично дублирует существующие Task/worker очереди, поэтому не первый
выбор.

**SearXNG** можно self-host как discovery metasearch: есть Docker deployment и JSON
Search API:
[Search API](https://github.com/searxng/searxng/blob/master/docs/dev/search_api.rst),
[Docker install](https://github.com/searxng/searxng/blob/master/docs/admin/installation-docker.rst).
Он не гарантирует стабильность upstream engines: `429`, CAPTCHA или запрет источника
дают `source_unavailable`, после чего задача использует лицензированный search API
или останавливается. SearXNG не является доказательством найденного факта: каждое
утверждение должно ссылаться на исходную страницу.

**Firecrawl 2.x не брать в baseline.** Он функционально силён и по умолчанию уважает
robots.txt, но self-host stack включает API/workers, Playwright, Redis, RabbitMQ,
PostgreSQL queue и опциональный FoundationDB. Официальный self-host guide указывает,
что baseline API unauthenticated, а root Compose не задаёт persistence volumes для
нескольких stateful services:
[репозиторий/robots policy](https://github.com/firecrawl/firecrawl),
[self-host guide](https://github.com/firecrawl/firecrawl/blob/main/SELF_HOST.md).
Это AGPL и лишняя операционная поверхность по сравнению с Crawl4AI + текущей
PostgreSQL очередью. Пересмотреть только после нагрузочного PoC.

### CAPTCHA и условия сайтов

CAPTCHA — это не задача для «улучшения агента». Политика должна быть жёсткой:

1. обнаружить challenge/CAPTCHA;
2. завершить browser execution без попыток stealth, solver API, proxy rotation,
   fingerprint spoofing или повторного штурма;
3. сохранить безопасный screenshot/URL/reason без cookies и персональных данных;
4. перевести задачу в `human_required_captcha` либо `official_api_required`;
5. после действия авторизованного оператора продолжить с новым ограниченным lease;
6. если ToS/robots запрещает автоматизацию, остановить источник.

Crawler обязан следовать [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html):
успешно полученные parseable robots rules обязательны, а при недоступности из-за
server/network error стандарт предписывает complete disallow. Robots.txt не заменяет
проверку Terms of Service, лицензии данных и privacy.

### «Самопознание интернета» как безопасный learning loop

- scheduler создаёт bounded research waves по утверждённым темам;
- search выдаёт кандидатов, crawler сохраняет source URL, retrieved time, checksum,
  content type, robots result и extractor version;
- deduplication выполняется до LLM;
- только цитируемые утверждения попадают в append-only Company Brain;
- документы имеют ACL, freshness/expiry и immutable versions;
- prompt injection не меняет tools, policy или system instructions;
- eval сравнивает precision, duplicate rate, stale rate, cost и downstream outcome;
- Meta Brain может предложить новый источник/экстрактор, но не включить его сам.

Для semantic retrieval после отдельного eval можно добавить **pgvector 0.8.x** в тот
же PostgreSQL. Проект поддерживает Postgres 13+, HNSW/IVFFlat, а официальный Python
package работает с SQLAlchemy и Psycopg 3:
[pgvector](https://github.com/pgvector/pgvector),
[pgvector-python/SQLAlchemy](https://github.com/pgvector/pgvector-python).
Embeddings должны быть производными от immutable chunks; ACL и freshness фильтры
применяются до ranking, а lexical retrieval остаётся fallback.

## 4. IP-телефония и voice agents

### Рекомендуемая архитектура

**LiveKit Agents + LiveKit SIP** даёт Python voice runtime, realtime STT/LLM/TTS,
job scheduling, SIP/PSTN и transfer-to-human. Сервер и SIP service можно self-host в
Docker/Kubernetes:
[LiveKit server](https://github.com/livekit/livekit),
[LiveKit Agents](https://github.com/livekit/agents),
[LiveKit SIP](https://github.com/livekit/sip),
[официальный Python outbound-caller example](https://github.com/livekit-examples/outbound-caller-python),
[warm transfer](https://docs.livekit.io/telephony/features/transfers/warm/).

LiveKit не является consent/DNC database и не должен решать, кому звонить. Он
получает уже разрешённый `call_attempt_id` и short-lived token от CleaningAIOS.
Self-hosted SIP требует Redis, UDP 5060 и media range 10000–20000; официальный guide
отмечает, что для Docker обычно нужен host networking. Это отдельный hardened host
или private network segment, не основной web container.

**Twilio Programmable Voice** подходит как первый carrier adapter там, где подтверждены
география, номера, стоимость и договорные условия. Calls API инициирует outbound call,
возвращает provider SID, имеет status callbacks и recording callbacks:
[Call resource](https://www.twilio.com/docs/voice/api/call-resource). Twilio требует
предварительное согласие для telemarketing, disclosure/identity, opt-out и проверку
против применимого DNC registry:
[Voice Services Policy](https://www.twilio.com/en-us/legal/service-country-specific-terms/voice-sip).
Это обязанность клиента, а не автоматическая функция API.

### Обязательный call gate в CleaningAIOS

Перед каждым dial:

- номер нормализован, verified и связан с CRM contact;
- есть consent evidence для точной цели/канала, либо это запрошенный клиентом звонок;
- нет локальной suppression/opt-out и пройден применимый DNC check;
- разрешены страна, timezone/calling hours, тип кампании и caller ID;
- approval связан с неизменяемым campaign digest/recipient set либо точным call;
- соблюдены per-contact retry cap, cooling period, campaign CPS и daily budget;
- есть idempotent `VoiceCallAttempt`; повторный worker не создаёт второй звонок;
- synthetic voice/AI identity и цель раскрываются в начале там, где это требуется;
- публично найденный номер без verified consent нельзя использовать для cold call.

Первый production vertical slice должен быть **inbound или customer-requested
callback**, затем approved follow-up. Массовый outbound нельзя включать до legal
matrix по юрисдикциям и подтверждённых opt-out/DNC тестов.

### Recording, callbacks и CRM

- `record=false` по умолчанию;
- после требуемого уведомления/согласия запись стартует отдельной командой;
- Twilio Recording API позволяет start/stop/pause/resume/delete:
  [Recordings resource](https://www.twilio.com/docs/voice/api/recording);
- recording/transcript получает encryption, ACL, retention deadline и legal hold;
- карточные/банковские данные не пишутся и не отправляются модели;
- любой «не звоните» немедленно завершает selling flow и атомарно создаёт suppression;
- negative intent, voicemail, no-answer, transfer и disconnect — отдельные outcomes;
- LiveKit warm transfer передаёт человеку краткое резюме без запрещённых данных;
- provider webhook принимается только по HTTPS и после signature validation. Twilio
  подписывает webhook `X-Twilio-Signature` и рекомендует SDK validator:
  [Secure webhooks](https://www.twilio.com/docs/usage/webhooks/webhooks-security).

Status callbacks могут прибыть не по порядку, поэтому transition должен быть
монотонным, deduplicated по provider event/Call SID/sequence и не превращать
`completed` в доказательство успешной продажи. Финальный outcome пишет только
CleaningAIOS, после чего ссылка на CRM-карточку входит в системный/CEO отчёт.

## 5. Безопасные автоматические обновления и deploy 24/7

Текущий [deploy_server.sh](../../scripts/deploy_server.sh) уже требует точный SHA,
отказывает на dirty checkout, берёт PostgreSQL backup, выполняет migrations, health
checks и восстанавливает прошлый application commit при ошибке. Операционная модель
описана в [SERVER_AUTOMATION.md](../SERVER_AUTOMATION.md). Это хороший baseline,
который надо расширить, а не заменить агентом с shell/root.

### Целевой pipeline

1. Dependabot открывает PR для Python, Docker и GitHub Actions. GitHub официально
   поддерживает schedules, ecosystems и security/version PR:
   [Dependabot version updates](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-version-updates).
2. CI запускает pytest, agent evals, Ruff, strict mypy, Alembic check, Compose smoke,
   dependency и image scan.
3. Build создаёт один OCI image, SBOM и provenance, публикует его в registry.
   Docker BuildKit и GitHub поддерживают attestations:
   [Docker SBOM/provenance](https://docs.docker.com/build/ci/github-actions/attestations/),
   [GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations).
4. Staging скачивает и проверяет **digest**, не mutable tag. Digest является
   immutable SHA-256 content identifier:
   [Docker image digests](https://docs.docker.com/dhi/explore/security-concepts/digests/).
5. Staging выполняет migration rehearsal, HTTP/worker/scheduler/bot smoke, agent
   evals и provider sandbox tests.
6. Production environment допускает только tested digest и ограничивает secrets/
   branch. GitHub Environments поддерживает branch restrictions, approvals и custom
   protection rules:
   [deployment environments](https://docs.github.com/en/actions/concepts/workflows-and-actions/deployment-environments).
7. После deploy идёт observation window по health, error rate, task lag и delivery
   failures. При пороге откатывается application image; DB downgrade автоматически
   не выполняется.

Для минимального участия владельца можно auto-promote patch/minor changes при всех
зелёных проверках и отсутствии schema/protected-capability изменений. Major updates,
новая migration, новый provider, расширение permissions, voice/outreach policy и
изменение approval logic требуют review. Это не торможение, а условие, позволяющее
остальной поток безопасно автоматизировать.

**Renovate** полезен вместо/поверх Dependabot, когда нужны package groups,
dependency dashboard, schedules и более тонкие automerge rules; он поддерживает
Python, Docker и GitHub Actions и доступен self-hosted:
[официальный репозиторий](https://github.com/renovatebot/renovate),
[self-hosting](https://github.com/renovatebot/renovate/blob/main/docs/usage/examples/self-hosting.md).
В текущем масштабе начать с Dependabot; self-hosted Renovate требует отдельного bot
identity и сам имеет широкие права на репозитории.

**Watchtower не использовать в production.** На дату исследования GitHub repository
архивирован, а его собственный README говорит, что продукт предназначен для homelab/
local environments и не рекомендуется для commercial/production:
[containrrr/watchtower](https://github.com/containrrr/watchtower). Автоматическая
замена running container по изменившемуся tag обходит CI, schema rehearsal,
attestation и release evidence.

## 6. Порядок внедрения

### Фаза A. Internet read plane

- ввести `ResearchFetch`/`BrowserRun` как отдельные durable records;
- реализовать URL policy, SSRF/redirect validation, robots, rate limits и budgets;
- сохранить внутренний Crawl4AI 0.9.3 sidecar по exact digest и при доказанной
  потребности добавить отдельный Scrapy worker для массовых статических источников;
- хранить provenance/checksum/freshness и писать только через обычный domain service;
- CAPTCHA переводит задачу в `human_required`, без solver.

**Gate:** тесты на localhost/private-IP/redirect SSRF, oversized content, robots
disallow, 429, timeout, prompt injection, duplicate fetch и CAPTCHA stop.

### Фаза B. CRM/reporting

- сделать canonical CRM deep link configurable;
- завершить cross-channel company/contact deduplication;
- добавить call timeline и next action в существующий PostgreSQL;
- только при реальной UX-потребности запустить read-mostly Twenty pilot.

**Gate:** replay webhook не создаёт дубль, conflict виден, PII не попадает в audit и
LLM traces, ссылки из отчёта ведут к авторизованной карточке.

### Фаза C. Voice vertical slice

- carrier-neutral interface + LiveKit Agents/SIP;
- начать с inbound/requested callback и human transfer;
- реализовать consent/DNC/suppression/calling-hours gate и signed webhooks;
- recording off until consent, bounded retention;
- затем малый approved follow-up batch с campaign digest.

**Gate:** opt-out во время звонка немедленно блокирует следующие attempts; retry не
звонит дважды; DNC/consent failure не достигает provider; recording без согласия не
создаётся; каждый outcome и provider SID виден в CRM.

### Фаза D. Artifact-based continuous delivery

- Dependabot PRs, lockfile, container scan, SBOM/provenance;
- build once, deploy same verified digest;
- staging/migration rehearsal, production environment gate, observation/rollback;
- регулярный restore drill с сохранённым evidence.

**Gate:** broken image и failed health автоматически откатываются; irreversible
migration блокирует auto-promotion; production никогда не тянет `latest`.

## 7. Решения, которые сознательно не рекомендуются

- CAPTCHA solvers, stealth plugins, fingerprint spoofing, proxy rotation ради обхода
  блокировки или ограничения сайта.
- Произвольный browser/shell/HTTP для каждого агента.
- Cold calling по публично найденным номерам без verified consent и DNC check.
- Отключение RBAC, audit, approval, suppression, unsubscribe или rate limits.
- Прямые write-tools у Claude, Jarvis, CEO или Meta Brain. Они создают proposal/task;
  внешний эффект исполняет policy-controlled worker.
- Две master CRM, shared database schema с Twenty/Odoo или best-effort dual-write.
- Unbounded self-modification, прямой commit/merge/deploy из production agent.
- Watchtower/`latest` как механизм production release.
- 60 постоянных agent loops без очереди, budgets, termination и outcome metrics.

## Вывод

Максимальная практическая автономность для CleaningAIOS достигается не максимальными
permissions, а максимальным числом **заранее разрешённых, наблюдаемых и обратимых
путей**. Текущий проект уже имеет подходящие контрольные границы. Лучшее соотношение
скорости и риска даёт точечная интеграция Crawl4AI/Scrapy, Twenty при подтверждённой
UX-потребности, LiveKit/SIP с carrier adapter и artifact-based CI/CD, при сохранении
PostgreSQL, Task, approvals и audit единственными источниками истины.
