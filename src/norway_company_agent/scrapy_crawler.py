from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import scrapy
from bs4 import BeautifulSoup
from scrapy.exceptions import IgnoreRequest

from .crawl_events import error_page_event, extract_page_event
from .website import (
    USER_AGENT,
    _priority_links,
    _site_key,
    assert_public_url,
    normalize_homepage,
)


class PublicNetworkMiddleware:
    """Reject unsafe URLs and requests that leave the company's registered domain."""

    def process_request(self, request, spider):
        try:
            assert_public_url(request.url)

            expected_site = request.meta.get("company_site_key")
            if expected_site and _site_key(request.url) != expected_site:
                raise ValueError(
                    "Request is outside the company's registered domain"
                )

            # Count downloader attempts, including retries and redirects.
            if not spider.reserve_global_request():
                raise IgnoreRequest("Global crawl request budget exhausted")

        except ValueError as exc:
            raise IgnoreRequest(str(exc)) from exc

    def process_response(self, request, response, spider):
        try:
            assert_public_url(response.url)

            expected_site = request.meta.get("company_site_key")
            if expected_site and _site_key(response.url) != expected_site:
                raise IgnoreRequest(
                    "Response is outside the company's registered domain"
                )

        except ValueError as exc:
            raise IgnoreRequest(str(exc)) from exc

        return response


class OperationalTelemetryMiddleware:
    def __init__(self, crawler):
        self.crawler = crawler
        crawler.signalpost_request_latency_ms = []
        crawler.signalpost_response_download_latency_ms = []
        crawler.signalpost_failure_attempt_latency_ms = []
        crawler.signalpost_request_domains = Counter()
        crawler.signalpost_response_statuses = Counter()
        crawler.signalpost_error_buckets = Counter()

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_request(self, request, spider):
        request.meta["_signalpost_started_at"] = time.monotonic()

    def _record(self, request):
        started = request.meta.get("_signalpost_started_at")
        if started is not None:
            self.crawler.signalpost_request_latency_ms.append(
                (time.monotonic() - started) * 1000
            )

        host = (
            urllib.parse.urlparse(request.url).hostname or "unknown"
        ).casefold()
        self.crawler.signalpost_request_domains[host] += 1

    def process_response(self, request, response, spider):
        self._record(request)

        download_latency = request.meta.get("download_latency")
        if download_latency is not None:
            self.crawler.signalpost_response_download_latency_ms.append(
                float(download_latency) * 1000
            )

        self.crawler.signalpost_response_statuses[str(response.status)] += 1
        return response

    def process_exception(self, request, exception, spider):
        started = request.meta.get("_signalpost_started_at")
        self._record(request)

        if started is not None:
            self.crawler.signalpost_failure_attempt_latency_ms.append(
                (time.monotonic() - started) * 1000
            )

        self.crawler.signalpost_error_buckets[
            type(exception).__name__
        ] += 1


class SignalpostWebsiteSpider(scrapy.Spider):
    name = "signalpost_websites"

    # Conservative local limits. Verify these against the challenge's
    # official request-budget definition before a competition run.
    MAX_PAGES_PER_COMPANY = 8
    MAX_TOTAL_REQUESTS = 1800

    custom_settings = {
        "USER_AGENT": USER_AGENT,
        "ROBOTSTXT_OBEY": True,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 1,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.25,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "DOWNLOAD_TIMEOUT": 10,
        "DOWNLOAD_MAXSIZE": 2_000_000,
        "DOWNLOAD_WARNSIZE": 1_000_000,
        "REDIRECT_MAX_TIMES": 5,
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "DOWNLOADER_MIDDLEWARES": {
            "norway_company_agent.scrapy_crawler.PublicNetworkMiddleware": 50,
            "norway_company_agent.scrapy_crawler.OperationalTelemetryMiddleware": 850,
        },
    }

    def __init__(
        self,
        profiles_path: str,
        limit: int | None = None,
        organisation_numbers: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        rows = [
            json.loads(line)
            for line in Path(profiles_path).read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]

        allowed = (
            {
                item.strip()
                for item in organisation_numbers.split(",")
                if item.strip()
            }
            if organisation_numbers
            else None
        )

        filtered = [
            row
            for row in rows
            if row.get("website")
            and row.get("organisation_number") is not None
            and (
                allowed is None
                or str(row["organisation_number"]) in allowed
            )
        ]

        self.profiles = filtered[: int(limit)] if limit else filtered

        # These objects are shared across callbacks for this spider.
        self.company_pages_seen: Counter[str] = Counter()
        self.company_urls_seen: dict[str, set[str]] = {}
        self.total_requests_reserved = 0

        self.crawler.signalpost_company_completion_ms_by_org = {}

    @staticmethod
    def _canonical_url(url: str) -> str:
        """Remove fragments; retain query strings because they can be meaningful."""
        return urllib.parse.urldefrag(url.strip())[0]

    def _reserve_company_page(
        self,
        organisation_number: str,
        url: str,
    ) -> bool:
        """Reserve a unique page before scheduling its request."""
        organisation_number = str(organisation_number)
        url = self._canonical_url(url)

        seen = self.company_urls_seen.setdefault(
            organisation_number, set()
        )

        if url in seen:
            return False

        if (
            self.company_pages_seen[organisation_number]
            >= self.MAX_PAGES_PER_COMPANY
        ):
            return False

        seen.add(url)
        self.company_pages_seen[organisation_number] += 1
        return True

    def reserve_global_request(self) -> bool:
        """
        Bound actual downloader attempts, including retries and redirects.

        This is a local guard, not a substitute for the competition's
        own request accounting.
        """
        if self.total_requests_reserved >= self.MAX_TOTAL_REQUESTS:
            return False

        self.total_requests_reserved += 1
        return True

    def _initial_requests(self):
        for profile in self.profiles:
            supplied = str(profile.get("website") or "").strip()
            url = normalize_homepage(supplied)

            if not url:
                continue

            organisation_number = str(profile["organisation_number"])

            try:
                assert_public_url(url)
                company_site_key = _site_key(url)
            except ValueError as exc:
                yield error_page_event(
                    organisation_number=organisation_number,
                    requested_url=url,
                    page_kind="homepage",
                    error=f"Unsafe or invalid homepage URL: {str(exc)[:180]}",
                )
                continue

            if not self._reserve_company_page(organisation_number, url):
                yield error_page_event(
                    organisation_number=organisation_number,
                    requested_url=url,
                    page_kind="homepage",
                    error="Homepage was not scheduled because its URL was already reserved.",
                )
                continue

            yield scrapy.Request(
                url,
                callback=self.parse_homepage,
                errback=self.errback_page,
                meta={
                    "organisation_number": organisation_number,
                    "requested_url": url,
                    "page_kind": "homepage",
                    "scheme_supplied": bool(
                        re.match(r"^https?://", supplied, re.I)
                    ),
                    "_company_started_at": time.monotonic(),
                    "company_site_key": company_site_key,
                },
            )

    async def start(self):
        for request in self._initial_requests():
            yield request

    def start_requests(self):
        # Compatibility with Scrapy versions before async start().
        yield from self._initial_requests()

    def parse_homepage(self, response):
        self._record_company_completion(response.request)

        expected_site = response.meta.get("company_site_key")

        # Do not accept homepage content after a cross-domain redirect.
        if expected_site and _site_key(response.url) != expected_site:
            yield error_page_event(
                organisation_number=str(
                    response.meta["organisation_number"]
                ),
                requested_url=response.meta["requested_url"],
                page_kind="homepage",
                error=(
                    "Homepage redirected outside the registered domain; "
                    "content withheld."
                ),
            )
            return

        event = self._event(response)
        yield event

        if event.get("status") != "available":
            return

        soup = BeautifulSoup(
            response.body.decode(
                response.encoding or "utf-8",
                errors="replace",
            ),
            "lxml",
        )

        organisation_number = str(
            response.meta["organisation_number"]
        )

        # _priority_links selects relevant pages on the same host.
        # Every child must pass the shared per-company budget.
        for url in _priority_links(response.url, soup):
            try:
                assert_public_url(url)
                if _site_key(url) != expected_site:
                    continue
            except ValueError:
                continue

            if not self._reserve_company_page(
                organisation_number, url
            ):
                continue

            yield scrapy.Request(
                url,
                callback=self.parse_secondary,
                errback=self.errback_page,
                meta={
                    "organisation_number": organisation_number,
                    "requested_url": url,
                    "page_kind": "priority",
                    "scheme_supplied": True,
                    "company_site_key": expected_site,
                },
            )

    def parse_secondary(self, response):
        expected_site = response.meta.get("company_site_key")

        if expected_site and _site_key(response.url) != expected_site:
            yield error_page_event(
                organisation_number=str(
                    response.meta["organisation_number"]
                ),
                requested_url=response.meta["requested_url"],
                page_kind=response.meta["page_kind"],
                error=(
                    "Page redirected outside the registered domain; "
                    "content withheld."
                ),
            )
            return

        yield self._event(response)

    def _event(self, response):
        return extract_page_event(
            organisation_number=str(
                response.meta["organisation_number"]
            ),
            requested_url=response.meta["requested_url"],
            final_url=response.url,
            status_code=response.status,
            content_type=response.headers.get(
                b"Content-Type", b""
            ).decode("latin-1"),
            body=bytes(response.body),
            page_kind=response.meta["page_kind"],
        )

    def errback_page(self, failure):
        request = failure.request
        organisation_number = str(
            request.meta["organisation_number"]
        )

        # Try HTTP only when HTTPS was inferred rather than explicitly
        # supplied by the registry/user.
        if (
            request.meta.get("page_kind") == "homepage"
            and not request.meta.get("scheme_supplied")
            and request.url.startswith("https://")
            and not request.meta.get("http_fallback_attempted")
        ):
            fallback = (
                "http://" + request.url.removeprefix("https://")
            )

            try:
                assert_public_url(fallback)
            except ValueError:
                fallback = ""

            if (
                fallback
                and _site_key(fallback)
                == request.meta.get("company_site_key")
                and self._reserve_company_page(
                    organisation_number, fallback
                )
            ):
                yield scrapy.Request(
                    fallback,
                    callback=self.parse_homepage,
                    errback=self.errback_page,
                    dont_filter=True,
                    meta={
                        **request.meta,
                        "requested_url": fallback,
                        "http_fallback_attempted": True,
                    },
                )
                return

        if request.meta.get("page_kind") == "homepage":
            self._record_company_completion(request)

        yield error_page_event(
            organisation_number=organisation_number,
            requested_url=request.meta.get(
                "requested_url", request.url
            ),
            page_kind=request.meta.get("page_kind", "unknown"),
            error=failure.getErrorMessage(),
        )

    def _record_company_completion(self, request):
        organisation_number = str(
            request.meta["organisation_number"]
        )

        values = getattr(
            self.crawler,
            "signalpost_company_completion_ms_by_org",
            {},
        )

        if organisation_number in values:
            return

        started = request.meta.get("_company_started_at")
        if started is not None:
            values[organisation_number] = (
                time.monotonic() - started
            ) * 1000

        self.crawler.signalpost_company_completion_ms_by_org = values
