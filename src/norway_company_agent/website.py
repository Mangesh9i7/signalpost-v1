from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any

import extruct
import tldextract
import trafilatura
from bs4 import BeautifulSoup

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
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse",
    "management", "team", "people", "locations", "lokasjoner",
    "avdelinger", "butikker", "news", "press", "aktuelt", "nyheter",
)


def assert_public_url(url: str) -> None:
    """Validate HTTP(S) URLs and reject hosts resolving to non-public IPs."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Malformed URL") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing credentials are blocked")

    if any(char in host for char in ("\r", "\n", "\x00")):
        raise ValueError("Malformed hostname")

    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        raise ValueError("Local hosts are blocked")

    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)

    try:
        # IP literals do not need DNS resolution.
        try:
            addresses = {str(ipaddress.ip_address(host))}
        except ValueError:
            results = socket.getaddrinfo(
                host,
                effective_port,
                type=socket.SOCK_STREAM,
            )
            addresses = {item[4][0].split("%")[0] for item in results}

    except (socket.gaierror, OSError, ValueError) as exc:
        raise ValueError("Hostname did not resolve safely") from exc

    if not addresses:
        raise ValueError("Hostname did not resolve")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("Invalid resolved IP address") from exc

        if not ip.is_global:
            raise ValueError(
                "Private, loopback, link-local, multicast, and reserved addresses are blocked"
            )


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate redirect targets before urllib follows them."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(
            req, fp, code, msg, headers, newurl
        )


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler)


def normalize_homepage(value: str | None) -> str | None:
    """Normalize a registry-provided website field to one HTTP(S) URL."""
    parts = re.split(r"[\s,;]+", str(value or "").strip())
    value = parts[0] if parts else ""

    if not value:
        return None

    # Do not turn mailto:, javascript:, file:, etc. into HTTPS URLs.
    if re.match(
        r"^[a-z][a-z0-9+.-]*:",
        value,
        re.IGNORECASE,
    ) and not re.match(r"^https?://", value, re.IGNORECASE):
        return None

    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value

    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None

    if parsed.username is not None or parsed.password is not None:
        # Credentials are not needed to identify a public company website.
        return None

    host = host.lower().rstrip(".")
    if not host:
        return None

    # IPv6 literals require brackets in the URL authority.
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"

    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            "",  # Drop query parameters from the seed URL.
            "",  # Drop fragments.
        )
    )


# Never fetch or update the public-suffix list over the network at runtime.
_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _registered_domain(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return _TLD(host).top_domain_under_public_suffix


def _site_key(url: str) -> str:
    """Return the registered domain, falling back to the hostname."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""

    if not host:
        return ""

    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    return _registered_domain(url) or host


def _read_limited(
    response: Any,
    max_bytes: int,
    deadline_s: float = 30.0,
) -> bytes:
    """Read at most max_bytes + 1 bytes, with a wall-clock deadline."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    started = time.monotonic()
    chunks: list[bytes] = []
    total = 0

    while total <= max_bytes:
        if time.monotonic() - started > deadline_s:
            raise TimeoutError(
                "Response did not complete within the time limit"
            )

        chunk = response.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            break

        chunks.append(chunk)
        total += len(chunk)

    return b"".join(chunks)


_ROBOTS_CACHE: dict[str, Any] = {}
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_MISSING = object()
_DISALLOW_ALL = object()


def _robots_allowed(url: str, timeout: float) -> bool:
    """Fail closed if robots.txt is unreachable or returns a server error."""
    assert_public_url(url)

    parsed = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/robots.txt", "", "")
    )

    with _ROBOTS_LOCK:
        parser = _ROBOTS_CACHE.get(robots_url, _ROBOTS_MISSING)

    if parser is _ROBOTS_MISSING:
        new_parser = urllib.robotparser.RobotFileParser()
        new_parser.set_url(robots_url)

        request = urllib.request.Request(
            robots_url,
            headers={"User-Agent": USER_AGENT},
        )

        try:
            with SAFE_OPENER.open(request, timeout=timeout) as response:
                robots_bytes = _read_limited(
                    response,
                    512_000,
                    deadline_s=15.0,
                )
                new_parser.parse(
                    robots_bytes.decode(
                        "utf-8", errors="replace"
                    ).splitlines()
                )
            parser = new_parser

        except urllib.error.HTTPError as exc:
            # A 4xx robots response means no usable robots policy was found.
            # A 5xx response means the policy cannot be retrieved safely.
            parser = None if 400 <= exc.code < 500 else _DISALLOW_ALL

        except Exception:
            parser = _DISALLOW_ALL

        with _ROBOTS_LOCK:
            # Concurrent callers reuse whichever result was cached first.
            existing = _ROBOTS_CACHE.get(
                robots_url, _ROBOTS_MISSING
            )
            if existing is _ROBOTS_MISSING:
                _ROBOTS_CACHE[robots_url] = parser
            else:
                parser = existing

    if parser is _DISALLOW_ALL:
        return False

    return True if parser is None else parser.can_fetch(USER_AGENT, url)


def normalize_social_url(url: str) -> dict[str, str] | None:
    """Normalize supported company social-profile URLs; reject share links."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except (AttributeError, ValueError):
        return None

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    if parsed.username is not None or parsed.password is not None:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next(
        (
            label
            for domain, label in SOCIAL_HOSTS.items()
            if host == domain or host.endswith("." + domain)
        ),
        None,
    )

    if not platform:
        return None

    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]

    rejected_first = {
        "facebook": {
            "sharer", "sharer.php", "share.php", "dialog",
            "policy.php", "privacy", "events", "groups", "plugins",
        },
        "instagram": {
            "p", "reel", "reels", "stories", "explore",
        },
        "x": {"intent", "share", "home", "search", "i"},
    }

    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None

    if platform == "facebook" and lowered[0] == "profile.php":
        return None

    if platform == "linkedin" and (
        lowered[0] != "company" or len(parts) < 2
    ):
        return None

    if platform == "youtube" and (
        lowered[0] not in {"channel", "user", "c"}
        and not parts[0].startswith("@")
    ):
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

    # Drop query strings and fragments; keep only the canonical profile path.
    return {
        "platform": platform,
        "url": f"https://{canonical_host}/{'/'.join(parts)}",
    }


def _social_links(
    base_url: str,
    soup: BeautifulSoup,
) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    candidates = [
        str(node.get("href") or "")
        for node in soup.select("a[href]")
    ]
    candidates.extend(
        str(node.get("data-href") or "")
        for node in soup.select("[data-href]")
    )
    candidates.extend(
        str(node.get("src") or "")
        for node in soup.select("iframe[src]")
    )

    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)

        try:
            parsed_candidate = urllib.parse.urlsplit(url)
        except ValueError:
            continue

        if (
            (parsed_candidate.hostname or "").casefold().removeprefix("www.")
            == "facebook.com"
            and parsed_candidate.path.startswith("/plugins/")
        ):
            embedded = urllib.parse.parse_qs(
                parsed_candidate.query
            ).get("href", [])
            if embedded:
                url = urllib.parse.urljoin(base_url, embedded[0])

        normalized = normalize_social_url(url)
        if normalized:
            found[(normalized["platform"], normalized["url"])] = normalized

    return sorted(
        found.values(),
        key=lambda item: (item["platform"], item["url"]),
    )


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

    return sorted(
        found.values(),
        key=lambda item: (item["platform"], item["url"]),
    )


_NEWS_START = PRIORITY_TERMS.index("news")


def _priority_links(
    base_url: str,
    soup: BeautifulSoup,
    limit: int = 4,
    extra: int = 2,
) -> list[str]:
    """Select relevant same-host pages, with separate slots for careers/news."""
    base = urllib.parse.urlsplit(base_url)
    candidates: dict[str, int] = {}
    special: set[str] = set()

    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)

        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            continue

        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.netloc.lower() != base.netloc.lower()
        ):
            continue

        haystack = (
            parsed.path + " " + anchor.get_text(" ", strip=True)
        ).casefold()

        rank = next(
            (
                index
                for index, term in enumerate(PRIORITY_TERMS)
                if term in haystack
            ),
            None,
        )

        is_special = rank is not None and rank >= _NEWS_START

        if rank is None and CAREER_PATH.search(haystack):
            rank, is_special = len(PRIORITY_TERMS), True

        if rank is None:
            continue

        clean = urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                "",
                "",
            )
        )

        if clean.rstrip("/") == base_url.rstrip("/"):
            continue

        candidates[clean] = min(rank, candidates.get(clean, rank))

        if is_special:
            special.add(clean)

    ordered = [
        url
        for url, _ in sorted(
            candidates.items(),
            key=lambda item: (item[1], item[0]),
        )
    ]

    chosen = ordered[:max(0, limit)]

    if extra > 0:
        chosen += [
            url
            for url in ordered
            if url in special and url not in chosen
        ][:extra]

    return chosen


def _decode_html(raw: bytes, content_type: str) -> str:
    match = re.search(
        r"charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)",
        content_type,
        flags=re.IGNORECASE,
    )

    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass

    return raw.decode("utf-8", errors="replace")


def _safe_signals(
    html: str,
    soup: BeautifulSoup,
    url: str,
    jsonld: list[Any] | None = None,
) -> dict[str, Any]:
    try:
        return extract_page_signals(
            html, soup, url, jsonld=jsonld
        )
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {str(exc)[:120]}"
        }


def _fetch_secondary_page(
    url: str,
    *,
    homepage_domain: str,
    timeout: float,
    max_bytes: int,
) -> tuple[
    dict[str, Any] | None,
    list[dict[str, str]],
    int,
    int,
    int,
    str | None,
]:
    try:
        assert_public_url(url)

        if _site_key(url) != homepage_domain:
            return None, [], 0, 0, 0, "outside registered domain"

        if not _robots_allowed(url, timeout):
            return None, [], 1, 0, 0, "robots.txt disallows page"

    except ValueError as exc:
        return None, [], 0, 0, 0, str(exc)[:120]

    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = _read_limited(response, max_bytes)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()

            assert_public_url(final_url)

            if _site_key(final_url) != homepage_domain:
                return (
                    None, [], 1, len(raw), elapsed,
                    "redirected outside registered domain",
                )

            if len(raw) > max_bytes:
                return (
                    None, [], 1, len(raw), elapsed,
                    "page exceeds byte limit",
                )

            if "html" not in content_type.casefold():
                return (
                    None, [], 1, len(raw), elapsed,
                    "unsupported content type",
                )

        page_html = _decode_html(raw, content_type)
        page_soup = BeautifulSoup(page_html, "lxml")

        try:
            page_text = trafilatura.extract(
                page_html,
                url=final_url,
                include_links=False,
                include_tables=False,
                favor_precision=True,
            ) or ""
        except Exception:
            page_text = ""

        page = {
            "url": final_url,
            "title": (
                page_soup.title.get_text(" ", strip=True)[:500]
                if page_soup.title
                else ""
            ),
            "main_text_excerpt": page_text[:5000],
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "signals": _safe_signals(
                page_html, page_soup, final_url
            ),
        }

        return page, _social_links(final_url, page_soup), 1, len(raw), elapsed, None

    except Exception as exc:
        return (
            None,
            [],
            1,
            0,
            int((time.monotonic() - started) * 1000),
            f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _fetch_feed(
    url: str,
    *,
    homepage_domain: str,
    timeout: float,
    max_bytes: int = 500_000,
) -> tuple[list[dict[str, Any]], int, int, int, str | None]:
    """Fetch a single RSS/Atom feed from the company's registered domain."""
    try:
        assert_public_url(url)

        if _site_key(url) != homepage_domain:
            return [], 0, 0, 0, "feed is outside registered domain"

        if not _robots_allowed(url, timeout):
            return [], 1, 0, 0, "robots.txt disallows feed"

    except ValueError as exc:
        return [], 0, 0, 0, str(exc)[:120]

    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml, text/xml"
            ),
        },
    )

    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = _read_limited(response, max_bytes)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()

            assert_public_url(final_url)

        if len(raw) > max_bytes:
            return [], 1, len(raw), elapsed, "feed exceeds byte limit"

        if _site_key(final_url) != homepage_domain:
            return [], 1, len(raw), elapsed, "feed redirected outside registered domain"

        return parse_feed(raw, final_url), 1, len(raw), elapsed, None

    except Exception as exc:
        return (
            [],
            1,
            0,
            int((time.monotonic() - started) * 1000),
            f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])

            if kinds & {
                "Organization",
                "Corporation",
                "LocalBusiness",
                "Store",
                "Restaurant",
            }:
                values.append(value)

            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2:
        return "js_fallback_candidate"
    return "static_complete"


def _fetch_website_raw(
    url: str | None,
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(
        re.match(r"^https?://", supplied_url, re.IGNORECASE)
    )

    normalized = normalize_homepage(url)

    empty_metrics = {
        "requests": 0,
        "bytes": 0,
        "latencies_ms": [],
    }

    if not normalized:
        return (
            evidence(
                "website",
                "not_found",
                "registry_linked_company_website",
                "https://data.brreg.no/enhetsregisteret/api/enheter",
                note="No valid registry website URL",
            ),
            empty_metrics,
        )

    if _is_search_engine_url(normalized):
        return (
            evidence(
                "website",
                "not_found",
                "registry_linked_company_website",
                "https://data.brreg.no/enhetsregisteret/api/enheter",
                note="Registry website field contains a search-engine or map link",
            ),
            empty_metrics,
        )

    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return (
            evidence(
                "website",
                "blocked",
                "registry_linked_company_website",
                normalized,
                note=str(exc),
            ),
            empty_metrics,
        )

    try:
        robots_ok = _robots_allowed(normalized, timeout)
    except ValueError as exc:
        return (
            evidence(
                "website",
                "blocked",
                "registry_linked_company_website",
                normalized,
                note=str(exc)[:200],
            ),
            empty_metrics,
        )

    if not robots_ok:
        return (
            evidence(
                "website",
                "blocked",
                "registry_linked_company_website",
                normalized,
                note="robots.txt disallows this user agent or could not be read",
            ),
            {
                "requests": 1,
                "bytes": 0,
                "latencies_ms": [],
            },
        )

    started = time.monotonic()
    request = urllib.request.Request(
        normalized,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = _read_limited(response, max_bytes)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()

            assert_public_url(final_url)

        metrics = {
            "requests": 1,
            "bytes": len(raw),
            "latencies_ms": [elapsed],
        }

        if len(raw) > max_bytes:
            return (
                evidence(
                    "website",
                    "blocked",
                    "registry_linked_company_website",
                    normalized,
                    note="Homepage exceeds byte limit",
                ),
                metrics,
            )

        if "html" not in content_type.casefold():
            return (
                evidence(
                    "website",
                    "source_error",
                    "registry_linked_company_website",
                    normalized,
                    note=f"Unsupported content type: {content_type}",
                ),
                metrics,
            )

        if _site_key(final_url) != _site_key(normalized):
            return (
                evidence(
                    "website",
                    "ambiguous",
                    "registry_linked_company_website",
                    normalized,
                    note=(
                        "Registry website redirects to a different registered "
                        "domain; content withheld"
                    ),
                ),
                metrics,
            )

        html = _decode_html(raw, content_type)
        soup = BeautifulSoup(html, "lxml")

        try:
            structured = extruct.extract(
                html,
                base_url=final_url,
                syntaxes=["json-ld", "microdata", "opengraph"],
            )
        except Exception:
            structured = {}

        try:
            text = trafilatura.extract(
                html,
                url=final_url,
                include_links=False,
                include_tables=False,
                favor_precision=True,
            ) or ""
        except Exception:
            text = ""

        title = (
            soup.title.get_text(" ", strip=True)
            if soup.title
            else ""
        )

        description_tag = soup.select_one(
            'meta[name="description"], meta[property="og:description"]'
        )
        description = (
            str(description_tag.get("content") or "").strip()
            if description_tag
            else ""
        )

        jsonld = structured.get("json-ld", [])
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
            "signals": _safe_signals(
                html, soup, final_url, jsonld
            ),
        }

        pages = [
            {
                "url": final_url,
                "title": title[:500],
                "main_text_excerpt": text[:5000],
                "content_sha256": value["content_sha256"],
            }
        ]

        social = list(value["social_links"])
        crawl_errors = []
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = _site_key(final_url)

        for page_url in _priority_links(final_url, soup):
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = (
                _fetch_secondary_page(
                    page_url,
                    homepage_domain=homepage_domain,
                    timeout=timeout,
                    max_bytes=min(max_bytes, 1_000_000),
                )
            )

            metrics["requests"] += page_requests
            bytes_received += page_bytes

            if page_elapsed:
                page_latencies.append(page_elapsed)

            if page:
                pages.append(page)
                social.extend(page_social)
            elif page_error:
                crawl_errors.append(
                    {"url": page_url, "error": page_error}
                )

        feed_url = next(
            (
                link
                for source in [
                    value.get("signals") or {},
                    *[
                        page.get("signals") or {}
                        for page in pages
                    ],
                ]
                for link in source.get("feed_links", [])
                if isinstance(link, str)
                and _site_key(link) == homepage_domain
            ),
            None,
        )

        value["feed_url"] = feed_url
        value["feed_items"] = []

        if feed_url:
            (
                feed_items,
                feed_requests,
                feed_bytes,
                feed_elapsed,
                feed_error,
            ) = _fetch_feed(
                feed_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
            )

            value["feed_items"] = feed_items
            metrics["requests"] += feed_requests
            bytes_received += feed_bytes

            if feed_elapsed:
                page_latencies.append(feed_elapsed)

            if feed_error:
                crawl_errors.append(
                    {"url": feed_url, "error": feed_error}
                )

        value["pages"] = pages
        value["social_links"] = list(
            {
                (item["platform"], item["url"]): item
                for item in social
            }.values()
        )
        value["crawl_errors"] = crawl_errors

        metrics["bytes"] = bytes_received
        metrics["latencies_ms"] = page_latencies

        record = evidence(
            "website",
            "available",
            "registry_linked_company_website",
            final_url,
            value=value,
            note="Company-controlled claim layer; not an official registry fact",
            content_sha256=value["content_sha256"],
        )

        return record, metrics

    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = (
            "not_found"
            if exc.code in {404, 410}
            else "source_error"
        )

        return (
            evidence(
                "website",
                status,
                "registry_linked_company_website",
                normalized,
                note=f"HTTP {exc.code}",
            ),
            {
                "requests": 1,
                "bytes": 0,
                "latencies_ms": [elapsed],
            },
        )

    except urllib.error.URLError as exc:
        first_elapsed = int((time.monotonic() - started) * 1000)

        if not supplied_scheme and normalized.startswith("https://"):
            fallback_url = "http://" + normalized[len("https://"):]

            # Validate the fallback independently. Do not bypass URL checks.
            try:
                assert_public_url(fallback_url)
            except ValueError:
                fallback_url = ""

            if fallback_url:
                record, metrics = _fetch_website_raw(
                    fallback_url,
                    timeout=timeout,
                    max_bytes=max_bytes,
                )
                metrics["latencies_ms"].insert(0, first_elapsed)
                return record, metrics

        return (
            evidence(
                "website",
                "source_error",
                "registry_linked_company_website",
                normalized,
                note=f"URLError: {str(exc.reason)[:180]}",
            ),
            {
                "requests": 1,
                "bytes": 0,
                "latencies_ms": [first_elapsed],
            },
        )

    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)

        return (
            evidence(
                "website",
                "source_error",
                "registry_linked_company_website",
                normalized,
                note=f"{type(exc).__name__}: {str(exc)[:180]}",
            ),
            {
                "requests": 1,
                "bytes": 0,
                "latencies_ms": [elapsed],
            },
        )


# Search-engine and map links must not become company evidence.
_SEARCH_HOST = re.compile(
    r"^(?:(?:www|maps|m|search|encrypted)\.)?"
    r"(?:google\.[a-z]{2,3}(?:\.[a-z]{2})?|bing\.com|"
    r"duckduckgo\.com|yahoo\.com|yandex\.[a-z]{2,3}|"
    r"baidu\.com|ecosia\.org|startpage\.com|qwant\.com|brave\.com)$"
)

_ANY_URL = re.compile(r"https?://[^\s\"'<>)\]\\]+", re.IGNORECASE)


def _is_search_engine_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
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
    """Remove search-engine/map URLs from nested evidence structures."""
    if isinstance(value, str):
        if _is_search_engine_url(value):
            return None

        if "://" in value:
            return _ANY_URL.sub(
                lambda match: (
                    ""
                    if _is_search_engine_url(match.group(0))
                    else match.group(0)
                ),
                value,
            )

        return value

    if isinstance(value, dict):
        result = {}

        for key, item in value.items():
            cleaned = strip_search_engine_urls(item)

            if cleaned is None and item is not None:
                continue

            result[key] = cleaned

        return result

    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := strip_search_engine_urls(item)) is not None
        ]

    return value


def fetch_website(
    url: str | None,
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
) -> tuple[dict[str, Any], dict[str, Any]]:
    record, metrics = _fetch_website_raw(
        url,
        timeout=timeout,
        max_bytes=max_bytes,
    )
    return strip_search_engine_urls(record), metrics
