from __future__ import annotations

import hashlib
from typing import Any

import extruct
import trafilatura
from bs4 import BeautifulSoup

from .evidence import evidence, utc_now
from .website import (
    _extraction_state,
    _jsonld_organisations,
    _registered_domain,
    _social_links,
    normalize_homepage,
    structured_social_links,
)


def _decode_html(body: bytes, content_type: str) -> str:
    """Decode HTML using the HTTP charset when available."""
    import re

    match = re.search(
        r"charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)",
        content_type,
        flags=re.IGNORECASE,
    )

    if match:
        encoding = match.group(1)
        try:
            return body.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass

    # BeautifulSoup can detect common HTML encodings from the document.
    return body.decode("utf-8", errors="replace")


def _deduplicate_social_links(
    links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one link per platform and URL, preserving first-seen order."""
    result = []
    seen = set()

    for item in links:
        if not isinstance(item, dict):
            continue

        platform = item.get("platform")
        url = item.get("url")

        if not platform or not url:
            continue

        key = (str(platform), str(url))
        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result


def extract_page_event(
    *,
    organisation_number: str,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
    body: bytes,
    page_kind: str,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Extract a bounded evidence record from one downloaded page."""
    base = {
        "organisation_number": str(organisation_number),
        "requested_url": requested_url,
        "final_url": final_url,
        "status_code": status_code,
        "content_type": content_type,
        "page_kind": page_kind,
        "retrieved_at": retrieved_at or utc_now(),
        "bytes": len(body),
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }

    if status_code < 200 or status_code >= 300:
        return {
            **base,
            "status": (
                "not_found"
                if status_code in {404, 410}
                else "source_error"
            ),
            "error": f"HTTP {status_code}",
        }

    if "html" not in content_type.casefold():
        return {
            **base,
            "status": "source_error",
            "error": f"Unsupported content type: {content_type}"[:240],
        }

    page_html = _decode_html(body, content_type)
    soup = BeautifulSoup(page_html, "lxml")

    # Text extraction is best-effort. Failure should not erase the
    # downloaded page or its hash and HTTP metadata.
    extraction_error = None
    try:
        text = (
            trafilatura.extract(
                page_html,
                url=final_url,
                include_links=False,
                include_tables=False,
                favor_precision=True,
            )
            or ""
        )
    except Exception as exc:
        text = ""
        extraction_error = type(exc).__name__

    title = (
        soup.title.get_text(" ", strip=True)[:500]
        if soup.title
        else ""
    )

    description_tag = soup.select_one(
        'meta[name="description"], meta[property="og:description"]'
    )
    description = (
        str(description_tag.get("content") or "").strip()[:2000]
        if description_tag
        else ""
    )

    identity_nodes = soup.select(
        "footer, address, [itemprop='legalName'], "
        "[itemprop='address'], [itemprop='telephone'], "
        "[itemprop='email']"
    )
    identity_text = " ".join(
        node.get_text(" ", strip=True)
        for node in identity_nodes
    )
    identity_text = " ".join(identity_text.split())[:3000]

    try:
        social_links = _social_links(final_url, soup)
    except Exception:
        social_links = []

    try:
        extraction_state = _extraction_state(text, soup)
    except Exception:
        extraction_state = "partial" if text or title else "empty"

    event: dict[str, Any] = {
        **base,
        "status": "available",
        "title": title,
        "description": description,
        "main_text_excerpt": text[:5000],
        "identity_text_excerpt": identity_text,
        "social_links": _deduplicate_social_links(social_links),
        "extraction_state": extraction_state,
    }

    if extraction_error:
        event["extraction_error"] = extraction_error

    if page_kind == "homepage":
        try:
            structured = extruct.extract(
                page_html,
                base_url=final_url,
                syntaxes=["json-ld", "microdata", "opengraph"],
            )
            organisations = _jsonld_organisations(structured)
        except Exception as exc:
            organisations = []
            event["structured_extraction_error"] = type(exc).__name__

        event["structured_organisations"] = organisations

        try:
            structured_links = structured_social_links(organisations)
        except Exception:
            structured_links = []

        event["social_links"] = _deduplicate_social_links(
            event["social_links"] + structured_links
        )

        event["registered_domain"] = _registered_domain(final_url)

    return event


def error_page_event(
    *,
    organisation_number: str,
    requested_url: str,
    page_kind: str,
    error: str,
) -> dict[str, Any]:
    """Create a terminal ledger event when a page cannot be downloaded."""
    return {
        "organisation_number": str(organisation_number),
        "requested_url": requested_url,
        "final_url": requested_url,
        "status_code": 0,
        "content_type": "",
        "page_kind": page_kind,
        "retrieved_at": utc_now(),
        "bytes": 0,
        "content_sha256": hashlib.sha256(b"").hexdigest(),
        "status": "source_error",
        "error": str(error)[:240],
    }


def missing_seed_error_events(
    profiles: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Create fallback events for crawlable seeds without a homepage event.

    A missing event is a crawl-pipeline gap, not evidence that the
    company's website does not exist.
    """
    homepage_orgs = {
        str(event.get("organisation_number") or "")
        for event in events
        if event.get("page_kind") == "homepage"
    }

    missing = []

    for profile in profiles:
        organisation_number = str(
            profile.get("organisation_number") or ""
        )
        website = str(profile.get("website") or "").strip()

        if not website or organisation_number in homepage_orgs:
            continue

        requested_url = normalize_homepage(website)
        if not requested_url:
            continue

        missing.append(
            error_page_event(
                organisation_number=organisation_number,
                requested_url=requested_url,
                page_kind="homepage",
                error=(
                    "No homepage event was emitted. The request may have "
                    "been rejected before download or the crawl may have "
                    "ended before an event was recorded."
                ),
            )
        )

    return missing


def merge_profile_events(
    profile: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge one company's page events into the existing website evidence."""
    organisation_number = str(
        profile.get("organisation_number") or ""
    )

    company_events = [
        item
        for item in events
        if str(item.get("organisation_number") or "")
        == organisation_number
    ]

    homepage_events = [
        item
        for item in company_events
        if item.get("page_kind") == "homepage"
    ]

    # Prefer a successful homepage event when retries or HTTP fallback
    # produced more than one event. Within each group, use retrieval time.
    homepage_events.sort(
        key=lambda item: (
            item.get("status") == "available",
            item.get("retrieved_at") or "",
        )
    )
    homepage = homepage_events[-1] if homepage_events else None

    if not homepage:
        return evidence(
            "website",
            "source_error",
            "registry_linked_company_website_scrapy",
            str(
                profile.get("website")
                or "https://data.brreg.no/enhetsregisteret/api/enheter"
            ),
            note="No homepage crawl event was recorded; website status is unknown",
        )

    if homepage.get("status") != "available":
        return evidence(
            "website",
            homepage.get("status", "source_error"),
            "registry_linked_company_website_scrapy",
            homepage.get("final_url") or homepage.get("requested_url"),
            note=homepage.get("error") or "Homepage fetch failed",
            retrieved_at=homepage.get("retrieved_at"),
            content_sha256=homepage.get("content_sha256"),
        )

    available = [
        item
        for item in company_events
        if item.get("status") == "available"
    ]

    unique_pages: dict[str, dict[str, Any]] = {}
    social: dict[tuple[str, str], dict[str, Any]] = {}

    # Homepage first; secondary pages in stable URL order.
    available.sort(
        key=lambda item: (
            item.get("page_kind") != "homepage",
            item.get("final_url") or "",
        )
    )

    for item in available:
        page_url = item.get("final_url") or item.get("requested_url")

        if page_url:
            unique_pages[page_url] = {
                "url": page_url,
                "title": item.get("title") or "",
                "main_text_excerpt": item.get("main_text_excerpt") or "",
                "identity_text_excerpt": item.get("identity_text_excerpt") or "",
                "content_sha256": item.get("content_sha256"),
            }

        for link in item.get("social_links") or []:
            if not isinstance(link, dict):
                continue

            platform = link.get("platform")
            url = link.get("url")

            if platform and url:
                social[(str(platform), str(url))] = link

    value = {
        "requested_url": homepage.get("requested_url"),
        "final_url": homepage.get("final_url"),
        "registered_domain": homepage.get("registered_domain"),
        "title": homepage.get("title") or "",
        "description": homepage.get("description") or "",
        "main_text_excerpt": homepage.get("main_text_excerpt") or "",
        "identity_text_excerpt": homepage.get("identity_text_excerpt") or "",
        "social_links": list(social.values()),
        "structured_organisations": homepage.get(
            "structured_organisations"
        ) or [],
        "content_sha256": homepage.get("content_sha256"),
        "extraction_state": homepage.get("extraction_state"),
        "pages": list(unique_pages.values()),
        "crawl_errors": [
            {
                "url": item.get("final_url") or item.get("requested_url"),
                "error": item.get("error"),
            }
            for item in company_events
            if item.get("status") != "available"
        ],
        "scheduler": "scrapy_resumable_v1",
    }

    return evidence(
        "website",
        "available",
        "registry_linked_company_website_scrapy",
        homepage.get("final_url"),
        value=value,
        note="Company-controlled claim layer; not an official registry fact",
        retrieved_at=homepage.get("retrieved_at"),
        content_sha256=homepage.get("content_sha256"),
    )