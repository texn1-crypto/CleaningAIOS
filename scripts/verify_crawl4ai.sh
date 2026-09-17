#!/usr/bin/env bash

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

docker compose --profile crawl exec -T crawl4ai python - <<'PY'
import json
import os
import socket
import urllib.error
import urllib.request


token = os.environ.get("CRAWL4AI_API_TOKEN", "").strip()
if not token:
    raise SystemExit("Crawl4AI container has no API token")


def crawl(url: str) -> tuple[int, dict]:
    payload = json.dumps(
        {
            "urls": [url],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {"stream": False, "cache_mode": "bypass"},
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:11235/crawl",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(2_000_000))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        return exc.code, body


public_status, public = crawl("https://example.com/")
public_rows = public.get("results") if isinstance(public, dict) else None
if (
    public_status != 200
    or public.get("success") is not True
    or not isinstance(public_rows, list)
    or not public_rows
    or public_rows[0].get("success") is not True
):
    raise SystemExit("Public Crawl4AI smoke request failed")

for private_url in (
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "https://127.0.0.1/",
):
    status, body = crawl(private_url)
    rows = body.get("results") if isinstance(body, dict) else None
    succeeded = (
        status == 200
        and body.get("success") is True
        and isinstance(rows, list)
        and rows
        and rows[0].get("success") is True
    )
    if succeeded:
        raise SystemExit("Crawl4AI accepted a forbidden private destination")

try:
    socket.getaddrinfo("db", 5432)
except socket.gaierror:
    pass
else:
    raise SystemExit("Crawl4AI can resolve the isolated PostgreSQL service")

print("Crawl4AI live verification passed: public crawl, SSRF rejection, DB isolation")
PY
