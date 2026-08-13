"""Store one Mail.ru application password for SMTP and IMAP without echoing it."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import tempfile


PASSWORD_NAMES = ("SMTP_PASSWORD", "IMAP_MAILRU_PASSWORD")


def configure(path: Path, password: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Environment file not found: {path}")
    if len(password) < 8 or "\n" in password or "\r" in password:
        raise ValueError("Application password is invalid")
    lines = path.read_text().splitlines()
    found: set[str] = set()
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name in PASSWORD_NAMES:
            lines[index] = f"{name}={password}"
            found.add(name)
    for name in PASSWORD_NAMES:
        if name not in found:
            lines.append(f"{name}={password}")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    path = Path(".env")
    password = getpass.getpass("Mail.ru application password (input is hidden): ")
    confirmation = getpass.getpass("Repeat application password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    configure(path, password)
    print("Mail.ru SMTP/IMAP application password configured; value was not printed.")


if __name__ == "__main__":
    main()
