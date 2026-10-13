"""Hiring and public-activity signals read from already-fetched company-site HTML.

Pure functions: no network, no randomness. Everything returned is *observed on the page*:
a job posting is only reported if the page marks it up (schema.org JobPosting) or lists it
under the careers section; a public-activity item needs a title AND a parseable date.
Nothing here is ever inferred or invented.
"""

from __future__ import annotations

import email.utils
import re
import urllib.parse
from datetime import date, timedelta
from typing import Any

from bs4 import BeautifulSoup

CAREER_PATH = re.compile(
    r"(?<![a-z0-9æøå])(karriere|career|careers|jobb|jobs|job|ledige?[-_ ]?stilling|stillinger|stilling|rekruttering|join[-_ ]us|work[-_ ]with[-_ ]us|bli[-_ ]med|vacanc)",
    re.I,
)
JOB_CHILD_PATH = re.compile(
    r"/(stilling|stillinger|ledig-stilling|ledige-stillinger|job|jobs|jobb|position|positions|vacancy|vacancies|careers?|karriere)/[^/?#]+",
    re.I,
)
NO_OPENINGS = re.compile(
    r"(ingen\s+ledige(\s+stillinger)?|ikke\s+noen\s+ledige|for\s+tiden\s+ingen\s+ledige|no\s+(current\s+|open\s+)?(openings|vacancies|positions)|"
    r"no\s+open\s+(roles|jobs|positions)|we\s+have\s+no\s+open)",
    re.I,
)
ATS_HOSTS = (
    "webcruiter.no", "webcruiter.com", "jobylon.com", "easycruit.com", "teamtailor.com", "recman.no", "recman.io",
    "varbi.com", "hr-manager.net", "lever.co", "greenhouse.io", "workable.com", "smartrecruiters.com",
    "ashbyhq.com", "personio.de", "personio.com", "recruitee.com", "cvpartner.com", "hrmanager.no", "emply.com",
)
GENERIC_LINK_TEXT = {
    "les mer", "read more", "se alle", "vis alle", "apply", "søk", "søk nå", "apply now", "see all jobs", "alle stillinger",
    "ledige stillinger", "jobs", "careers", "karriere", "tilbake", "back", "neste", "next", "previous", "more", "mer",
    "open positions", "se alle stillinger", "send søknad", "kontakt oss", "contact us",
}
ARTICLE_TYPES = {"NewsArticle", "Article", "BlogPosting", "Report", "PressRelease", "SocialMediaPosting", "TechArticle"}

_MONTHS = {
    "januar": 1, "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2, "mars": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11, "desember": 12, "december": 12, "des": 12, "dec": 12,
}


def _clean(text: Any, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def parse_date(value: Any, today: date | None = None) -> str | None:
    """Return YYYY-MM-DD, or None. Rejects dates before 2000 or more than a day in the future."""
    text = _clean(value, 80)
    if not text:
        return None
    today = today or date.today()
    y = mo = d = None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        y, mo, d = (int(g) for g in match.groups())
    else:
        match = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
        if match:
            d, mo, y = (int(g) for g in match.groups())
        else:
            match = re.match(r"^(\d{1,2})\.?\s+([A-Za-zæøåÆØÅ]+)\.?,?\s+(\d{4})", text)
            if match and match.group(2).casefold() in _MONTHS:
                d, mo, y = int(match.group(1)), _MONTHS[match.group(2).casefold()], int(match.group(3))
            else:
                try:
                    parsed = email.utils.parsedate_to_datetime(text)
                    y, mo, d = parsed.year, parsed.month, parsed.day
                except (TypeError, ValueError, IndexError):
                    return None
    try:
        found = date(y, mo, d)
    except (TypeError, ValueError):
        return None
    if found < date(2000, 1, 1) or found > today + timedelta(days=1):
        return None
    return found.isoformat()


def is_careers_url(url: str, title: str = "") -> bool:
    path = urllib.parse.urlparse(url).path
    return bool(CAREER_PATH.search(path) or CAREER_PATH.search(title or ""))


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from _walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child)


def _kinds(node: dict[str, Any]) -> set[str]:
    kind = node.get("@type")
    return {str(k) for k in (kind if isinstance(kind, list) else [kind]) if k}


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    return _clean(value)


def _job_location(node: dict[str, Any]) -> str | None:
    location = node.get("jobLocation")
    location = location[0] if isinstance(location, list) and location else location
    if isinstance(location, dict):
        address = location.get("address")
        if isinstance(address, dict):
            return _clean(address.get("addressLocality") or address.get("addressRegion") or "") or None
        return _clean(address) or None
    return None


def _extract_jsonld(html: str, url: str) -> list[Any]:
    try:
        import extruct

        return extruct.extract(html, base_url=url, syntaxes=["json-ld"]).get("json-ld", []) or []
    except Exception:  # noqa: BLE001 - malformed JSON-LD must never break a crawl
        return []


def extract_page_signals(
    html: str,
    soup: BeautifulSoup,
    url: str,
    *,
    jsonld: list[Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Read job postings, dated items, feed links and ATS links from one page."""
    today = today or date.today()
    jsonld = _extract_jsonld(html, url) if jsonld is None else jsonld
    base = urllib.parse.urlparse(url)
    title_tag = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    careers_page = is_careers_url(url, title_tag)

    postings: list[dict[str, Any]] = []
    dated: list[dict[str, Any]] = []

    # 1. schema.org JobPosting / articles (strongest, machine-readable)
    for node in _walk(jsonld):
        kinds = _kinds(node)
        if "JobPosting" in kinds:
            title = _first_text(node.get("title") or node.get("name"))
            if title:
                postings.append({
                    "title": title,
                    "url": urllib.parse.urljoin(url, str(node.get("url") or "")) if node.get("url") else url,
                    "date_posted": parse_date(node.get("datePosted"), today),
                    "valid_through": parse_date(node.get("validThrough"), today + timedelta(days=3650)),
                    "location": _job_location(node),
                    "hiring_organisation": _first_text(node.get("hiringOrganization")) or None,
                    "method": "json-ld JobPosting",
                })
        elif kinds & ARTICLE_TYPES:
            when = parse_date(node.get("datePublished") or node.get("dateCreated"), today)
            title = _first_text(node.get("headline") or node.get("name"))
            raw_url = node.get("url") or node.get("mainEntityOfPage")
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("@id")
            link = urllib.parse.urljoin(url, raw_url) if isinstance(raw_url, str) and raw_url else url
            if when and title:
                dated.append({"title": title, "url": link, "date": when, "kind": "article", "method": "json-ld article"})

    # 2. <time datetime> blocks inside list items / articles
    for node in soup.select("time[datetime]"):
        when = parse_date(node.get("datetime"), today)
        if not when:
            continue
        container = node
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
            if container.name in {"article", "li", "tr"}:
                break
        heading = container.find(["h1", "h2", "h3", "h4"])
        anchor = None
        for candidate in container.find_all("a", href=True):
            if len(_clean(candidate.get_text(" ", strip=True))) >= 8:
                anchor = candidate
                break
        title = _clean(heading.get_text(" ", strip=True) if heading else (anchor.get_text(" ", strip=True) if anchor else ""))
        if len(title) < 4:
            continue
        link = urllib.parse.urljoin(url, str(anchor.get("href"))) if anchor else url
        dated.append({"title": title, "url": link, "date": when, "kind": "listing-item", "method": "html time tag"})

    # 3. article page metadata
    meta = soup.select_one('meta[property="article:published_time"], meta[name="article:published_time"], meta[name="date"]')
    if meta:
        when = parse_date(meta.get("content"), today)
        og = soup.select_one('meta[property="og:title"]')
        title = _clean((og.get("content") if og else "") or title_tag)
        if when and title:
            canonical = soup.select_one('link[rel="canonical"]')
            link = urllib.parse.urljoin(url, str(canonical.get("href"))) if canonical and canonical.get("href") else url
            dated.append({"title": title, "url": link, "date": when, "kind": "article", "method": "article:published_time meta"})

    # 4. listing links on a careers page (heuristic, lower confidence)
    no_openings = None
    if careers_page:
        page_path = base.path.rstrip("/")
        depth = page_path.count("/")
        seen = {p["url"] for p in postings}
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absolute = urllib.parse.urljoin(url, href)
            parsed = urllib.parse.urlparse(absolute)
            if parsed.netloc.lower() != base.netloc.lower() or re.search(r"[?&](page|side|p)=", parsed.query, re.I):
                continue
            path = parsed.path.rstrip("/")
            child = bool(page_path) and path.startswith(page_path + "/") and path.count("/") > depth
            if not (child or JOB_CHILD_PATH.search(path)):
                continue
            if path.rsplit("/", 1)[-1].casefold() in {"apply", "soknad", "søknad", "kontakt", "contact", "om-oss", "about", "feed", "rss"}:
                continue
            text = _clean(anchor.get_text(" ", strip=True), 160)
            if not (5 <= len(text) <= 140) or text.casefold() in GENERIC_LINK_TEXT:
                continue
            clean_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            if clean_url in seen:
                continue
            seen.add(clean_url)
            postings.append({"title": text, "url": clean_url, "date_posted": None, "valid_through": None, "location": None,
                             "hiring_organisation": None, "method": "careers-page listing link"})
            if len(postings) >= 25:
                break
        match = NO_OPENINGS.search(soup.get_text(" ", strip=True))
        if match and not postings:
            no_openings = _clean(match.group(0), 120)

    # 5. ATS portals and RSS/Atom feeds (recorded, not crawled here)
    ats: list[str] = []
    for anchor in soup.select("a[href]"):
        host = (urllib.parse.urlparse(urllib.parse.urljoin(url, str(anchor.get("href") or ""))).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in ATS_HOSTS):
            link = urllib.parse.urljoin(url, str(anchor.get("href")))
            if link not in ats:
                ats.append(link)
    feeds = []
    for node in soup.select('link[rel~="alternate"][href]'):
        kind = str(node.get("type") or "").lower()
        if "rss" in kind or "atom" in kind:
            feeds.append(urllib.parse.urljoin(url, str(node.get("href"))))

    def unique(items: list[dict[str, Any]], key: tuple[str, ...]) -> list[dict[str, Any]]:
        out, seen_keys = [], set()
        for item in items:
            k = tuple(item.get(f) for f in key)
            if k not in seen_keys:
                seen_keys.add(k)
                out.append(item)
        return out

    return {
        "page_url": url,
        "careers_page": careers_page,
        "job_postings": unique(postings, ("title", "url"))[:25],
        "dated_items": unique(dated, ("title", "date"))[:25],
        "no_openings_statement": no_openings,
        "ats_links": ats[:5],
        "feed_links": list(dict.fromkeys(feeds))[:3],
    }


def parse_feed(raw: bytes, base_url: str, today: date | None = None) -> list[dict[str, Any]]:
    """Parse RSS/Atom bytes into dated items. Entity expansion and network access are disabled."""
    from lxml import etree

    today = today or date.today()
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=True)
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []
    items: list[dict[str, Any]] = []
    for element in root.iter():
        if not isinstance(element.tag, str) or etree.QName(element).localname not in {"item", "entry"}:
            continue
        fields: dict[str, str] = {}
        link = ""
        for child in element:
            if not isinstance(child.tag, str):
                continue
            name = etree.QName(child).localname
            if name == "link":
                href = child.get("href")
                if href and child.get("rel", "alternate") == "alternate":
                    link = href
                elif child.text and not link:
                    link = child.text.strip()
            elif name in {"title", "pubDate", "published", "updated", "date"} and name not in fields:
                fields[name] = _clean(child.text, 300)
        when = parse_date(fields.get("pubDate") or fields.get("published") or fields.get("date") or fields.get("updated"), today)
        title = fields.get("title", "")
        if when and title:
            items.append({"title": title, "url": urllib.parse.urljoin(base_url, link) if link else base_url,
                          "date": when, "kind": "feed-item", "method": "rss/atom feed"})
        if len(items) >= 50:
            break
    return items
