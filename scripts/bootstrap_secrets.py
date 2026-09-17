"""Generate missing local application secrets without printing their values."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import tempfile


SECRET_NAMES = (
    "TELEGRAM_CALLBACK_SECRET",
    "UNSUBSCRIBE_SECRET",
    "PUBLIC_LEAD_RATE_SECRET",
    "OPENJARVIS_API_KEY",
)
CRAWL4AI_SECRET_NAME = "CRAWL4AI_API_TOKEN"
CRAWL4AI_SETTINGS = {
    "CRAWL4AI_ENABLED": "true",
    "CRAWL4AI_BASE_URL": "http://crawl4ai:11235",
    "AGENT_READ_TOOL_TIMEOUT_SECONDS": "35",
    "AGENT_READ_TOOL_TOTAL_TIMEOUT_SECONDS": "45",
}


def _set_value(
    lines: list[str],
    positions: dict[str, int],
    values: dict[str, str],
    name: str,
    value: str,
) -> bool:
    if values.get(name) == value:
        return False
    replacement = f"{name}={value}"
    if name in positions:
        lines[positions[name]] = replacement
    else:
        positions[name] = len(lines)
        lines.append(replacement)
    values[name] = value
    return True


def bootstrap(path: Path, *, enable_crawl4ai: bool = False) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Environment file not found: {path}")
    lines = path.read_text().splitlines()
    positions: dict[str, int] = {}
    values: dict[str, str] = {}
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name in {*SECRET_NAMES, CRAWL4AI_SECRET_NAME, *CRAWL4AI_SETTINGS}:
            positions[name] = index
            values[name] = value.strip()

    updated: list[str] = []
    secret_names = (*SECRET_NAMES, *((CRAWL4AI_SECRET_NAME,) if enable_crawl4ai else ()))
    for name in secret_names:
        if values.get(name):
            continue
        replacement = f"{name}={secrets.token_urlsafe(48)}"
        if name in positions:
            lines[positions[name]] = replacement
        else:
            lines.append(replacement)
        updated.append(name)

    if enable_crawl4ai:
        for name, value in CRAWL4AI_SETTINGS.items():
            if _set_value(lines, positions, values, name, value):
                updated.append(name)

    if not updated:
        return []
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=Path(".env"))
    parser.add_argument(
        "--enable-crawl4ai",
        action="store_true",
        help="Generate the private Crawl4AI service token and enable its safe defaults.",
    )
    args = parser.parse_args()
    updated = bootstrap(args.path, enable_crawl4ai=args.enable_crawl4ai)
    print("Configured missing secrets: " + (", ".join(updated) if updated else "none"))


if __name__ == "__main__":
    main()
