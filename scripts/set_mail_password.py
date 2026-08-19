"""Store an application password for SMTP without echoing it."""

from __future__ import annotations

import getpass
import argparse
import os
from pathlib import Path
import re
import tempfile


PASSWORD_NAMES = ("SMTP_PASSWORD", "IMAP_MAILRU_PASSWORD")
SMTP_PROVIDERS = {
    "gmail": ("smtp.gmail.com", 587),
    "mailru": ("smtp.mail.ru", 465),
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _update(path: Path, values: dict[str, str]) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Environment file not found: {path}")
    lines = path.read_text().splitlines()
    found: set[str] = set()
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name in values:
            lines[index] = f"{name}={values[name]}"
            found.add(name)
    for name, value in values.items():
        if name not in found:
            lines.append(f"{name}={value}")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure(path: Path, password: str) -> None:
    if len(password) < 8 or "\n" in password or "\r" in password:
        raise ValueError("Application password is invalid")
    _update(path, {name: password for name in PASSWORD_NAMES})


def configure_smtp(path: Path, provider: str, email: str, password: str) -> None:
    provider = provider.strip().lower()
    email = email.strip().lower()
    password = password.replace(" ", "")
    if provider not in SMTP_PROVIDERS:
        raise ValueError("Unsupported SMTP provider")
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("Sender email is invalid")
    if len(password) < 12 or "\n" in password or "\r" in password:
        raise ValueError("Application password is invalid")
    smtp_host, smtp_port = SMTP_PROVIDERS[provider]
    values = {
        "SMTP_HOST": smtp_host,
        "SMTP_PORT": str(smtp_port),
        "SMTP_USERNAME": email,
        "SMTP_PASSWORD": password,
        "SMTP_FROM_EMAIL": email,
    }
    if provider == "mailru":
        values.update(
            {
                "IMAP_MAILRU_USERNAME": email,
                "IMAP_MAILRU_PASSWORD": password,
            }
        )
    _update(path, values)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=sorted(SMTP_PROVIDERS), default="mailru")
    parser.add_argument("--email")
    parser.add_argument("--path", type=Path, default=Path(".env"))
    args = parser.parse_args()
    email = (args.email or input("Sender email: ")).strip()
    password = getpass.getpass("Application password (input is hidden): ")
    confirmation = getpass.getpass("Repeat application password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    configure_smtp(args.path, args.provider, email, password)
    print(f"{args.provider} SMTP application password configured; value was not printed.")


if __name__ == "__main__":
    main()
