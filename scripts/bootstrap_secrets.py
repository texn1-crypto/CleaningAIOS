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
)


def bootstrap(path: Path) -> list[str]:
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
        if name in SECRET_NAMES:
            positions[name] = index
            values[name] = value.strip()

    updated: list[str] = []
    for name in SECRET_NAMES:
        if values.get(name):
            continue
        replacement = f"{name}={secrets.token_urlsafe(48)}"
        if name in positions:
            lines[positions[name]] = replacement
        else:
            lines.append(replacement)
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
    args = parser.parse_args()
    updated = bootstrap(args.path)
    print("Configured missing secrets: " + (", ".join(updated) if updated else "none"))


if __name__ == "__main__":
    main()
