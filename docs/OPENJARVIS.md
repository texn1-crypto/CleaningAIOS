# OpenJarvis in CleaningAIOS

OpenJarvis is an advisory-only personal assistant exposed to authorized Telegram
operators through `/jarvis <question>`. Every request first passes through the
existing Request Analyst. OpenJarvis does not receive CleaningAIOS tools, database
credentials, customer data, or authority to execute tasks.

## Production topology

- `ollama` serves `qwen3:0.6b`; deployment and the watchdog verify the expected
  model ID `7df6b6e09427` so a changed upstream tag cannot be accepted silently.
- `openjarvis` exposes its OpenAI-compatible API only on the internal `jarvis`
  Docker network. No host port is published.
- `bot` is the only CleaningAIOS application service attached to that network.
- `OPENJARVIS_API_KEY` protects the API and is generated in the server `.env`
  during deployment without printing its value.
- OpenJarvis runs as UID/GID 10001 with a read-only root filesystem, all Linux
  capabilities dropped, `no-new-privileges`, bounded memory/PIDs, and temporary
  writable state only.

The main PostgreSQL workflow, RBAC, approval, audit, consent, suppression, and
idempotency controls remain authoritative. Advice that should become work must be
submitted as a normal CleaningAIOS task and follows those controls.

## Owner usage

In the private authorized Telegram chat, send:

```text
/jarvis Как улучшить план продаж на следующую неделю?
```

Do not include passwords, tokens, payment details, or customer personal data.
The adapter redacts common secret patterns before inference, but redaction is a
last line of defence rather than permission to send sensitive data.

## Deployment and verification

Production deployment builds the pinned OpenJarvis package, starts the isolated
services, pulls the fixed model name, verifies service health, makes a real
advisory request from the bot container, and then verifies all container states.
The server watchdog checks both Jarvis containers and confirms that the model is
still present without downloading it automatically. The Python base image,
OpenJarvis release, and its resolved dependency versions are pinned for repeatable
rebuilds. Ollama is attached to a dedicated empty outbound network only while the
pinned model is downloaded; deployment disconnects and removes that network before
OpenJarvis starts. Its steady-state network remains internal-only. The local model
runs with one inference slot, one loaded model, a 4,096-token context window, and a
bounded response budget so it fits predictably on the 4 GB production VM.

OpenJarvis upstream: <https://github.com/open-jarvis/OpenJarvis>
