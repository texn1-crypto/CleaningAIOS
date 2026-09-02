# Agent observability

`GET /api/observability/agents?window_hours=24` returns an authenticated,
aggregate-only snapshot of agent runs and workflow queues. It includes success
rate, p95 duration, recorded cost, stale running records, per-agent outcomes,
task/event status counts and pending approvals. It never includes run input,
output, errors, message bodies, tokens or customer data.

`GET /metrics` exposes the same safe aggregate as Prometheus text. Production
scrapers must send a configured `X-API-Key`; anonymous production access returns
401. Do not put the API key in the scrape URL or logs.

The initial service objectives are configurable:

- `AGENT_SLO_SUCCESS_RATE_PERCENT=95`
- `AGENT_SLO_P95_DURATION_SECONDS=300`
- `AGENT_STALE_RUN_MINUTES=15`
- `AGENT_SLO_WINDOW_HOURS=24`

An empty result is `insufficient_data`, not healthy. Any missed objective makes
the aggregate status `degraded`. The duration sample is capped at 10,000 recent
runs and the response reports when the cap was applied.

Web, worker, scheduler and Telegram bot emit one-line JSON logs by default. Web
responses include `X-Correlation-ID`; a valid caller-supplied value is preserved,
otherwise the service creates one. Only the URL path is logged, never its query
string. Common credential forms and configured service secrets are redacted from
messages and exception text. Set `LOG_FORMAT=text` only for local debugging.
