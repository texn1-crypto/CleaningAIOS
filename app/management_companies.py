from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BusinessRecord, ImportJob, OutreachConsent, Suppression
from .operations import parse_lead_import


USER_AGENT = "CleaningAIOS-Research/1.0 (+https://cleaningaios.51-250-33-133.sslip.io/)"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"\+?[0-9][0-9()\-\s]{7,20}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_region(value: object) -> str | None:
    text = " ".join(str(value or "").lower().replace("ё", "е").split())
    if "санкт" in text or text in {"спб", "78"}:
        return "Санкт-Петербург"
    if "ленинград" in text or text in {"ло", "47"}:
        return "Ленинградская область"
    return None


def normalize_email(value: object) -> str:
    email = str(value or "").strip().lower().removeprefix("mailto:").split("?", 1)[0]
    return email if EMAIL_RE.fullmatch(email) else ""


def normalize_phone(value: object) -> str:
    raw = str(value or "").strip().removeprefix("tel:")
    match = PHONE_RE.search(raw)
    if not match:
        return ""
    digits = re.sub(r"\D", "", match.group())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits if 10 <= len(digits) <= 15 else ""


def normalize_emails(*values: object) -> list[str]:
    emails: set[str] = set()
    for value in values:
        for candidate in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(value or "")):
            normalized = normalize_email(candidate)
            if normalized:
                emails.add(normalized)
    return sorted(emails)


def normalize_phones(*values: object) -> list[str]:
    phones: set[str] = set()
    for value in values:
        for candidate in re.split(r"[|;,\n]+", str(value or "")):
            normalized = normalize_phone(candidate)
            if normalized:
                phones.add(normalized)
    return sorted(phones)


def _split_text_values(value: object) -> list[str]:
    return sorted({item.strip() for item in re.split(r"\s*\|\s*|[\r\n]+", str(value or "")) if item.strip()})


def _optional_number(value: object, *, integer: bool = False) -> int | float | None:
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if integer else number


def _external_id(source: dict, title: str, region: str) -> str:
    inn = re.sub(r"\D", "", str(source.get("inn") or source.get("ИНН") or ""))
    ogrn = re.sub(r"\D", "", str(source.get("ogrn") or source.get("ОГРН") or ""))
    if inn:
        return f"inn:{inn}"
    if ogrn:
        return f"ogrn:{ogrn}"
    explicit = str(source.get("external_id") or source.get("source_id") or "").strip()
    if explicit:
        return f"source:{hashlib.sha256(explicit.encode()).hexdigest()[:32]}"
    # A shared dispatcher email can legitimately belong to several УК/ТСЖ, so it
    # must never be used as the organization identity.
    digest = hashlib.sha256(f"{title.lower()}|{region}".encode()).hexdigest()[:32]
    return f"name:{digest}"


def import_management_companies(
    db: Session,
    *,
    filename: str,
    content: bytes,
    source_kind: str,
    source_url: str,
    actor: str,
) -> dict:
    job = ImportJob(import_type="management_companies", filename=filename, created_by=actor)
    db.add(job)
    db.flush()
    rows = parse_lead_import(filename, content)
    errors: list[dict] = []
    updated = 0
    for index, source in enumerate(rows, 2):
        title = str(
            source.get("company")
            or source.get("name")
            or source.get("title")
            or source.get("organization")
            or source.get("Наименование")
            or ""
        ).strip()
        region = normalize_region(source.get("region") or source.get("Регион") or source.get("subject"))
        emails = normalize_emails(
            source.get("email"), source.get("emails"), source.get("Электронная почта"), source.get("e-mail")
        )
        phones = normalize_phones(source.get("phone"), source.get("phones"), source.get("Телефон"))
        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""
        website = str(source.get("website") or source.get("site") or source.get("Сайт") or "").strip()
        if not title:
            errors.append({"row": index, "error": "missing company/name/title"})
            continue
        if not region:
            errors.append({"row": index, "error": "region must be Saint Petersburg or Leningrad Oblast"})
            continue
        external_id = _external_id(source, title, region)
        record = db.scalar(
            select(BusinessRecord).where(
                BusinessRecord.record_type == "management_company",
                BusinessRecord.external_id == external_id,
            )
        )
        provenance = {
            "source_kind": source_kind,
            "source_url": source_url,
            "collected_at": utcnow().isoformat(),
            "row": index,
        }
        public_data = {
            "region": region,
            "email": email,
            "emails": emails,
            "phone": phone,
            "phones": phones,
            "website": website,
            "candidate_website": str(source.get("candidate_website") or "").strip(),
            "organization_type": str(source.get("organization_type") or source.get("type") or "").strip(),
            "scope_status": str(source.get("scope_status") or "in_scope").strip(),
            "address": str(source.get("address") or source.get("Адрес") or "").strip(),
            "vk_url": str(source.get("vk_url") or "").strip(),
            "contact_person": str(source.get("contact_person") or "").strip(),
            "managed_objects": _optional_number(source.get("managed_objects"), integer=True),
            "managed_area_m2": _optional_number(source.get("managed_area_m2")),
            "internet_verification_status": str(source.get("internet_verification_status") or "not_checked").strip(),
            "source_refs": _split_text_values(source.get("source_refs")),
            "inn": re.sub(r"\D", "", str(source.get("inn") or source.get("ИНН") or "")),
            "ogrn": re.sub(r"\D", "", str(source.get("ogrn") or source.get("ОГРН") or "")),
            "marketing_consent_status": "unknown",
        }
        if record:
            previous = dict(record.data or {})
            existing_sources = list((record.data or {}).get("provenance") or [])
            if not any(item.get("source_url") == source_url and item.get("row") == index for item in existing_sources):
                existing_sources.append(provenance)
            record.data = {
                **previous,
                **{key: value for key, value in public_data.items() if value not in (None, "", [])},
                "marketing_consent_status": previous.get("marketing_consent_status") or "unknown",
                "provenance": existing_sources,
            }
            record.title = title
            record.source = source_kind
            updated += 1
        else:
            db.add(
                BusinessRecord(
                    record_type="management_company",
                    external_id=external_id,
                    title=title,
                    status="collected",
                    source=source_kind,
                    data={**public_data, "provenance": [provenance]},
                )
            )
            job.imported_rows += 1
    job.total_rows = len(rows)
    job.skipped_rows = len(errors)
    job.errors = errors
    job.status = "completed_with_errors" if errors else "completed"
    job.completed_at = utcnow()
    db.flush()
    return {
        "job_id": job.id,
        "status": job.status,
        "total_rows": job.total_rows,
        "created": job.imported_rows,
        "updated": updated,
        "skipped": job.skipped_rows,
        "errors": errors,
    }


def _record_emails(record: BusinessRecord) -> list[str]:
    data = record.data or {}
    return normalize_emails(data.get("email"), data.get("emails"))


def audit_management_company_contacts(
    db: Session,
    *,
    batch_limit: int = 20,
    enrich_verified_websites: bool = False,
) -> dict:
    """Measure real contact coverage and optionally inspect already verified sites.

    Candidate domains are deliberately excluded: only ``website`` is considered
    verified enough for the SSRF/robots-aware crawler.
    """
    records = list(
        db.scalars(
            select(BusinessRecord)
            .where(BusinessRecord.record_type == "management_company")
            .order_by(BusinessRecord.id)
        ).all()
    )
    verified_consents = set(
        db.scalars(
            select(OutreachConsent.address).where(
                OutreachConsent.status == "verified",
                OutreachConsent.purpose == "commercial_outreach",
            )
        ).all()
    )
    suppressed = set(db.scalars(select(Suppression.address)).all())
    verified_websites = [record for record in records if str((record.data or {}).get("website") or "").strip()]
    candidate_websites = [record for record in records if str((record.data or {}).get("candidate_website") or "").strip()]
    verified_website_ids = {record.id for record in verified_websites}
    missing = [record for record in records if not _record_emails(record)]
    enrichments: list[dict] = []
    if enrich_verified_websites:
        for record in [item for item in missing if item.id in verified_website_ids][: max(0, min(batch_limit, 100))]:
            try:
                enrichments.append(enrich_management_company(db, record.id))
            except (httpx.HTTPError, OSError, ValueError) as exc:
                enrichments.append({"record_id": record.id, "status": "failed", "reason": str(exc)[:300]})

    # Recompute coverage after verified-site enrichment so the task result is a
    # truthful post-run snapshot rather than a stale pre-run count.
    email_addresses = {email for record in records for email in _record_emails(record)}
    missing = [record for record in records if not _record_emails(record)]
    eligible = sorted((email_addresses & verified_consents) - suppressed)
    return {
        "status": "partial" if missing else "complete",
        "collection": "management_company_contacts",
        "organizations": len(records),
        "organizations_with_email": len(records) - len(missing),
        "organizations_missing_email": len(missing),
        "unique_email_addresses": len(email_addresses),
        "verified_websites": len(verified_websites),
        "candidate_websites_unverified": len(candidate_websites),
        "verified_marketing_consents": len(email_addresses & verified_consents),
        "suppressed_addresses": len(email_addresses & suppressed),
        "send_eligible_addresses": len(eligible),
        "website_enrichments": enrichments,
        "search_adapter_required": bool(missing),
        "reason": (
            "Для организаций без email и подтверждённого сайта нужен поиск по официальному реестру/поисковому API; "
            "кандидат домена не считается подтверждённым сайтом."
            if missing else "Все организации имеют email из источника с provenance."
        ),
        "evidence": [
            {
                "type": "management_company_contact_coverage",
                "organization_count": len(records),
                "missing_email_count": len(missing),
                "missing_record_ids_sample": [record.id for record in missing[:50]],
            },
            {
                "type": "outreach_safety_gate",
                "eligible_address_count": len(eligible),
                "consent_required": True,
                "owner_approval_required": True,
            },
        ],
    }


def management_company_outreach_summary(db: Session) -> dict:
    records = list(db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "management_company")).all())
    verified_consents = set(
        db.scalars(
            select(OutreachConsent.address).where(
                OutreachConsent.status == "verified",
                OutreachConsent.purpose == "commercial_outreach",
            )
        ).all()
    )
    suppressed = set(db.scalars(select(Suppression.address)).all())
    segments: dict[str, int] = {}
    regions: dict[str, int] = {}
    addresses: set[str] = set()
    for record in records:
        data = record.data or {}
        org_type = str(data.get("organization_type") or "Не указан")
        region = str(data.get("region") or "Не указан")
        segments[org_type] = segments.get(org_type, 0) + 1
        regions[region] = regions.get(region, 0) + 1
        addresses.update(_record_emails(record))
    eligible = (addresses & verified_consents) - suppressed
    return {
        "status": "prepared",
        "action": "prepare_management_company_outreach",
        "organizations": len(records),
        "segments_by_type": dict(sorted(segments.items())),
        "segments_by_region": dict(sorted(regions.items())),
        "unique_email_addresses": len(addresses),
        "verified_marketing_consents": len(addresses & verified_consents),
        "suppressed_addresses": len(addresses & suppressed),
        "send_eligible_addresses": len(eligible),
        "messages_queued": 0,
        "owner_approval_required_before_campaign": True,
        "evidence": [
            {
                "type": "management_company_outreach_segment",
                "organization_count": len(records),
                "eligible_address_count": len(eligible),
                "messages_queued": 0,
            }
        ],
    }


def _validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) website URLs are allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError("Website hostname cannot be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, local or reserved website addresses are forbidden")
    return url


class ContactHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.emails: set[str] = set()
        self.phones: set[str] = set()
        self.contact_links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        email = normalize_email(href) if href.lower().startswith("mailto:") else ""
        phone = normalize_phone(href) if href.lower().startswith("tel:") else ""
        if email:
            self.emails.add(email)
        if phone:
            self.phones.add(phone)
        absolute = urljoin(self.base_url, href)
        parsed = urlparse(absolute)
        if parsed.hostname == urlparse(self.base_url).hostname and any(
            token in parsed.path.lower() for token in ("contact", "kontakty", "kontakti", "kontact")
        ):
            self.contact_links.add(absolute)

    def handle_data(self, data: str) -> None:
        for candidate in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", data):
            email = normalize_email(candidate)
            if email:
                self.emails.add(email)
        for candidate in PHONE_RE.findall(data):
            phone = normalize_phone(candidate)
            if phone:
                self.phones.add(phone)


def _fetch_text(
    client: httpx.Client,
    url: str,
    *,
    allowed_content_types: tuple[str, ...],
) -> tuple[str, str]:
    current = _validate_public_url(url)
    for _ in range(4):
        response = client.get(current, headers={"User-Agent": USER_AGENT})
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise ValueError("Website redirect has no target")
            current = _validate_public_url(urljoin(current, location))
            continue
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not any(value in content_type for value in allowed_content_types):
            raise ValueError("Website response has an unsupported content type")
        raw = response.content[:1_000_001]
        if len(raw) > 1_000_000:
            raise ValueError("Website page exceeds 1 MB limit")
        return current, response.text
    raise ValueError("Too many website redirects")


def _fetch_page(client: httpx.Client, url: str) -> tuple[str, str]:
    return _fetch_text(client, url, allowed_content_types=("text/html", "application/xhtml+xml"))


def enrich_management_company(db: Session, record_id: int) -> dict:
    record = db.get(BusinessRecord, record_id)
    if not record or record.record_type != "management_company":
        raise LookupError("Management company not found")
    website = str((record.data or {}).get("website") or "").strip()
    if not website:
        return {"status": "configuration_required", "reason": "Company website is missing", "record_id": record.id}
    _validate_public_url(website)
    parsed = urlparse(website)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        robots = RobotFileParser()
        robots.set_url(robots_url)
        try:
            _, robots_text = _fetch_text(client, robots_url, allowed_content_types=("text/plain",))
            robots.parse(robots_text.splitlines())
        except (httpx.HTTPError, ValueError):
            robots.parse([])
        if not robots.can_fetch(USER_AGENT, website):
            return {"status": "blocked", "reason": "robots.txt forbids collection", "record_id": record.id}
        resolved, html = _fetch_page(client, website)
        parser = ContactHTMLParser(resolved)
        parser.feed(html)
        pages = [resolved]
        for link in sorted(parser.contact_links)[:3]:
            if not robots.can_fetch(USER_AGENT, link):
                continue
            try:
                page_url, page_html = _fetch_page(client, link)
            except (httpx.HTTPError, ValueError):
                continue
            child = ContactHTMLParser(page_url)
            child.feed(page_html)
            parser.emails.update(child.emails)
            parser.phones.update(child.phones)
            pages.append(page_url)
    data = dict(record.data or {})
    emails = sorted(set(data.get("emails") or []) | parser.emails | ({data["email"]} if data.get("email") else set()))
    phones = sorted(set(data.get("phones") or []) | parser.phones | ({data["phone"]} if data.get("phone") else set()))
    provenance = list(data.get("provenance") or [])
    provenance.append({"source_kind": "company_website", "source_url": resolved, "collected_at": utcnow().isoformat(), "pages": pages})
    record.data = {
        **data,
        "emails": emails,
        "phones": phones,
        "email": emails[0] if emails else "",
        "phone": phones[0] if phones else "",
        "marketing_consent_status": data.get("marketing_consent_status") or "unknown",
        "provenance": provenance,
    }
    db.flush()
    return {
        "status": "enriched",
        "record_id": record.id,
        "emails_found": len(emails),
        "phones_found": len(phones),
        "pages_checked": pages,
        "marketing_consent_status": record.data["marketing_consent_status"],
    }
