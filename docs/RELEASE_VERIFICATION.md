# Release verification

On 2026-09-23, deployment run `35930759164` attempts 1 and 2 both passed
application health for `e36cd6d184d4bcb9973d686d1c076f5fe54fb630`, then failed a
single Telegram `getMe` connection attempt after ten seconds. The built-in rollback
restored `1a7b53f841d2f0aae1510249eed4cb7f2fd68be0`. The database remained running;
no downgrade or data restore was performed. A later identical read-only Telegram
probe succeeded in 0.17 seconds. The exact underlying network cause is unverified.

The release gate now delegates to `app.deployment_probes.verify_telegram_identity`.
It makes at most three `getMe` reads with ten-second socket timeouts, waiting two
then four seconds only after transient transport or HTTP 5xx failure. A socket
timeout is not a process-wide deadline. Authentication errors, HTTP 429, TLS
verification failures and invalid identities fail immediately. The credential-bearing
URL and provider exception text never appear in the probe's failure output.
All failures still stop deployment and retain the existing rollback behavior.
This retry policy applies to the read-only release check, never to business actions.

`tests/test_deployment_probes.py` verifies bounded retries, success, exhaustion,
credential redaction, auth/rate-limit/TLS failure and malformed identity responses.
Live release evidence is recorded in the associated pull request, not inferred
from these isolated transport fixtures.

Local validation for this change on 2026-09-23: 600 pytest cases passed (including
17 release-probe cases), Ruff and all CI strict-mypy targets passed, 37 agent
intent evals passed, 100/100 quality criteria had passing executed regression
evidence, and Compose configuration plus deployment/watchdog shell syntax passed.
The independent read-only Claude review timed out after 100 seconds without
changing the working tree; this is not a review approval. PostgreSQL migration,
container and live-provider checks remain release gates, not claims from local tests.
