# Crawl4AI public-web gateway

CleaningAI OS uses Crawl4AI as one isolated, read-only web extraction service.
Agents do not receive a browser, arbitrary HTTP client or direct access to the
Crawl4AI API. They can request only the registered `web.public_crawl` tool.

The deployment is pinned to the multi-platform OCI digest for
`unclecode/crawl4ai:0.9.3`, not only its mutable tag. The digest was verified from
the registry on 2026-09-17. That release is used because it contains the current
Docker-server security fixes. Review the
[official 0.9.3 release notes](https://github.com/unclecode/crawl4ai/releases/tag/v0.9.3)
and the [secure Docker migration guide](https://github.com/unclecode/crawl4ai/blob/main/deploy/docker/MIGRATION.md)
before changing the version.

## Enable the service

Keep the token only in the untracked `.env` file or the production secret store.
The repository's secret bootstrapper can generate it locally without printing
the value and enable the safe defaults:

```bash
python scripts/bootstrap_secrets.py --enable-crawl4ai .env
```

The resulting configuration is equivalent to:

```text
CRAWL4AI_ENABLED=true
CRAWL4AI_BASE_URL=http://crawl4ai:11235
CRAWL4AI_API_TOKEN=<private-random-token>
AGENT_READ_TOOL_TIMEOUT_SECONDS=35
AGENT_READ_TOOL_TOTAL_TIMEOUT_SECONDS=45
```

Start or recreate the stack with the optional crawler profile:

```bash
docker compose --profile crawl up -d
```

The crawler has no published host port. It is not attached to the Compose
`backend` network that contains PostgreSQL, the scheduler and the bot. Only the
web and worker services share its dedicated `crawl` network so they can make
authenticated requests. Crawl4AI requires the same bearer token on every API
request; the application never returns or logs that value.

Readiness reports one of `disabled`, `credentials_required`,
`invalid_configuration`, or `configured` under `public_web_research`. It reports
configuration state only, not the credential.

## Agent contract

A permitted task can include one page request:

```json
{
  "read_only_tools": [
    {
      "name": "web.public_crawl",
      "arguments": {
        "url": "https://example.org/public-page",
        "max_chars": 12000
      }
    }
  ]
}
```

The current allowlist covers research, tender, sales, marketing, growth,
request-analysis, copy/creative and public lead-scouting roles. Finance, HR and
system-administration agents do not receive the crawler. Add a role only after a
concrete need and a policy test.

The result contains bounded Markdown, source URLs, a content checksum and
`untrusted_external_data=true`. Web text is evidence, never an instruction. It
cannot authorize outreach, publication, a tender submission, a contract, a
payment or any other external action.

## Enforced safety boundary

- one URL and at most one crawl call per agent run, HTTPS port 443 only;
- the application rejects local, private, reserved and direct-IP targets before
  forwarding; the container's own per-hop SSRF enforcement is the authoritative
  fetch-time boundary and must pass the live check below;
- no request-supplied JavaScript, browser arguments, cookies, headers, proxy,
  deep-crawl strategy, hooks, file path, screenshots, PDF export or LLM config;
- robots rules are checked by the crawler for every request; CAPTCHA, logins,
  paywalls and anti-bot controls are never bypassed;
- no redirects between the application and the Crawl4AI service;
- Crawl4AI's own authenticated 0.9.3 SSRF boundary remains active;
- response-body, Markdown, call-count, per-call, total-time and stored-result
  limits are enforced;
- every attempted tool call is recorded in `agent_tool_calls` using an argument
  digest rather than the raw URL or returned content.

Operators remain responsible for robots rules, source terms, copyright and data
protection. Do not use the tool to bypass logins, paywalls, access controls or
anti-bot measures.

## Verification

Run the focused tests and inspect the effective Compose configuration:

```bash
pytest -q tests/test_crawl4ai.py tests/test_agent_tools.py
docker compose --env-file .env.example --profile crawl config
bash scripts/verify_crawl4ai.sh
```

The service should stay disabled when no token is configured. Before production
activation, a live test against the pinned image must prove that a harmless
public page succeeds, link-local/RFC1918 destinations fail, and the crawler
cannot resolve or connect to `db`. The test must never print the token.
