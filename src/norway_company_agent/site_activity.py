"""Hiring and public-activity evidence from an already-verified company website (+ optional NAV feed).

Rules that protect precision:
* Website-derived facts are only used when the exact-entity identity gate marked the site publishable.
* A public-activity item needs a title AND a parseable date. Social links alone are not "activity".
* A hiring posting is either machine-readable (schema.org JobPosting) or a listing link under the
  company's own careers section; the method and a confidence are stored with every record.
* "Checked and found nothing" is kept apart from "not checked" through the `note` and `value.checked`.

No extra network calls happen here; the crawler (website.py) already captured the signals.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from . import nav_jobs
from .evidence import utc_now
from .identity import _tokens

NEWS_PATH = re.compile(
    r"/(?:news|press|aktuelt|nyheter|artikler|blog|blogg|nyheit|pressemelding|pressemeldinger)(?:/|$)",
    re.I,
)


def _verified_site(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not identity.get("publishable"):
        return None
    return website, value


def _all_signals(value: dict[str, Any]) -> list[dict[str, Any]]:
    items = [value.get("signals") or {}]
    items += [(page.get("signals") or {}) for page in value.get("pages") or []]
    return [item for item in items if item and not item.get("error")]


def _record(field: str, status: str, source_type: str, *, note: str, value: Any = None, source_url: str | None = None,
            retrieved_at: str | None = None, content_sha256: str | None = None, source_class: str | None = None,
            as_of: str | None = None, confidence: float | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "field": field,
        "status": status,
        "source_type": source_type,
        "note": note,
        "value": value,
    }
    retrieved_at = retrieved_at or utc_now()
    for key, item in (("source_class", source_class), ("source_url", source_url), ("retrieved_at", retrieved_at),
                      ("content_sha256", content_sha256), ("as_of", as_of), ("confidence", confidence)):
        if item is not None:
            record[key] = item
    return record


# ---------------------------------------------------------------------------
# Public activity
# ---------------------------------------------------------------------------
def extract_public_activity(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a public_activity evidence record (always present, never invented)."""
    verified = _verified_site(profile)
    if not verified:
        return _record("public_activity", "not_available", "company_owned_site",
                       note="No verified company website with publishable identity, so no activity is attributed to this entity")
    website, value = verified
    source_url = value.get("final_url") or website.get("source_url")

    dated: dict[tuple[str, str], dict[str, Any]] = {}
    for signals in _all_signals(value):
        for item in signals.get("dated_items") or []:
            dated[(item["title"], item["date"])] = {**item, "page": signals.get("page_url")}
    for item in value.get("feed_items") or []:
        dated[(item["title"], item["date"])] = {**item, "page": value.get("feed_url")}
    items = sorted(dated.values(), key=lambda i: (i["date"], i["title"]), reverse=True)[:10]

    pages = value.get("pages") or []
    news_pages = [
        {"url": str(p.get("url")), "title": (p.get("title") or "")[:300], "content_sha256": p.get("content_sha256")}
        for p in pages
        if NEWS_PATH.search(urlparse(str(p.get("url") or "")).path)
    ][:5]
    socials = value.get("social_links") or []

    base = dict(source_url=source_url, retrieved_at=website.get("retrieved_at"),
                content_sha256=value.get("content_sha256") or website.get("content_sha256"), source_class="company_owned")
    if items:
        latest = items[0]["date"]
        # The evidence URL and hash must describe the same page: use the page the newest item came from
        page_hashes = {str(pg.get("url")): pg.get("content_sha256") for pg in pages if pg.get("content_sha256")}
        latest_page = str(items[0].get("page") or "")
        if latest_page in page_hashes:
            base["source_url"], base["content_sha256"] = latest_page, page_hashes[latest_page]
        recent_cutoff = (date.today() - timedelta(days=365)).isoformat()
        methods = sorted({i.get("method") for i in items if i.get("method")})
        return _record(
            "public_activity", "available", "company_owned_site",
            note=f"{len(items)} dated item(s) on the verified company site; latest {latest}",
            value={
                "items": items,
                "latest_date": latest,
                "items_last_12_months": sum(1 for i in items if i["date"] >= recent_cutoff),
                "news_pages": news_pages,
                "social_links": socials[:8],
                "methods": methods,
                "interpretation": "Dated items published by the company on its own verified site; not independent sentiment.",
            },
            as_of=latest, confidence=0.9 if any(m in {"json-ld article", "rss/atom feed"} for m in methods) else 0.8, **base,
        )
    if news_pages:
        return _record(
            "public_activity", "available", "company_owned_site",
            note=f"Verified site has {len(news_pages)} news/press page(s), but no item carried a readable date",
            value={"items": [], "latest_date": None, "news_pages": news_pages, "social_links": socials[:8],
                   "interpretation": "News section exists on the company's own site; publication dates could not be read."},
            confidence=0.5, **base,
        )
    note = "Verified site contains no news/press section or dated posts in the bounded crawl"
    if socials:
        note += f"; {len(socials)} social profile link(s) exist but their posts are not readable without restricted-platform access"
    return _record("public_activity", "not_available", "company_owned_site", note=note, **base)


# ---------------------------------------------------------------------------
# Hiring
# ---------------------------------------------------------------------------
def _dedupe_postings(postings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out = []
    for posting in postings:
        key = (str(posting.get("title") or "").casefold(), str(posting.get("url") or ""))
        if key not in seen:
            seen.add(key)
            out.append(posting)
    return out


def _posting_belongs_to(posting: dict[str, Any], legal_name: Any) -> bool:
    """False when a job posting names a hiring organisation that shares nothing with the company's name.

    NAV ads are confirmed by organisation number and are always accepted. A page can also advertise jobs
    for a client or a sister company; publishing those under this company would be a wrong-company fact.
    """
    if str(posting.get("method") or "").startswith("NAV"):
        return True
    employer = _tokens(posting.get("hiring_organisation"))
    core = _tokens(legal_name)
    if not employer or not core:
        return True  # nothing to compare: the posting sits on the verified company site
    return bool(set(employer) & set(core))

def extract_hiring(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a hiring evidence record: real postings when observable, otherwise an honest reason."""
    checked: list[str] = []
    postings: list[dict[str, Any]] = []
    nav_error = None
    nav_url = None

    # Official, identity-exact job feed (only when the user enabled it)
    
    if nav_jobs.enabled():
        nav = nav_jobs.lookup(
            profile["organisation_number"],
            profile.get("name", ""),
        )
        nav_error = nav.get("error")
        nav_url = nav.get("source_url")

        # Only use NAV results when the feed returned successfully.
        # A timeout is not evidence that the company has no openings.
        if not nav_error:
            checked.append("NAV Arbeidsplassen feed")
            postings.extend(nav.get("ads") or [])

    verified = _verified_site(profile)
    careers_url = no_openings = None
    ats: list[str] = []
    site_postings: list[dict[str, Any]] = []
    sha = None
    page_sha: dict[str, str] = {}
    if verified:
        website, value = verified
        checked.append("company website")
        sha = value.get("content_sha256") or website.get("content_sha256")
        page_sha = {str(pg.get("url")): pg.get("content_sha256") for pg in value.get("pages") or [] if pg.get("content_sha256")}
        page_sha[str(value.get("final_url"))] = value.get("content_sha256")
        for signals in _all_signals(value):
            for posting in signals.get("job_postings") or []:
                site_postings.append({**posting, "source_page": signals.get("page_url")})
            if signals.get("careers_page") and not careers_url:
                careers_url = signals.get("page_url")
            no_openings = no_openings or signals.get("no_openings_statement")
            ats += [a for a in signals.get("ats_links") or [] if a not in ats]
        postings += site_postings
    postings = _dedupe_postings(postings)

    # Precision first. A posting only counts when it is still open and is not advertised for another employer.
    today = date.today().isoformat()
    open_now = [p for p in postings if not p.get("valid_through") or p["valid_through"] >= today]
    expired_n = len(postings) - len(open_now)
    own = [p for p in open_now if _posting_belongs_to(p, profile.get("name"))]
    other_org_n = len(open_now) - len(own)
    postings = own
    withheld: dict[str, int] = {}
    if expired_n:
        withheld["expired"] = expired_n
    if other_org_n:
        withheld["other_organisation"] = other_org_n
    withheld_note = ("; withheld: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in withheld.items())) if withheld else ""

    if postings:
        dated = [p["date_posted"] for p in postings if p.get("date_posted")]
        structured = any(p.get("method") in {"json-ld JobPosting"} or str(p.get("method", "")).startswith("NAV") for p in postings)
        from_nav = any(str(p.get("method", "")).startswith("NAV") for p in postings)
        first = next((p for p in postings if not str(p.get("method", "")).startswith("NAV")), None)
        first_site = (first or {}).get("source_page") or (first or {}).get("url")
        sha = page_sha.get(str(first_site)) or sha  # hash of the page the evidence URL points at
        return _record(
            "hiring", "available", "permitted_jobs" if not from_nav else "official_job_feed",
            note=f"{len(postings)} open position(s) found via {', '.join(sorted({p.get('method') for p in postings}))}{withheld_note}",
            value={
                "open_positions": len(postings),
                "postings": postings[:20],
                "careers_page": careers_url,
                "checked": checked,
                "withheld": withheld or None,
                "interpretation": "Postings observed on the company's own site and/or the official NAV feed; counts are of listings seen, not a headcount plan.",
            },
            source_url=first_site or nav_url or careers_url,
            retrieved_at=((profile.get("evidence", {}).get("website") or {}).get("retrieved_at") if first_site else None),
            content_sha256=sha if first_site else None,
            source_class="official_job_feed" if from_nav and not first_site else "company_owned",
            as_of=max(dated) if dated else None, confidence=0.9 if structured else 0.6,
        )

    # nothing found: say precisely what was and was not checked
    if withheld:  # postings existed but none is a currently open posting for this company
        return _record("hiring", "not_available", "permitted_jobs",
                       note="Postings were seen but none is usable" + withheld_note,
                       value={"checked": checked, "withheld": withheld}, source_url=careers_url,
                       content_sha256=page_sha.get(str(careers_url)) or sha, source_class="company_owned")
    if no_openings:
        return _record("hiring", "not_available", "permitted_jobs",
                       note=f"Company careers page states no open positions (\"{no_openings}\")",
                       value={"open_positions": 0, "careers_page": careers_url, "checked": checked},
                       source_url=careers_url, content_sha256=page_sha.get(str(careers_url)) or sha, source_class="company_owned")
    if careers_url or ats:
        where = careers_url or ats[0]
        return _record("hiring", "not_available", "permitted_jobs",
                       note=f"A careers page/portal exists ({where}) but no individual openings could be read from it",
                       value={"careers_page": careers_url, "ats_links": ats[:3], "checked": checked},
                       source_url=where, content_sha256=page_sha.get(str(where)) or sha, source_class="company_owned")

def ensure_hiring_placeholder(profile: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible name used by the batch runner; now does real extraction."""
    return extract_hiring(profile)