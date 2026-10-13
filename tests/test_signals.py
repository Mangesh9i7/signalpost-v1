from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for _key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs4 import BeautifulSoup  # noqa: E402

from norway_company_agent import nav_jobs, website  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.signals import extract_page_signals, is_careers_url, parse_date, parse_feed  # noqa: E402
from norway_company_agent.site_activity import extract_hiring, extract_public_activity  # noqa: E402

TODAY = date(2026, 10, 8)

HOME = """<html><head><title>Acme AS</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>
<h1>Acme AS</h1><p>Org.nr 923 609 016. We build scheduling software for clinics across Norway and provide support.</p>
<a href="/om-oss">Om oss</a> <a href="/kontakt">Kontakt</a> <a href="/karriere">Karriere</a>
<a href="/aktuelt">Aktuelt</a> <a href="/utstilling">Utstilling</a></body></html>"""
CAREERS = """<html><head><title>Karriere - Acme AS</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Backend-utvikler",
"datePosted":"2026-09-20","validThrough":"2026-12-31","hiringOrganization":{"@type":"Organization","name":"Acme AS"},
"jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"Oslo"}}}</script></head><body>
<a href="/karriere/produktsjef">Produktsjef</a> <a href="/karriere/apply">Apply</a> <a href="/karriere/produktsjef">Produktsjef</a>
<a href="https://acme.jobylon.com/jobs/">Se alle</a> <a href="/karriere/les-mer">Les mer</a></body></html>"""
NEWS = """<html><head><title>Aktuelt - Acme AS</title></head><body>
<article><time datetime="2026-09-15">15. september 2026</time><h3><a href="/aktuelt/ny-kunde">Ny kunde i Bergen</a></h3></article>
<article><time datetime="2024-02-01">1.2.2024</time><h3><a href="/aktuelt/gammel">Gammel nyhet fra 2024</a></h3></article>
<article><time datetime="2031-01-01">future</time><h3><a href="/aktuelt/fremtid">Fremtidig element</a></h3></article></body></html>"""
RSS = """<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><item><title>Lansering &x; av ny versjon</title><link>/aktuelt/lansering</link>
<pubDate>Tue, 30 Sep 2025 08:00:00 +0200</pubDate></item><item><title>Uten dato</title><link>/x</link></item></channel></rss>"""
PAGES = {"/": HOME, "/karriere": CAREERS, "/aktuelt": NEWS, "/om-oss": "<html><title>Om oss</title></html>",
         "/kontakt": "<html><title>Kontakt</title></html>", "/feed.xml": RSS}


class SiteHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        body = PAGES.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml" if self.path.endswith("xml") else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class SignalParsingTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(parse_date("2026-09-15T10:00:00+02:00", TODAY), "2026-09-15")
        self.assertEqual(parse_date("15.09.2026", TODAY), "2026-09-15")
        self.assertEqual(parse_date("15. september 2026", TODAY), "2026-09-15")
        self.assertEqual(parse_date("Tue, 30 Sep 2025 08:00:00 +0200", TODAY), "2025-09-30")
        self.assertIsNone(parse_date("2031-01-01", TODAY))  # future
        self.assertIsNone(parse_date("1999-05-05", TODAY))  # implausibly old
        self.assertIsNone(parse_date("soon", TODAY))

    def test_careers_url_does_not_match_exhibition(self):
        self.assertTrue(is_careers_url("https://x.no/karriere"))
        self.assertTrue(is_careers_url("https://x.no/ledige-stillinger"))
        self.assertFalse(is_careers_url("https://x.no/utstilling"))

    def test_page_signals(self):
        soup = BeautifulSoup(CAREERS, "lxml")
        signals = extract_page_signals(CAREERS, soup, "https://acme.no/karriere", today=TODAY)
        titles = {p["title"] for p in signals["job_postings"]}
        self.assertEqual(titles, {"Backend-utvikler", "Produktsjef"})  # apply/les-mer excluded, duplicates merged
        json_ld = next(p for p in signals["job_postings"] if p["method"] == "json-ld JobPosting")
        self.assertEqual((json_ld["date_posted"], json_ld["location"]), ("2026-09-20", "Oslo"))
        self.assertEqual(signals["ats_links"], ["https://acme.jobylon.com/jobs/"])

    def test_news_items_need_title_and_valid_date(self):
        signals = extract_page_signals(NEWS, BeautifulSoup(NEWS, "lxml"), "https://acme.no/aktuelt", today=TODAY)
        self.assertEqual([i["date"] for i in signals["dated_items"]], ["2026-09-15", "2024-02-01"])  # future item rejected

    def test_no_openings_statement(self):
        html = "<html><title>Karriere</title><body>Vi har for tiden ingen ledige stillinger.</body></html>"
        signals = extract_page_signals(html, BeautifulSoup(html, "lxml"), "https://acme.no/karriere", today=TODAY)
        self.assertEqual(signals["job_postings"], [])
        self.assertIn("ingen ledige", signals["no_openings_statement"].casefold())

    def test_feed_parser_ignores_entities_and_undated_items(self):
        items = parse_feed(RSS.encode(), "https://acme.no/feed.xml", TODAY)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date"], "2025-09-30")
        self.assertNotIn("root:", items[0]["title"])  # /etc/passwd was not expanded

    def test_priority_links_reserve_slots_for_careers_and_news(self):
        anchors = "".join(f'<a href="/{n}">{n}</a>' for n in ("om-oss", "kontakt", "ledelse", "team", "locations", "karriere", "aktuelt"))
        links = website._priority_links("https://acme.no/", BeautifulSoup(anchors, "lxml"))
        self.assertIn("https://acme.no/karriere", links)
        self.assertIn("https://acme.no/aktuelt", links)


class EndToEndSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), SiteHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _profile(self):
        profile = {"organisation_number": "923609016", "name": "ACME AS", "website": self.url, "evidence": {}}
        with patch.object(website, "assert_public_url", lambda url: None):
            record, metrics = website.fetch_website(self.url)
        profile["evidence"]["website"] = apply_website_identity_gate(profile, record)["website"]
        return profile, record, metrics

    def test_crawl_collects_signals_and_extractors_use_them(self):
        profile, record, metrics = self._profile()
        self.assertEqual(record["status"], "available")
        self.assertTrue(profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"])
        hiring = extract_hiring(profile)
        self.assertEqual(hiring["status"], "available")
        self.assertEqual(hiring["value"]["open_positions"], 2)
        self.assertEqual(hiring["as_of"], "2026-09-20")
        activity = extract_public_activity(profile)
        self.assertEqual(activity["status"], "available")
        dates = {item["date"] for item in activity["value"]["items"]}
        self.assertIn("2026-09-15", dates)   # time tag on the news page
        self.assertIn("2025-09-30", dates)   # RSS feed item
        self.assertNotIn("2031-01-01", dates)
        self.assertEqual(activity["as_of"], activity["value"]["latest_date"])

    def test_unverified_site_is_never_used(self):
        profile, _, _ = self._profile()
        profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"] = False
        self.assertEqual(extract_hiring(profile)["status"], "not_available")
        self.assertIn("Not checked", extract_hiring(profile)["note"])
        self.assertEqual(extract_public_activity(profile)["status"], "not_available")


NAV_ADS = {
    "a1": {"name": "Acme AS", "orgnr": "923609016", "title": "Sykepleier", "status": "ACTIVE"},
    "a2": {"name": "ACME AS", "orgnr": "111111111", "title": "Annen Acme-jobb", "status": "ACTIVE"},   # same name, other company
    "a3": {"name": "Acme AS", "orgnr": "923609016", "title": "Gammel stilling", "status": "INACTIVE"},
}


class NavHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send_response(401)
            self.end_headers()
            return
        port = self.server.server_port
        if self.path.startswith("/api/v1/feed"):
            first = "page2" not in self.path
            keys = ["a1", "a2"] if first else ["a3"]
            body = {"items": [{"id": k, "url": f"/api/v1/ads/{k}", "title": NAV_ADS[k]["title"],
                               "_feed_entry": {"uuid": k, "status": NAV_ADS[k]["status"], "businessName": NAV_ADS[k]["name"]}} for k in keys],
                    "next_url": f"http://127.0.0.1:{port}/api/v1/feed?page2=1" if first else None}
        else:
            ad = NAV_ADS[self.path.rsplit("/", 1)[-1]]
            body = {"json": {"title": ad["title"], "published": "2026-10-01T00:00:00", "expires": "2026-12-01T00:00:00",
                             "employer": {"name": ad["name"], "orgnr": ad["orgnr"]}, "link": f"https://arbeidsplassen.nav.no/stillinger/stilling/{self.path[-2:]}"}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class NavFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), NavHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.env = patch.dict(os.environ, {"SIGNALPOST_NAV_TOKEN": "test-token", "SIGNALPOST_NAV_BASE_URL": f"http://127.0.0.1:{self.server.server_port}"})
        self.env.start()
        nav_jobs.reset()

    def tearDown(self):
        self.env.stop()
        nav_jobs.reset()

    def test_only_orgnr_confirmed_active_ads_are_returned(self):
        profile = {"organisation_number": "923609016", "name": "Acme AS", "evidence": {}}
        record = extract_hiring(profile)
        self.assertEqual(record["status"], "available")
        self.assertEqual([p["title"] for p in record["value"]["postings"]], ["Sykepleier"])  # not the other company's ad, not the inactive one
        self.assertEqual(record["source_class"], "official_job_feed")

    def test_wrong_company_with_same_name_gets_nothing(self):
        profile = {"organisation_number": "999999999", "name": "Acme AS", "evidence": {}}
        self.assertEqual(extract_hiring(profile)["status"], "not_available")

    def test_bad_token_fails_closed(self):
        with patch.dict(os.environ, {"SIGNALPOST_NAV_TOKEN": "wrong"}):
            nav_jobs.reset()
            record = extract_hiring({"organisation_number": "923609016", "name": "Acme AS", "evidence": {}})
        self.assertEqual(record["status"], "not_available")

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"SIGNALPOST_NAV_TOKEN": "", "SIGNALPOST_NAV_PUBLIC_TOKEN": ""}):
            self.assertFalse(nav_jobs.enabled())


if __name__ == "__main__":
    unittest.main()
