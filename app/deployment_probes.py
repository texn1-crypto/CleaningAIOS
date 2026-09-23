from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request


class TelegramProbeFailed(RuntimeError):
    pass


def verify_telegram_identity(token: str, base_url: str = "https://api.telegram.org") -> int:
    """Retry only a read-only identity probe; never log its credential-bearing URL."""
    token = token.strip()
    if not token:
        raise TelegramProbeFailed("Telegram bot token is missing")
    url = f"{base_url.rstrip('/')}/bot{token}/getMe"
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                body = response.read(65_537)
            if len(body) > 65_536:
                raise TelegramProbeFailed("Telegram getMe response is too large")
            payload = json.loads(body)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if not 500 <= status < 600:
                raise TelegramProbeFailed(f"Telegram getMe rejected: HTTP {status}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise TelegramProbeFailed("Telegram TLS verification failed") from None
        except ssl.SSLError:
            raise TelegramProbeFailed("Telegram TLS verification failed") from None
        except (TimeoutError, ConnectionError):
            pass
        except (ValueError, UnicodeError):
            raise TelegramProbeFailed("Telegram getMe returned invalid JSON") from None
        else:
            result = payload.get("result") if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict) or payload.get("ok") is not True
                or not isinstance(result, dict) or result.get("is_bot") is not True
                or type(result.get("id")) is not int or result["id"] <= 0
            ):
                raise TelegramProbeFailed("Telegram getMe did not confirm bot identity")
            return attempt
        if attempt == 3:
            raise TelegramProbeFailed("Telegram getMe unavailable after 3 attempts") from None
        time.sleep(2 ** attempt)
    raise AssertionError("Unreachable probe state")
