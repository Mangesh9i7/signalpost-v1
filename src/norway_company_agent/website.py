from __future__ import annotations

import hashlib
import json
import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence
from .signals import CAREER_PATH, extract_page_signals, parse_feed

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
)


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def normalize_homepage(value: str | None) -> str | None:
    # The registry field is free text: it can hold several addresses ("a.no, b.no"). Use the first one only.
    parts = re.split(r"[\s,;]+", str(value or "").strip())
    value = parts[0] if parts else ""
    if not value:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) and not re.match(r"^https?://", value, re.I):
        if "." not in value.split(":", 1)[0]:  # javascript:, mailto:, file: ... are never websites
            return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    try:
        parsed = urllib.parse.urlparse(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    netloc = host if ":" not in host else f"[{host}]"  # drops any user:password@ part
    if port:
        netloc += f":{port}"
    return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path or "/", "", "", ""))


# The public-suffix list ships inside tldextract. Never let it fetch a fresh copy over the network
# (that would be an unexpected outbound request, and a stall, inside a time-boxed run).
_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return _TLD(parsed.hostname or "").top_domain_under_public_suffix


def _site_key(url: str) -> str:
    """Registered domain, or the bare host for IP addresses / single-label hosts (which have none)."""
    return _registered_domain(url) or (urllib.parse.urlparse(url).hostname or "").lower()


def _read_limited(response: Any, max_bytes: int, deadline_s: float = 30.0) -> bytes:
    """Read at most max_bytes+1 bytes and give up after deadline_s of wall-clock time.

    A socket timeout alone resets on every byte, so a server that drips data could hold a worker forever.
    """
    started = time.monotonic()
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        if time.monotonic() - started > deadline_s:
            raise TimeoutError("response not completed within the time limit")
        chunk = response.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


_ROBOTS_CACHE: dict[str, Any] = {}
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_MISSING = object()
_DISALLOW_ALL = object()  # robots.txt exists but could not be read (5xx / unreachable)


def _robots_allowed(url: str, timeout: float) -> bool:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    with _ROBOTS_LOCK:
        parser = _ROBOTS_CACHE.get(robots_url, _ROBOTS_MISSING)
    if parser is _ROBOTS_MISSING:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        try:
            with SAFE_OPENER.open(request, timeout=timeout) as response:
                parser.parse(_read_limited(response, 512_000, 15.0).decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as exc:
            # RFC 9309: a 4xx answer means "no robots.txt" (everything allowed). A 5xx answer means the
            # file could not be read, so the crawler must assume the site is off limits.
            parser = None if 400 <= exc.code < 500 else _DISALLOW_ALL
        except Exception:  # noqa: BLE001 - network error/timeout: robots.txt unreachable, so do not crawl
            parser = _DISALLOW_ALL
        with _ROBOTS_LOCK:
            _ROBOTS_CACHE[robots_url] = parser  # one robots.txt request per host per run
    if parser is _DISALLOW_ALL:
        return False
    return True if parser is None else parser.can_fetch(USER_AGENT, url)


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


_NEWS_START = PRIORITY_TERMS.index("news")


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4, extra: int = 2) -> list[str]:
    """Pick pages worth fetching: `limit` identity/general pages plus up to `extra` careers/news pages.

    The extra slots stop careers and news pages being crowded out by about/contact pages.
    """
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    special: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in haystack), None)
        is_special = rank is not None and rank >= _NEWS_START
        if rank is None and CAREER_PATH.search(haystack):
            rank, is_special = len(PRIORITY_TERMS), True
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
        if is_special:
            special.add(clean)
    ordered = [url for url, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0]))]
    chosen = ordered[:limit]
    if extra:
        chosen += [url for url in ordered if url in special and url not in chosen][:extra]
    return chosen


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    try:
        if not _robots_allowed(url, timeout):
            return None, [], 1, 0, 0, "robots.txt disallows page"
    except ValueError as exc:  # unsafe or unresolvable address: skip this page, keep the homepage
        return None, [], 0, 0, 0, str(exc)[:120]
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = _read_limited(response, max_bytes)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _site_key(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "signals": _safe_signals(page_html, page_soup, final_url),
        }
        return page, _social_links(final_url, page_soup), 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _safe_signals(html: str, soup: BeautifulSoup, url: str, jsonld: list[Any] | None = None) -> dict[str, Any]:
    try:
        return extract_page_signals(html, soup, url, jsonld=jsonld)
    except Exception as exc:  # noqa: BLE001 - signals are a bonus; never lose the page
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _fetch_feed(url: str, *, homepage_domain: str, timeout: float, max_bytes: int = 500_000) -> tuple[list[dict[str, Any]], int, int, int, str | None]:
    """Fetch one RSS/Atom feed from the verified company domain. Returns items, requests, bytes, ms, error."""
    try:
        if _site_key(url) != homepage_domain:
            return [], 0, 0, 0, "feed is outside the company's registered domain"
        if not _robots_allowed(url, timeout):
            return [], 1, 0, 0, "robots.txt disallows feed"
    except ValueError as exc:
        return [], 0, 0, 0, str(exc)[:120]
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = _read_limited(response, max_bytes)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
        if len(raw) > max_bytes:
            return [], 1, len(raw), elapsed, "feed exceeds byte limit"
        if _site_key(final_url) != homepage_domain:
            return [], 1, len(raw), elapsed, "feed redirected outside registered domain"
        return parse_feed(raw, final_url), 1, len(raw), elapsed, None
    except Exception as exc:  # noqa: BLE001
        return [], 1, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def _fetch_website_raw(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if _is_search_engine_url(normalized):
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter",
                        note="The registry website field holds a search-engine or map link, not a company website"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        robots_ok = _robots_allowed(normalized, timeout)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)[:200]), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not robots_ok:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent (or could not be read)"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = _read_limited(response, max_bytes)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        if _site_key(final_url) != _site_key(normalized):
            # The registry site now belongs to another domain (parked, sold, redirected to a group site).
            # Facts from there are not facts about the registry-declared website: withhold them.
            return evidence("website", "ambiguous", "registry_linked_company_website", normalized,
                            note=f"Registry website redirects to a different domain ({_site_key(final_url)}); content withheld"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _site_key(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "social_links": _social_links(final_url, soup),
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "extraction_state": _extraction_state(text, soup),
            "signals": _safe_signals(html, soup, final_url, structured.get("json-ld", [])),
        }
        pages = [{"url": final_url, "title": title[:500], "main_text_excerpt": text[:5000], "content_sha256": value["content_sha256"]}]
        social = value["social_links"]
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = _site_key(final_url)
        for page_url in _priority_links(final_url, soup):
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                social.extend(page_social)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        feed_url = next(
            (
                link
                for source in [value.get("signals") or {}, *[(page.get("signals") or {}) for page in pages]]
                for link in source.get("feed_links", [])
                if _site_key(link) == homepage_domain
            ),
            None,
        )
        value["feed_url"] = feed_url
        value["feed_items"] = []
        if feed_url:
            feed_items, feed_requests, feed_bytes, feed_elapsed, feed_error = _fetch_feed(feed_url, homepage_domain=homepage_domain, timeout=timeout)
            value["feed_items"] = feed_items
            requests += feed_requests
            bytes_received += feed_bytes
            if feed_elapsed:
                page_latencies.append(feed_elapsed)
            if feed_error:
                crawl_errors.append({"url": feed_url, "error": feed_error})
        value["pages"] = pages
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = _fetch_website_raw("http://" + normalized[len("https://"):], timeout=timeout, max_bytes=max_bytes)
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}


# ---------------------------------------------------------------------------
# Search-engine / map links must never end up in evidence (rule R5).
# Company sites often publish them (schema.org `hasMap`, "find us" links, embeds); they are not facts about
# the company, so they are dropped from everything the crawler returns.
# ---------------------------------------------------------------------------
_SEARCH_HOST = re.compile(
    r"^(?:(?:www|maps|m|search|encrypted)\.)?(?:google\.[a-z]{2,3}(?:\.[a-z]{2})?|bing\.com|duckduckgo\.com|"
    r"yahoo\.com|yandex\.[a-z]{2,3}|baidu\.com|ecosia\.org|startpage\.com|qwant\.com|brave\.com)$"
)
_ANY_URL = re.compile(r"https?://[^\s\"'<>)\]\\]+", re.I)


def _is_search_engine_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url).strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.lower()
    if host in {"maps.app.goo.gl", "g.page"}:
        return True
    if host in {"goo.gl", "www.goo.gl"} and path.startswith("/maps"):
        return True
    if host == "brave.com" or host.endswith(".brave.com"):
        return host.startswith("search.")
    return bool(_SEARCH_HOST.match(host))


def strip_search_engine_urls(value: Any) -> Any:
    """Return `value` without search-engine / map URLs (dict entries and list items are removed, URLs inside
    longer text are cut out). Everything else, including sites.google.com company pages, is left untouched."""
    if isinstance(value, str):
        if _is_search_engine_url(value):
            return None
        if "://" in value:
            return _ANY_URL.sub(lambda m: "" if _is_search_engine_url(m.group(0)) else m.group(0), value)
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            cleaned = strip_search_engine_urls(item)
            if cleaned is None and item is not None:
                continue  # the whole value was a search-engine URL
            out[key] = cleaned
        return out
    if isinstance(value, list):
        return [cleaned for cleaned in (strip_search_engine_urls(item) for item in value) if cleaned is not None]
    return value


def fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> tuple[dict[str, Any], dict[str, Any]]:
    record, metrics = _fetch_website_raw(url, timeout=timeout, max_bytes=max_bytes)
    return strip_search_engine_urls(record), metrics