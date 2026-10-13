"""Regression tests for the audit fixes (rules of the Builderr Signalpost challenge + correctness bugs).

No internet and no API key are needed: local HTTP servers and fakes stand in for the outside world.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for _key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent import llm_synthesis, nav_jobs, website  # noqa: E402
from norway_company_agent.batch import (  # noqa: E402
    apply_live_registry,
    evidence_terminal_state,
    profile_complete_for_modules,
    read_organisation_inputs,
    terminal_envelope,
    validate_envelopes,
)
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.identity import _contains_org_number  # noqa: E402
from norway_company_agent.official import fetch_official_modules, normalize_entity  # noqa: E402
from norway_company_agent.sampling import normalize_row  # noqa: E402
from norway_company_agent.site_activity import extract_hiring  # noqa: E402

TODAY = date.today()


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_competition_batch", ROOT / "scripts" / "run_competition_batch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Handler(BaseHTTPRequestHandler):
    """Tiny site: robots.txt behaviour and a redirect are chosen per test through class attributes."""

    robots_status = 404
    robots_body = ""
    redirect_to: str | None = None

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            self.send_response(Handler.robots_status)
            self.send_header("Content-Type", "text/plain")
            raw = Handler.robots_body.encode()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if Handler.redirect_to and not str(self.headers.get("Host", "")).startswith("localhost"):  # redirect once, then serve
            self.send_response(302)
            self.send_header("Location", Handler.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = b"<html><head><title>Acme AS</title></head><body><h1>Acme AS</h1><p>Org.nr 923 609 016. Scheduling software.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class CrawlerRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        cls.other = f"http://localhost:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        website._ROBOTS_CACHE.clear()
        Handler.robots_status, Handler.robots_body, Handler.redirect_to = 404, "", None
        self.patch = patch.object(website, "assert_public_url", lambda url: None)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        website._ROBOTS_CACHE.clear()

    def test_missing_robots_file_allows_crawling(self):
        self.assertEqual(website.fetch_website(self.url)[0]["status"], "available")

    def test_explicit_disallow_is_respected(self):
        Handler.robots_status, Handler.robots_body = 200, "User-agent: *\nDisallow: /\n"
        record = website.fetch_website(self.url)[0]
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(evidence_terminal_state(record), "blocked_robots")

    def test_unreadable_robots_file_is_treated_as_disallow(self):
        Handler.robots_status = 503  # RFC 9309: a server error means we may not assume permission
        self.assertEqual(website.fetch_website(self.url)[0]["status"], "blocked")

    def test_redirect_to_another_domain_withholds_the_page(self):
        Handler.redirect_to = self.other
        record = website.fetch_website(self.url)[0]
        self.assertEqual(record["status"], "ambiguous")
        self.assertIsNone(record.get("value"))
        self.assertEqual(evidence_terminal_state(record), "ambiguous")

    def test_homepage_field_is_cleaned(self):
        self.assertEqual(website.normalize_homepage("www.acme.no, www.other.no"), "https://www.acme.no/")
        self.assertEqual(website.normalize_homepage("https://user:pw@acme.no/x?y=1#z"), "https://acme.no/x")
        self.assertIsNone(website.normalize_homepage("javascript:alert(1)"))
        self.assertIsNone(website.normalize_homepage("mailto:post@acme.no"))
        self.assertIsNone(website.normalize_homepage(""))

    def test_private_addresses_are_still_blocked(self):
        self.patch.stop()
        try:
            self.assertEqual(website.fetch_website("http://127.0.0.1:9/")[0]["status"], "blocked")
        finally:
            self.patch.start()


class IdentityTests(unittest.TestCase):
    def test_org_number_must_be_a_token_not_scattered_digits(self):
        org = "923609016"
        for text in ("Org.nr 923 609 016", "NO923609016MVA", "923.609.016"):
            self.assertTrue(_contains_org_number(text, org), text)
        for text in ("tlf 99 92 36 09 016", "pris 1923609016", "tel 92360 9016", "9236090161"):
            self.assertFalse(_contains_org_number(text, org), text)


def _verified_profile(postings, name="ACME AS"):
    page = {"url": "https://acme.no/karriere", "content_sha256": "b" * 64, "signals": {"page_url": "https://acme.no/karriere", "careers_page": True, "job_postings": postings}}
    site = evidence("website", "available", "registry_linked_company_website", "https://acme.no/", content_sha256="a" * 64, value={
        "final_url": "https://acme.no/", "content_sha256": "a" * 64, "identity_assessment": {"publishable": True},
        "signals": {"page_url": "https://acme.no/"}, "pages": [page]})
    return {"organisation_number": "923609016", "name": name, "evidence": {"website": site}}


class HiringRuleTests(unittest.TestCase):
    def posting(self, title, valid_through=None, organisation=None):
        return {"title": title, "url": f"https://acme.no/karriere/{title}", "date_posted": None, "valid_through": valid_through,
                "location": None, "hiring_organisation": organisation, "method": "json-ld JobPosting"}

    def test_expired_postings_are_not_reported_as_open(self):
        old = (TODAY - timedelta(days=30)).isoformat()
        record = extract_hiring(_verified_profile([self.posting("gammel", old)]))
        self.assertEqual(record["status"], "not_available")
        self.assertIn("expired", record["note"])

    def test_open_posting_is_kept_and_expired_one_dropped(self):
        old = (TODAY - timedelta(days=1)).isoformat()
        future = (TODAY + timedelta(days=30)).isoformat()
        record = extract_hiring(_verified_profile([self.posting("ny", future), self.posting("gammel", old)]))
        self.assertEqual(record["status"], "available")
        self.assertEqual([p["title"] for p in record["value"]["postings"]], ["ny"])

    def test_posting_for_another_employer_is_withheld(self):
        record = extract_hiring(_verified_profile([self.posting("kundejobb", organisation="Zeta Consulting")]))
        self.assertEqual(record["status"], "not_available")
        self.assertIn("other organisation", record["note"])

    def test_posting_naming_the_company_is_kept(self):
        record = extract_hiring(_verified_profile([self.posting("egen", organisation="Acme")]))
        self.assertEqual(record["status"], "available")


class NavTests(unittest.TestCase):
    def test_employer_orgnr_is_preferred_over_other_orgnr_fields(self):
        detail = {"json": {"contact": {"orgnr": "111111111"}, "employer": {"name": "Acme", "orgnr": "923609016"}}}
        self.assertEqual(nav_jobs._employer_orgnr(detail), "923609016")

    def test_employer_without_orgnr_is_unconfirmed(self):
        detail = {"json": {"contact": {"orgnr": "111111111"}, "employer": {"name": "Acme"}}}
        self.assertIsNone(nav_jobs._employer_orgnr(detail))


class RegistryTests(unittest.TestCase):
    ROW = {"organisasjonsnummer": "923609016", "navn": "ACME AS", "organisasjonsform.kode": "AS",
           "harRegistrertAntallAnsatte": "false", "antallAnsatte": "0", "hjemmeside": ""}

    def test_not_registered_headcount_is_null_not_zero(self):
        record = normalize_row(dict(self.ROW))
        self.assertIsNone(record["employees"])
        self.assertIsNone(record["website"])

    def test_registered_zero_is_kept(self):
        record = normalize_row({**self.ROW, "harRegistrertAntallAnsatte": "true"})
        self.assertEqual(record["employees"], 0)

    def test_live_entity_with_unregistered_headcount_is_null(self):
        self.assertIsNone(normalize_entity({"organisasjonsnummer": "1", "antallAnsatte": 0, "harRegistrertAntallAnsatte": False})["employees"])

    def test_profile_is_built_from_live_record_without_bulk(self):
        body = {"organisasjonsnummer": "923609016", "navn": "ACME AS", "organisasjonsform": {"kode": "AS"}, "antallAnsatte": 7,
                "harRegistrertAntallAnsatte": True, "hjemmeside": "www.acme.no", "epostadresse": "x@acme.no", "telefon": "12345678",
                "forretningsadresse": {"kommune": "OSLO", "kommunenummer": "0301"},
                "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmeringstjenester"}}

        def fetcher(url):
            from norway_company_agent.http import FetchResult
            return FetchResult(url, 200, 1, 10, body=body, content_sha256="c" * 64, retrieved_at="2026-10-12T00:00:00Z")

        records, _ = fetch_official_modules("923609016", {"registry_live"}, fetcher=fetcher, keep_raw=True)
        profile = {"organisation_number": "923609016", "evidence": {
            "registry": evidence("registry", "not_fetched", "x", "urn:x"), "accounting_obligation": evidence("accounting_obligation", "not_fetched", "x", "urn:x"),
            **records}}
        apply_live_registry(profile)
        self.assertEqual((profile["name"], profile["employees"], profile["website"], profile["municipality"], profile["industry_code"]),
                         ("ACME AS", 7, "www.acme.no", "OSLO", "62.010"))
        registry = profile["evidence"]["registry"]
        self.assertEqual(registry["status"], "available")
        self.assertEqual(registry["value"]["antallAnsatte"], 7)
        self.assertNotIn("epostadresse", registry["value"])  # contact details are not kept
        self.assertNotIn("telefon", registry["value"])
        self.assertNotIn("raw_body", profile["evidence"]["registry_live"])

    def test_unknown_company_stays_a_terminal_not_found(self):
        def fetcher(url):
            from norway_company_agent.http import FetchResult
            return FetchResult(url, 404, 1, 0, error="HTTP 404", retrieved_at="2026-10-12T00:00:00Z")

        records, _ = fetch_official_modules("999999999", {"registry_live"}, fetcher=fetcher, keep_raw=True)
        profile = {"organisation_number": "999999999", "evidence": {"registry": evidence("registry", "not_fetched", "x", "urn:x"), **records}}
        apply_live_registry(profile)
        self.assertEqual(profile["evidence"]["registry"]["status"], "not_found")


class InputTests(unittest.TestCase):
    def test_lenient_reading_keeps_every_row_and_collapses_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.txt"
            path.write_text("923609016\nnot-a-number\n923 609 016\n\n12345\n", encoding="utf-8")
            records = read_organisation_inputs(path, strict=False)
            self.assertEqual([r["organisation_number"] for r in records], ["923609016", "not-a-number", "12345"])
            self.assertEqual([bool(r.get("invalid_input")) for r in records], [False, True, True])
            with self.assertRaises(ValueError):
                read_organisation_inputs(path)  # the strict default still refuses bad input

    def test_budget_exhausted_is_terminal_and_resumable(self):
        record = {"status": "budget_exhausted"}
        self.assertEqual(evidence_terminal_state(record), "budget_exhausted")
        self.assertFalse(profile_complete_for_modules({"evidence": {"website": record}}, ["website"]))


class ContractTests(unittest.TestCase):
    def test_contract_warnings_do_not_fail_a_complete_run(self):
        profile = {"organisation_number": "923609016", "name": "ACME AS", "evidence": {"registry": evidence("registry", "available", "o", "https://x.test")}}
        envelope = terminal_envelope(profile, run_id="t", modules=["registry"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
        envelope["claims"].append({"field": "bogus", "availability": "maybe", "value": 1, "evidence_ids": ["ev-missing"]})
        report = validate_envelopes([envelope], 1)
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["contract_warning_count"], 2)


class LlmRuleTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("SIGNALPOST_LLM_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "SIGNALPOST_LLM_BASE_URL", "SIGNALPOST_LLM_DISABLE", "SIGNALPOST_LLM_MODEL"):
            os.environ.pop(key, None)
        llm_synthesis.reset_state()

    def tearDown(self):
        self.env.stop()
        llm_synthesis.reset_state()

    def test_personal_provider_keys_are_never_picked_up(self):
        os.environ["OPENAI_API_KEY"] = "sk-personal-0123456789abcdef"
        os.environ["GROQ_API_KEY"] = "gsk_personal_0123456789abcdef"
        self.assertEqual(llm_synthesis._resolve_config(), (None, "no-key"))

    def test_only_the_declared_variable_is_used_and_defaults_are_neutral(self):
        os.environ["SIGNALPOST_LLM_API_KEY"] = "Zk3-test-credential-9f8a7b6c"
        config, _ = llm_synthesis._resolve_config()
        self.assertEqual((config["base_url"], config["model"], config["key_source"]),
                         ("https://api.openai.com/v1", "gpt-4o-mini", "SIGNALPOST_LLM_API_KEY"))

    def test_key_is_never_sent_over_plain_http_to_a_remote_host(self):
        os.environ["SIGNALPOST_LLM_API_KEY"] = "Zk3-test-credential-9f8a7b6c"
        os.environ["SIGNALPOST_LLM_BASE_URL"] = "http://llm.example.com/v1"
        self.assertEqual(llm_synthesis._resolve_config(), (None, "unsafe-base-url"))

    def test_summary_with_invented_names_or_urls_is_rejected(self):
        facts = [{"id": "F1", "text": "ACME AS (organisation number 923609016) is a private limited company registered in Oslo."}]
        self.assertIsNone(llm_synthesis._validate_summary("ACME AS is a private limited company in Oslo [F1].", facts))
        for bad in ("ACME AS delivers Kubernetes tooling to Equinor [F1].", "ACME AS is a company in Oslo, see https://acme-evil.example [F1]."):
            self.assertEqual(llm_synthesis._validate_summary(bad, facts), "term-not-in-facts", bad)

    def test_cost_stats_exist_and_hold_no_secret(self):
        stats = llm_synthesis.get_cost_stats()
        self.assertIn("estimated_cost_usd", stats)
        self.assertNotIn("key", stats)


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_secrets_or_banned_discovery_code_ship_with_the_project(self):
        self.assertFalse((ROOT / ".env").exists(), ".env must never be part of the project")
        self.assertIn(".env", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        for banned in ("src/norway_company_agent/discovery.py", "src/norway_company_agent/website_discovery.py", "scripts/run_brave_discovery.py"):
            self.assertFalse((ROOT / banned).exists(), banned)

    def test_no_search_engine_endpoint_is_called_by_the_code(self):
        pattern = re.compile(r"api\.search\.brave\.com|google\.com/search|bing\.com/search|duckduckgo\.com/\?|serpapi|googleapis\.com/customsearch", re.I)
        offenders = [str(p.relative_to(ROOT)) for folder in ("src", "scripts") for p in (ROOT / folder).rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_no_literal_api_keys_in_the_source_tree(self):
        pattern = re.compile(r"AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9]{32,}|gsk_[A-Za-z0-9]{30,}|AQ\.[A-Za-z0-9_-]{30,}")
        offenders = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".json", ".jsonl", ".toml", ".example", ".txt"}
                     and ".venv" not in p.parts and p.name != "uv.lock" and pattern.search(p.read_text(encoding="utf-8", errors="ignore"))]
        self.assertEqual(offenders, [])


class RunnerTests(unittest.TestCase):
    """The batch runner with the network replaced by fakes."""

    @classmethod
    def setUpClass(cls):
        cls.run_mod = _load_runner()

    def setUp(self):
        self.env = patch.dict(os.environ, {"SIGNALPOST_LLM_DISABLE": "1"})
        self.env.start()
        nav_jobs.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_official(self, org, modules, fetcher=None, *, keep_raw=False):
        body = {"organisasjonsnummer": org, "navn": f"FIRMA {org} AS", "organisasjonsform": {"kode": "AS"}, "antallAnsatte": 3,
                "harRegistrertAntallAnsatte": True, "forretningsadresse": {"kommune": "OSLO", "kommunenummer": "0301"},
                "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmeringstjenester"}}
        records = {}
        if "registry_live" in modules:
            records["registry_live"] = evidence("registry_live", "available", "official_registry_live", f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                                                value=normalize_entity(body), content_sha256="d" * 64)
            if keep_raw:
                records["registry_live"]["raw_body"] = body
        return records, []

    def fake_website(self, url, **kwargs):
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}

    def run_batch(self, lines, *extra):
        batch = self.dir / "batch.txt"
        batch.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = self.dir / "out" / "envelopes.jsonl"
        with patch.object(self.run_mod, "fetch_official_modules", self.fake_official), patch.object(self.run_mod, "fetch_website", self.fake_website):
            code = self.run_mod.main(["--organisations", str(batch), "--output", str(out), "--workers", "2", *extra])
        envelopes = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        report = json.loads((self.dir / "out" / "report.json").read_text(encoding="utf-8"))
        return code, envelopes, report

    def test_one_envelope_per_company_without_a_bulk_file(self):
        code, envelopes, report = self.run_batch(["923609016", "923609018"])
        self.assertEqual(code, 0)
        self.assertEqual([e["organisation_number"] for e in envelopes], ["923609016", "923609018"])
        first = envelopes[0]
        claims = {c["field"]: c for c in first["claims"]}
        self.assertEqual(claims["legal_name"]["value"], "FIRMA 923609016 AS")
        self.assertEqual(claims["employees"]["value"], 3)
        self.assertEqual(claims["official_website"]["availability"], "not_available")
        self.assertIsNone(claims["official_website"]["value"])
        self.assertEqual(report["registry"]["registry_source"], "live_api")
        self.assertEqual(report["validation"]["checks"]["exact_expected_count"], True)
        self.assertIn("llm", report)

    def test_bad_lines_and_duplicates_never_abort_the_batch(self):
        code, envelopes, report = self.run_batch(["923609016", "oops", "923609016", "923609018"])
        self.assertEqual(code, 0)
        self.assertEqual([e["organisation_number"] for e in envelopes], ["923609016", "oops", "923609018"])
        bad = envelopes[1]
        self.assertEqual(bad["state"], "complete")
        self.assertEqual(bad["modules"]["registry"]["state"], "failed")
        self.assertEqual({c["availability"] for c in bad["claims"]} - {"failed", "not_available", "not_applicable"}, set())
        self.assertEqual(report["invalid_inputs"], ["oops"])

    def test_wrong_expected_count_is_reported_but_every_company_still_gets_an_envelope(self):
        code, envelopes, report = self.run_batch(["923609016", "923609018"], "--expected-count", "5")
        self.assertEqual(len(envelopes), 2)
        self.assertEqual(code, 1)
        self.assertFalse(report["validation"]["checks"]["exact_expected_count"])

    def test_exhausted_time_budget_still_emits_terminal_envelopes(self):
        code, envelopes, report = self.run_batch(["923609016", "923609018", "923609020"], "--time-budget-s", "0.000001")
        self.assertEqual(code, 0)
        self.assertEqual(len(envelopes), 3)
        for envelope in envelopes:
            self.assertEqual(envelope["modules"]["roles"]["state"], "budget_exhausted")
            self.assertEqual(envelope["modules"]["registry"]["state"], "budget_exhausted")
        self.assertEqual(report["companies_with_budget_exhausted_modules"], 3)

    def test_unreadable_bulk_file_falls_back_to_the_live_registry(self):
        bulk = self.dir / "bulk.csv"
        bulk.write_bytes(b"\x00\x01 not a csv at all")
        code, envelopes, report = self.run_batch(["923609016"], "--bulk", str(bulk))
        self.assertEqual(code, 0)
        self.assertEqual(len(envelopes), 1)
        claims = {c["field"]: c for c in envelopes[0]["claims"]}
        self.assertEqual(claims["legal_name"]["availability"], "available")  # the live record names the company anyway


if __name__ == "__main__":
    unittest.main()
