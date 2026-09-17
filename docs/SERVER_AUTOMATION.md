# Server automation for the Telegram runtime

## Operating model

The production Telegram bot runs only on the server. Developer laptops must not
start the `bot` Compose profile with the production token. The server keeps one
polling process, while `web`, `worker`, `scheduler`, PostgreSQL and Crawl4AI run in
the same reviewed release.

Code updates are release-gated:

1. a change is merged to `main`;
2. GitHub CI runs tests, static checks, agent evals, dependency audit, migrations
   and a Compose smoke test;
3. only a successful CI run for the current `main` SHA can trigger production;
4. the server takes a database backup, checks out that exact SHA, runs migrations,
   rebuilds the application and verifies HTTP health plus Telegram `getMe`;
5. a failed release restores the previous application commit. Database downgrade
   is intentionally not automatic, so migrations must remain backward compatible.

The watchdog runs every two minutes and recreates a missing, exited or unhealthy
container without fetching code. Docker's `restart: unless-stopped` policy covers
process exits and host restarts. GitHub Actions is the only automatic code-release
path.

## Controlled learning and repair

`Meta Brain`, `Evolution Research`, agent evals and `System Admin` form the quality
loop. They may collect aggregate outcomes, find failures and create deduplicated
improvement proposals. They cannot edit production, merge a pull request, weaken a
test, publish content, send outreach or approve their own action. A code change is
released only after repository review and the same CI gate described above.

This distinction is deliberate:

- runtime self-healing may restart a failed service;
- measured learning may create an evidence-backed improvement proposal;
- source-code self-modification is not allowed in production.

## Required GitHub production settings

Create a GitHub environment named `production`. Add these environment secrets:

- `PRODUCTION_SSH_HOST`: server hostname or IPv4 address;
- `PRODUCTION_SSH_USER`: non-root deployment user with Docker access;
- `PRODUCTION_SSH_KEY`: dedicated private deployment key;
- `PRODUCTION_SSH_KNOWN_HOSTS`: pinned server host-key line produced and verified
  by the server administrator.

Add these environment variables when defaults are not correct:

- `PRODUCTION_SSH_PORT`, default `22`;
- `PRODUCTION_PATH`, default `/opt/cleaningaios`.

Never paste those secret values into a task, issue, pull request, Telegram message
or application log.

## One-time server prerequisites

The deployment user needs Docker Compose, Git, curl, flock and cron. The repository
must already be cloned at `PRODUCTION_PATH`, with `origin` pointing to
`texn1-crypto/CleaningAIOS`. The server `.env` stays only on the server and must
contain the Telegram owner binding, callback secret, database password and other
production credentials.

The first successful deployment elevates through the deployment user's configured
passwordless `sudo` rule, then installs the watchdog in root's crontab. Its log is
stored outside the repository in `cleaningaios-runtime/watchdog.log`.

## Recovery evidence

GitHub Actions records the exact deployed SHA and failed deployment logs. The
application `/health` response must report the same SHA. Telegram validation prints
only `telegram_getme=ok`; it never prints the token. Pre-deployment database backups
are written outside the repository, under a mode-700 directory next to the checkout
unless `CLEANINGAIOS_BACKUP_DIR` selects another protected location.
