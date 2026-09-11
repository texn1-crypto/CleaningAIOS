from __future__ import annotations

import json
import resource
import sys
from pathlib import Path
from typing import Any


_ERROR_PAGE_LIMIT = "page_limit"
_ERROR_PAGE_TEXT_LIMIT = "page_text_limit"
_ERROR_TOTAL_TEXT_LIMIT = "total_text_limit"
_ERROR_ENCRYPTED = "encrypted"
_ERROR_PARSE = "parse_failed"


def _apply_resource_limits(*, memory_bytes: int, cpu_seconds: int) -> None:
    """Constrain the untrusted parser process before importing pypdf."""

    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _extract(
    path: Path,
    *,
    max_pages: int,
    max_page_text_chars: int,
    max_total_text_chars: int,
    max_segments: int,
) -> dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        return {"ok": False, "error": _ERROR_ENCRYPTED}
    if len(reader.pages) > max_pages:
        return {"ok": False, "error": _ERROR_PAGE_LIMIT}

    total_text_chars = 0
    segments: list[list[str]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if len(text) > max_page_text_chars:
            return {"ok": False, "error": _ERROR_PAGE_TEXT_LIMIT}
        total_text_chars += len(text)
        if total_text_chars > max_total_text_chars:
            return {"ok": False, "error": _ERROR_TOTAL_TEXT_LIMIT}
        for paragraph_number, raw in enumerate(text.splitlines(), start=1):
            if len(segments) < max_segments:
                segments.append(
                    [f"page {page_number}, line {paragraph_number}", raw]
                )
    return {"ok": True, "segments": segments}


def main() -> int:
    if len(sys.argv) != 8:
        return 2
    try:
        path = Path(sys.argv[1])
        max_pages = int(sys.argv[2])
        max_page_text_chars = int(sys.argv[3])
        max_total_text_chars = int(sys.argv[4])
        max_segments = int(sys.argv[5])
        memory_bytes = int(sys.argv[6])
        cpu_seconds = int(sys.argv[7])
        if min(
            max_pages,
            max_page_text_chars,
            max_total_text_chars,
            max_segments,
            memory_bytes,
            cpu_seconds,
        ) <= 0:
            return 2
        _apply_resource_limits(
            memory_bytes=memory_bytes,
            cpu_seconds=cpu_seconds,
        )
        payload = _extract(
            path,
            max_pages=max_pages,
            max_page_text_chars=max_page_text_chars,
            max_total_text_chars=max_total_text_chars,
            max_segments=max_segments,
        )
    except BaseException:
        payload = {"ok": False, "error": _ERROR_PARSE}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
