from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_server_shell_scripts_are_syntactically_valid() -> None:
    scripts = [
        ROOT / "scripts" / "deploy_server.sh",
        ROOT / "scripts" / "server_watchdog.sh",
        ROOT / "scripts" / "install_server_watchdog.sh",
    ]
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)


def test_deploy_requires_ci_main_and_pinned_host_identity() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text()
    assert 'workflows: ["CI"]' in workflow
    assert "workflow_run.conclusion == 'success'" in workflow
    assert "workflow_run.head_branch == 'main'" in workflow
    assert "workflow_dispatch" not in workflow
    assert "PRODUCTION_SSH_KNOWN_HOSTS" in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "environment: production" in workflow


def test_watchdog_repairs_runtime_without_updating_code() -> None:
    watchdog = (ROOT / "scripts" / "server_watchdog.sh").read_text()
    assert "--no-build" in watchdog
    assert "git pull" not in watchdog
    assert "git checkout" not in watchdog


def test_deploy_is_exact_release_and_refuses_dirty_checkout() -> None:
    deploy = (ROOT / "scripts" / "deploy_server.sh").read_text()
    assert 'TARGET_SHA" != "$remote_main' in deploy
    assert "main:refs/remotes/origin/main" in deploy
    assert "git_safe status --porcelain" in deploy
    assert "Production checkout contains local changes; deployment refused" in deploy
    assert "telegram_getme=ok" in deploy
