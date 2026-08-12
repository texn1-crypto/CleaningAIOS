# Baseline before production work

Captured from commit `d095a04` before source changes.

- Repository contained only `CleaningAIOS_Real_MVP.zip`.
- `python3 -m pytest -q` failed with `No module named pytest`.
- `docker --version`, `docker compose version`, and `docker compose up --build -d` failed with `command not found: docker`.
- Original Compose required `POSTGRES_PASSWORD`, `TELEGRAM_BOT_TOKEN`, and `OWNER_TELEGRAM_ID` without startup validation.
- Original application created tables at runtime and had no migrations, RBAC, audit log, workers, scheduler, CI, or production rollback procedure.

The original ZIP remains tracked at repository root so the uploaded artifact and Git history are preserved.
