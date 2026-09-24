from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import settings
from .daily_owner_pack import _build_pdf
from .integrations import _DNSPinningTransport
from .models import AuditLog, BusinessRecord, OwnerNotification
from .notifications import _verified_document_attachment, queue_owner_notification
from .platform import event_bus


ORIGIN = "https://www.b2b-center.ru"
CATALOGS = (
    f"{ORIGIN}/search/sankt-peterburg/uborka-pomeshhenij/",
    f"{ORIGIN}/search/leningradskaya-oblast/uborka-pomeshhenij/",
)
ROSELTORG_ORIGIN = "https://www.roseltorg.ru"
# Observed public GET form. Region is the CUSTOMER's region, not delivery evidence.
ROSELTORG_CATALOG = ROSELTORG_ORIGIN + "/procedures/search?query_field=%D1%83%D0%B1%D0%BE%D1%80%D0%BA&region%5B%5D=47&region%5B%5D=78"
SOURCE_PROFILE = "b2b-roseltorg-v1"
USER_AGENT = "CleaningAIOS"
MAX_BYTES = 2_000_000
REPORT_TYPE = "tender_search_report"
SOURCE = "public_procurement_catalog"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _clean(value: str) -> str:
    return " ".join(value.split())


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    content: str = ""
    children: list[Node] = field(default_factory=list)

    def find(self, css: str = "", tag: str = "") -> list[Node]:
        result = []
        for child in self.children:
            if (not css or css in child.attrs.get("class", "").split()) and (not tag or tag == child.tag):
                result.append(child)
            result.extend(child.find(css, tag))
        return result


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes += 1
        if self.nodes > 50_000 or len(self.stack) > 80:
            raise ValueError("page_structure_limit")
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if any(node.tag in {"script", "style", "noscript"} for node in self.stack):
            return
        for node in self.stack:
            node.content += data + " "


def _page(html: str) -> Node:
    parser = PageParser()
    parser.feed(html)
    return parser.root


def _first(node: Node, css: str) -> str:
    found = node.find(css)
    return _clean(found[0].content) if found else ""


def _date(node: Node, css: str) -> str:
    groups = node.find(css)
    dates = groups[0].find(tag="time") if groups else []
    if not dates:
        return ""
    try:
        return datetime.strptime(dates[0].attrs.get("datetime", ""), "%d.%m.%Y %H:%M:%S %z").astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def normalize_keywords(raw: object = None) -> list[str]:
    if raw is None:
        raw = settings.tender_search_keywords.split("|")
    if isinstance(raw, str):
        raw = raw.split("|")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError("Provide 1 to 8 public procurement keywords")
    words = sorted(set(_clean(str(item)).lower().replace("ё", "е") for item in raw))
    if any(not re.fullmatch(r"[а-яa-z0-9 -]{2,80}", word) for word in words):
        raise ValueError("Keywords must be public words, not URLs, credentials or contacts")
    return words


def _matches(title: str, keywords: list[str]) -> bool:
    value = title.lower().replace("ё", "е")
    # Match Russian inflections without allowing generic supplies to match body text.
    stems = {"уборка": "уборк", "уборки": "уборк", "клининг": "клининг", "мойка": "мойк", "мытье": "мыть"}
    return any(all(re.search(r"\b" + re.escape(stems.get(token, token)), value) for token in keyword.split()) for keyword in keywords)


def parse_catalog(html: str, source_url: str) -> list[dict[str, Any]]:
    root = _page(html)
    cards = root.find("card-item")
    if not cards:
        # A layout change or a challenge must not become a successful empty search.
        raise ValueError("catalog_layout_unrecognized")
    result: list[dict[str, Any]] = []
    for card in cards[:100]:
        title = _first(card, "card-item__title")
        details = [urljoin(ORIGIN, link.attrs.get("href", "")) for link in card.find(tag="a")
                   if re.fullmatch(r"/search/number/[a-zA-Z0-9-]+/", link.attrs.get("href", ""))]
        if not title or not details:
            continue
        official = ""
        identity = ""
        for link in card.find(tag="a"):
            parts = urlsplit(link.attrs.get("href", ""))
            number = parse_qs(parts.query).get("regNumber", [""])[0]
            if parts.hostname == "zakupki.gov.ru" and re.fullmatch(r"\d{11,19}", number):
                identity = "eis:" + number
                if re.fullmatch(r"/epz/order/notice/[a-z0-9/]+/common-info\.html", parts.path):
                    official = f"https://zakupki.gov.ru{parts.path}?regNumber={number}"
                break
        price = next((n.attrs.get("content", "") for n in card.find(tag="meta") if n.attrs.get("itemprop") == "price"), "")
        try:
            amount = Decimal(re.sub(r"\s", "", price).replace(",", "."))
            price = str(amount) if amount.is_finite() and amount > 0 else ""
        except InvalidOperation:
            price = ""
        result.append({
            "identity": identity or "b2b:" + urlsplit(details[0]).path.split("/")[-2],
            "title": title[:1000], "customer": _first(card, "card-item__organization-name")[:800],
            "source_url": source_url, "detail_url": details[0], "official_url": official,
            "deadline_at": _date(card, "card-item__info-end-date"),
            "published_at": _date(card, "card-item__info-main"), "initial_price_rub": price,
        })
    return result


def delivery_address(html: str) -> str:
    root = _page(html)
    for node in root.find("content-address"):
        value = _clean(node.content)
        if "Адрес поставки:" in value:
            return value.split("Адрес поставки:", 1)[1][:1600].strip()
    # The detail page's markup uses the label inside its address container.
    for node in root.find():
        if any("content-address__text" in x.attrs.get("class", "") for x in node.children):
            value = _clean(node.content)
            if "Адрес поставки:" in value:
                return value.split("Адрес поставки:", 1)[1][:1600].strip()
    return ""


def parse_roseltorg_catalog(html: str, source_url: str) -> list[dict[str, Any]]:
    cards = _page(html).find("search-results__item")
    if not cards:
        raise ValueError("catalog_layout_unrecognized")
    result: list[dict[str, Any]] = []
    for card in cards[:100]:
        number = card.attrs.get("data-feature-favorite-lots-procedure-number", "")
        lot = card.attrs.get("data-feature-favorite-lots-lot-number", "")
        if not re.fullmatch(r"[A-Z0-9-]{1,64}", number) or not re.fullmatch(r"[1-9][0-9]{0,5}", lot):
            continue
        path = f"/procedure/{number}/{lot}"
        links = [n for n in card.find("search-results__link--description") if n.attrs.get("href") == path]
        if not links or not _first(card, "search-results__status").lower().replace("ё", "е").startswith("прием заявок"):
            continue
        sums = card.find("search-results__sum")
        desktop = sums[0].find("desktop", "p") if sums else []
        price = _clean(desktop[0].content) if desktop else ""
        try:
            amount = Decimal(re.sub(r"\s", "", price.removesuffix("₽")).replace(",", "."))
            price = str(amount) if price.endswith("₽") and amount.is_finite() and amount > 0 else ""
        except InvalidOperation:
            price = ""
        result.append({
            "identity": f"roseltorg:{number}:{lot}", "procedure_number": number, "lot_number": lot,
            "title": _clean(links[0].content)[:1000], "customer": "",
            "source_url": source_url, "detail_url": ROSELTORG_ORIGIN + path, "official_url": "",
            # Catalog timestamps have NO zone; only a detail-page МСК timestamp is accepted.
            "deadline_at": "", "published_at": "", "initial_price_rub": price,
        })
    return result


def roseltorg_details(html: str, number: str) -> dict[str, str]:
    root = _page(html)
    fields = {_first(row, "lot-common-info__label"): _first(row, "lot-common-info__value")
              for row in root.find("lot-common-info__row")}
    if fields.get("Номер процедуры") != number:
        raise ValueError("detail_identity_mismatch")
    stages = [_first(n, "lot-steps__title").lower().replace("ё", "е") for n in root.find("lot-steps__heading")
              if "steps__item--current" in n.attrs.get("class", "").split()]
    if stages != ["прием заявок"]:
        raise ValueError("procedure_not_accepting_applications")
    result = {"customer": fields.get("Организатор торгов", "")[:800],
              "title": fields.get("Наименование процедуры", "")[:1000]}
    for key, label in (("deadline_at", "Дата и время окончания подачи заявок"), ("published_at", "Дата публикации")):
        match = re.fullmatch(r"(?:до )?(\d{2}\.\d{2}\.\d{2}(?:\d{2})? \d{2}:\d{2}) \(МСК\)", fields.get(label, ""))
        result[key] = ""
        if match:
            try:
                raw = match[1]
                value = datetime.strptime(raw, "%d.%m.%Y %H:%M" if len(raw) == 16 else "%d.%m.%y %H:%M")
                result[key] = value.replace(tzinfo=ZoneInfo("Europe/Moscow")).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    delivery = root.find("lot-delivery__text")
    result["delivery_address"] = _first(delivery[0], "lot-expand-text__text")[:1600] if delivery else ""
    return result


def _delivery(db: Session, report: BusinessRecord, notify: bool) -> tuple[int | None, str]:
    _verified_document_attachment(report.data)
    notification_id = report.data.get("notification_id")
    if notify and not notification_id:
        notification = queue_owner_notification(db, idempotency_key=report.external_id + ":telegram", channel="telegram",
            resource_type=REPORT_TYPE, resource_id=str(report.id), subject="Подборка тендеров на клининг",
            body=f"Найдено неистёкших объявлений: {len(report.data['record_ids'])}. В PDF: сроки, цены, ссылки и отметки непроверенных данных. Охват частичный.",
            data=report.data)
        notification_id = notification.id
        report.data = {**report.data, "notification_id": notification_id}
    notification = db.get(OwnerNotification, notification_id) if notification_id else None
    return notification_id, notification.status if notification else "not_requested"


def robots_policy(body: str, path: str) -> tuple[bool, float]:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for line in body.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif agents:
            rules.append((key, value))
    groups.append((agents, rules))
    explicit = [r for a, r in groups if any(x != "*" and x in USER_AGENT.lower() for x in a)]
    selected = explicit or [r for a, r in groups if "*" in a]
    matches: list[tuple[int, bool]] = []
    delay = 1.0
    for group in selected:
        for key, value in group:
            if key == "crawl-delay":
                try:
                    delay = max(delay, float(value))
                except ValueError:
                    pass
            if key not in {"allow", "disallow"} or not value:
                continue
            pattern = "^" + re.escape(value.rstrip("$")).replace(r"\*", ".*") + ("$" if value.endswith("$") else "")
            if re.search(pattern, path):
                matches.append((len(value.replace("*", "").rstrip("$")), key == "allow"))
    return (max(matches)[1] if matches else True), delay


class CatalogReader:
    """Public GETs only; no login, hidden endpoints, private session or AI fallback."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.robots: dict[str, str] = {}
        self.last_request = 0.0
        self.requests = 0

    def _get(self, url: str) -> str:
        self.requests += 1
        if self.requests > 21:
            raise ValueError("request_budget_exceeded")
        with self.client.stream("GET", url, headers={"User-Agent": USER_AGENT + "/2.1 public-procurement-monitor"}) as response:
            # No redirect traversal: an authentication/challenge route stays unavailable.
            if response.status_code != 200:
                raise ValueError(f"source_http_{response.status_code}")
            content_type = response.headers.get("content-type", "").lower()
            if not any(t in content_type for t in ("text/html", "text/plain")):
                raise ValueError("source_content_type")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError("source_size_limit")
        self.last_request = time.monotonic()
        body = raw.decode("utf-8", errors="replace")
        if re.search(r"<title[^>]*>[^<]*(?:captcha|access denied|доступ ограничен)", body, re.I):
            raise ValueError("source_access_challenge")
        return body

    def read(self, url: str) -> str:
        parts = urlsplit(url)
        catalog = url in (*CATALOGS, ROSELTORG_CATALOG)
        detail = not parts.query and (
            (parts.netloc == "www.b2b-center.ru" and re.fullmatch(r"/search/number/[a-zA-Z0-9-]+/", parts.path))
            or (parts.netloc == "www.roseltorg.ru" and re.fullmatch(r"/procedure/[A-Z0-9-]{1,64}/[1-9][0-9]{0,5}", parts.path)))
        if parts.scheme != "https" or parts.fragment or not (catalog or detail):
            raise ValueError("source_not_allowlisted")
        origin = f"https://{parts.netloc}"
        if origin not in self.robots:
            self.robots[origin] = self._get(origin + "/robots.txt")
        allowed, delay = robots_policy(self.robots[origin], parts.path + ("?" + parts.query if parts.query else ""))
        if not allowed or delay > 10 or not 0 <= delay <= 10:
            raise ValueError("source_robots_restricted")
        time.sleep(max(0.0, delay - (time.monotonic() - self.last_request)))
        return self._get(url)


def discover_tenders(keywords: list[str], now: datetime) -> dict[str, Any]:
    receipts: list[dict[str, Any]] = []
    found: dict[str, dict[str, Any]] = {}
    excluded: Counter[str] = Counter()
    with httpx.Client(timeout=12, trust_env=False, follow_redirects=False, transport=_DNSPinningTransport()) as client:
        reader = CatalogReader(client)
        for url in (*CATALOGS, ROSELTORG_CATALOG):
            try:
                body = reader.read(url)
                rows = parse_roseltorg_catalog(body, url) if url == ROSELTORG_CATALOG else parse_catalog(body, url)
                receipts.append({"url": url, "status": "fetched", "sha256": hashlib.sha256(body.encode()).hexdigest(), "items": len(rows)})
                for row in rows:
                    if not _matches(row["title"], keywords) or re.match(r"\s*поставка\b", row["title"], re.I):
                        excluded["keyword_or_service_mismatch"] += 1
                    elif not row["deadline_at"] and url != ROSELTORG_CATALOG:
                        excluded["deadline_unknown"] += 1
                    elif row["deadline_at"] and datetime.fromisoformat(row["deadline_at"]) <= now:
                        excluded["expired"] += 1
                    elif any(not place.startswith("ленинградск") for place in re.findall(
                        r"\b(?:по|в|на территории)\s+([а-я-]+)\s+област", row["title"].lower()
                    )):
                        excluded["explicit_outside_region_in_title"] += 1
                    else:
                        found.setdefault(row["identity"], row)
            except (httpx.HTTPError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                receipts.append({"url": url, "status": "unavailable", "reason": reason[:80]})
        selected: list[dict[str, Any]] = []
        for row in sorted(found.values(), key=lambda r: r["deadline_at"] or "9999")[:16]:
            roseltorg = row["source_url"] == ROSELTORG_CATALOG
            try:
                body = reader.read(row["detail_url"])
                if roseltorg:
                    row.update(roseltorg_details(body, row["procedure_number"]))
                    if not row["deadline_at"]:
                        excluded["deadline_unknown"] += 1
                        continue
                    if datetime.fromisoformat(row["deadline_at"]) <= now:
                        excluded["expired"] += 1
                        continue
                    if not _matches(row["title"], keywords) or re.match(r"\s*поставка\b", row["title"], re.I):
                        excluded["keyword_or_service_mismatch"] += 1
                        continue
                address = row["delivery_address"] if roseltorg else delivery_address(body)
                row["detail_sha256"] = hashlib.sha256(body.encode()).hexdigest()
                row["delivery_address"] = address
                if address and re.search(r"\b(?:санкт[ -]петербург(?:а|е)?|ленинградск(?:ая|ой|ую)\s+обл(?:асть|асти)?|спб)\b", address, re.I):
                    row["region_evidence"] = "delivery_address"
                elif address and (not roseltorg or re.search(r"(?<!\d )\bг\.\s*[А-ЯЁ][а-яё-]+|\b(?:город|область|обл\.)\b", address)):
                    excluded["delivery_outside_target_region"] += 1
                    continue
                else:
                    row["region_evidence"] = "needs_verification"
            except (httpx.HTTPError, ValueError) as exc:
                if roseltorg:
                    # An unverified catalog time is not a usable future deadline.
                    excluded["detail_not_verified"] += 1
                    continue
                row["delivery_address"] = ""
                row["region_evidence"] = "needs_verification"
                row["detail_error"] = str(exc)[:80] if isinstance(exc, ValueError) else type(exc).__name__
            row["catalog_sha256"] = next(x["sha256"] for x in receipts if x["url"] == row["source_url"] and x["status"] == "fetched")
            selected.append(row)
    return {"items": sorted(selected, key=lambda r: (r["deadline_at"], r["identity"])), "sources": receipts, "excluded": dict(excluded), "partial": True,
            "candidate_limit_reached": len(found) > 16, "observed_at": now.isoformat()}


def run_tender_search(db: Session, payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
    keywords = normalize_keywords(payload.get("keywords"))
    interval = max(60, min(settings.tender_search_interval_minutes, 1440))
    window = int(current.timestamp()) // (interval * 60)
    run_key = "public-tender-search:" + _digest([SOURCE_PROFILE, window, keywords])
    if db.get_bind().dialect.name == "postgresql":
        # A single transaction owns observation/report publication across worker replicas.
        db.execute(text("SELECT pg_advisory_xact_lock(724190831)"))
    existing = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "tender_search_run", BusinessRecord.external_id == run_key))
    if existing:
        result = existing.data["result"]
        existing_report = db.get(BusinessRecord, result["report_id"])
        if existing_report is None:
            raise RuntimeError("Persisted tender report is missing")
        notification_id, delivery_status = _delivery(db, existing_report, bool(payload.get("notify_owner", True)))
        return {**result, "reused": True, "notification_id": notification_id, "delivery_status": delivery_status}
    discovery = discover_tenders(keywords, current)
    successful = sum(x["status"] == "fetched" for x in discovery["sources"])
    if not successful:
        return {"status": "unavailable", "reason": "Public tender catalogs could not be read; no successful search is claimed.",
                "sources": discovery["sources"], "partial": True, "evidence": []}
    record_ids = []
    for item in discovery["items"]:
        row = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "tender", BusinessRecord.source == SOURCE, BusinessRecord.external_id == item["identity"]))
        facts_hash = _digest(item)
        if row is None:
            row = BusinessRecord(record_type="tender", source=SOURCE, external_id=item["identity"], owner="tender", status="needs_verification", data={})
            db.add(row)
        row.title = item["title"][:255]
        row.deadline_at = datetime.fromisoformat(item["deadline_at"]).replace(tzinfo=None)
        row.data = {**(row.data or {}), **item, "last_observed_at": current.isoformat(),
                    "qualification_status": "NEEDS_VERIFICATION", "source_kind": "public_etp_page" if item["source_url"] == ROSELTORG_CATALOG else "public_catalog_not_official_api",
                    "search_facts_hash": facts_hash, "submission_authorized": False}
        db.flush()
        record_ids.append(row.id)
    local = current.astimezone(ZoneInfo("Europe/Moscow"))
    # Observation timestamps / whole-page hashes are not material tender changes.
    material = [{k: v for k, v in item.items() if not k.endswith("sha256")} for item in discovery["items"]]
    report_key = "tender-digest:" + _digest([local.date().isoformat(), keywords, material, [{k: v for k, v in s.items() if k not in {"sha256", "items"}} for s in discovery["sources"]]])
    report = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == REPORT_TYPE, BusinessRecord.external_id == report_key))
    if report is None:
        entries: list[dict[str, object]] = []
        for item in discovery["items"]:
            deadline = datetime.fromisoformat(item["deadline_at"]).astimezone(ZoneInfo("Europe/Moscow"))
            label = item["title"] if item["region_evidence"] == "delivery_address" else "Место работ требует проверки: " + item["title"]
            entries.append({"label": label, "url": item["detail_url"], "details": [
                f"Номер: {item['identity']} · Заказчик: {item['customer'] or 'не указан'}",
                f"Приём заявок до: {deadline:%d.%m.%Y %H:%M} МСК (по публичной карточке)",
                f"Начальная цена: {item['initial_price_rub'] + ' RUB' if item['initial_price_rub'] else 'не указана'}",
                f"Адрес поставки: {item.get('delivery_address') or 'НЕ ПОДТВЕРЖДЁН; не считать закупкой СПб/ЛО'}",
                f"Первоисточник ЕИС: {item['official_url'] or 'ссылка на извещение не подтверждена; см. карточку площадки'}",
                "Условия, документы и возможность участия требуют отдельной проверки. Заявка не подавалась.",
            ]})
        entries.append({"label": "Проверенные источники и полнота поиска", "details": [
            "Частичный поиск: два каталога B2B-Center и первая страница Росэлторга по слову «уборк» и регионам заказчика 47/78; не весь интернет и не официальный API ЕИС.",
            *[f"{s['url']}: {s['status']}" for s in discovery["sources"]],
            "Отклонено: " + json.dumps(discovery["excluded"], ensure_ascii=False),
            "Адрес заказчика не подменяет адрес выполнения работ. Неподтверждённая география явно отмечена.",
        ]})
        path = Path(settings.document_storage_path) / "reports" / "tenders" / f"tenders-{local.date()}-{_digest(report_key)[:16]}.pdf"
        checksum = _build_pdf(path, title="Подборка тендеров на клининг", generated_at=local, report_day=local.date(),
                              summary_rows=[("Ключевые слова", ", ".join(keywords)), ("Неистёкших объявлений", len(discovery["items"])),
                                            ("Подтверждён адрес СПб/ЛО", sum(i["region_evidence"] == "delivery_address" for i in discovery["items"])),
                                            ("Участие / подача заявки", "Не выполнялись")], entries=entries)
        report = BusinessRecord(record_type=REPORT_TYPE, external_id=report_key, title=f"Тендеры · {local:%d.%m.%Y %H:%M}", status="ready", source=SOURCE,
                                data={"document_path": str(path), "document_filename": path.name, "document_sha256": checksum,
                                      "document_content_type": "application/pdf", "record_ids": record_ids, "keywords": keywords,
                                      "partial": True, "observed_at": current.isoformat()})
        db.add(report)
        db.flush()
    notification_id, delivery_status = _delivery(db, report, bool(payload.get("notify_owner", True)))
    result = {"status": "completed", "report_id": report.id, "record_ids": record_ids, "count": len(record_ids),
              "notification_id": notification_id, "delivery_status": delivery_status,
              "download_url": f"/api/tender-search/reports/{report.id}/download", "partial": True,
              "keywords": keywords, "sources": discovery["sources"], "excluded": discovery["excluded"],
              "evidence": [{"type": "document_export", "report_id": report.id, "sha256": report.data["document_sha256"]}]}
    run = BusinessRecord(record_type="tender_search_run", external_id=run_key, title="Public tender keyword search", source=SOURCE, status="completed",
                         data={"result": result, "discovery": discovery})
    db.add(run)
    db.flush()
    db.add(AuditLog(actor="tender", action="tender_search.completed", resource_type=REPORT_TYPE, resource_id=str(report.id), details={"run_id": run.id, "tender_count": len(record_ids), "partial": True}))
    event_bus.publish(db, "tender_search.completed", REPORT_TYPE, str(report.id), {"run_id": run.id, "count": len(record_ids)}, idempotency_key=run_key)
    return result
