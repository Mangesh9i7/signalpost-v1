#!/usr/bin/env python3
"""Audit Signalpost output against the challenge rules as described by the project owner.

Rules checked (taken from the owner's description, not from the official challenge text):
  R1 employees   = Brreg antallAnsatte as an integer, else null (never a defaulted 0)
  R2 website     = only the registry's `hjemmeside`, else null; evidence must come from that same domain
  R3 hiring      = only the NAV job feed (orgnr-confirmed) or the verified registry website
  R4 activity    = only from the verified registry website
  R5 no search engines anywhere in the evidence URLs
  R6 contract shape: official availability vocabulary, cited evidence exists, no value on a non-available claim
Also audits the envelope `claims` (the OUTPUT_CONTRACT view): employees and official_website must be null unless supported.
Severity: VIOLATION = breaks a rule, FORMAT = wrong type/shape, WARN = coverage/quality or internal-only cosmetics.

Usage:
  python signalpost_compliance_check.py --input envelopes.jsonl --report report.json
  python signalpost_compliance_check.py --input envelopes.jsonl --write-clean clean.jsonl   # "" -> null only
Exit code is 1 when any VIOLATION or FORMAT issue is found.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

# Registered domains of search engines. Matching the whole domain (not a word inside the host) avoids flagging
# an ordinary company called e.g. "Brave AS" at brave.no.
SEARCH_DOMAINS = {
    "google.com", "google.no", "bing.com", "duckduckgo.com", "yahoo.com", "search.yahoo.com", "yandex.com", "yandex.ru",
    "baidu.com", "ecosia.org", "startpage.com", "kagi.com", "search.brave.com", "api.search.brave.com", "brave.com",
    "serpapi.com", "serper.dev",
}
ALLOWED_AVAILABILITY = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}


def host(url) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    h = (urlparse(u if "://" in u else "http://" + u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def same_site(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ra, rb = ".".join(a.split(".")[-2:]), ".".join(b.split(".")[-2:])
    return ra == rb


def walk_urls(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from walk_urls(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_urls(v)
    elif isinstance(node, str) and node.startswith(("http://", "https://")):
        yield node


def check(p: dict, claims: list | None = None, envelope_evidence: list | None = None) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    ev = p.get("evidence") or {}
    reg = (ev.get("registry") or {}).get("value") or {}

    # R1 employees
    emp = p.get("employees")
    raw = reg.get("antallAnsatte")
    har = str(reg.get("harRegistrertAntallAnsatte", "")).lower()
    if not reg:
        out.append(("WARN", "R1", "registry data missing, employees cannot be verified"))
    else:
        expected = int(str(raw).strip()) if har == "true" and str(raw).strip().isdigit() else None
        if emp is not None and (isinstance(emp, bool) or not isinstance(emp, int)):
            out.append(("FORMAT", "R1", f"employees must be an integer or null, got {emp!r}"))
        elif emp != expected:
            out.append(("VIOLATION", "R1", f"employees={emp!r} but registry gives {expected!r}"))

    # R2 website
    reg_site = str(reg.get("hjemmeside") or "").strip()
    prof_site = p.get("website")
    if prof_site == "":
        out.append(("FORMAT", "R2", 'profile.website is "" (must be null when the registry has no hjemmeside)'))
    elif prof_site:
        if not reg_site:
            out.append(("VIOLATION", "R2", f"website {prof_site!r} but the registry has no hjemmeside"))
        elif host(prof_site) != host(reg_site):
            out.append(("VIOLATION", "R2", f"website {prof_site!r} differs from registry {reg_site!r}"))
    web = ev.get("website") or {}
    wv = web.get("value") or {}
    web_ok = web.get("status") == "available" and bool((wv.get("identity_assessment") or {}).get("publishable"))
    if web.get("status") == "available":
        if not reg_site:
            out.append(("VIOLATION", "R2", "website evidence exists but the registry has no hjemmeside"))
        elif not same_site(host(wv.get("final_url") or web.get("source_url")), host(reg_site)):
            out.append(("VIOLATION", "R2", "website evidence is on a different domain than the registry website"))
        if not str(web.get("source_type") or "").startswith("registry_linked_company_website"):
            out.append(("VIOLATION", "R2", f"website source_type is {web.get('source_type')!r}"))
    for key in ("website_discovery", "website_discovered"):
        if key in ev:
            out.append(("VIOLATION", "R2", f"evidence['{key}'] exists: websites found by search or name-guessing are not allowed"))
    if str((ev.get("website") or {}).get("source_type") or "") in {"search_discovered_company_website", "discovered_company_site"}:
        out.append(("VIOLATION", "R2", "website came from a discovery path, not the registry hjemmeside"))

    # R3 hiring
    hire = ev.get("hiring") or {}
    hv = hire.get("value") or {}
    checked = [str(c).lower() for c in hv.get("checked") or []]
    for c in checked:
        if "company website" in c and not web_ok:
            out.append(("VIOLATION", "R3", "hiring checked a website that is not the verified registry website"))
        elif "company website" not in c and "nav" not in c:
            out.append(("VIOLATION", "R3", f"hiring source {c!r} is not NAV or the verified website"))
    for ad in hv.get("ads") or hv.get("postings") or []:
        ok_nav = "orgnr-confirmed" in str(ad.get("method") or "").lower()
        origin = ad.get("source_page") or ad.get("url")  # a posting may link to an ATS, but must be found on the verified page
        ok_site = web_ok and same_site(host(origin), host(reg_site))
        if not (ok_nav or ok_site):
            out.append(("VIOLATION", "R3", f"job ad {ad.get('url')!r} is not orgnr-confirmed NAV or found on the verified site"))
    if hv.get("nav_incomplete"):
        out.append(("WARN", "R3", "NAV job feed was only partly read for this company"))
    if hv.get("open_positions") and not (hv.get("ads") or hv.get("postings")):
        out.append(("WARN", "R3", "open_positions > 0 but no ads listed as evidence"))
    if hire.get("status") == "not_available" and not checked:
        out.append(("WARN", "R3", "hiring not checked (no verified website, NAV not available)"))

    # R4 public activity
    act = ev.get("public_activity") or {}
    if act.get("status") == "available":
        if not web_ok:
            out.append(("VIOLATION", "R4", "public activity present without a verified website"))
        for n in (act.get("value") or {}).get("news_pages") or []:
            if not same_site(host(n.get("url")), host(reg_site)):
                out.append(("VIOLATION", "R4", f"activity page {n.get('url')!r} is off the verified domain"))

    # envelope claims (contract view)
    if claims is not None:
        cl = {c.get("field"): c for c in claims}
        ec = cl.get("employees")
        if ec is not None and reg:
            exp = int(str(raw).strip()) if har == "true" and str(raw).strip().isdigit() else None
            if ec.get("value") != exp:
                out.append(("VIOLATION", "R1", f"claim employees={ec.get('value')!r} but registry gives {exp!r}"))
        wc = cl.get("official_website")
        if wc is not None and wc.get("value") not in (None, ""):
            if not reg_site:
                out.append(("VIOLATION", "R2", "claim official_website set but registry has no hjemmeside"))
            elif not (web_ok and same_site(host(wc.get("value")), host(reg_site))):
                out.append(("VIOLATION", "R2", f"claim official_website {wc.get('value')!r} is not the verified registry site"))

    # R5 search engines
    for u in walk_urls(p):
        h = host(u)
        if any(h == d or h.endswith("." + d) for d in SEARCH_DOMAINS) and not h.startswith(("maps.", "play.", "fonts.")):
            out.append(("VIOLATION", "R5", f"search engine URL in evidence: {u}"))
            break

    # R6 contract shape: official availability vocabulary, evidence that exists, nothing "available" without a source
    if claims is not None:
        ids = {e.get("id") for e in (envelope_evidence or [])}
        for c in claims:
            if c.get("availability") not in ALLOWED_AVAILABILITY:
                out.append(("FORMAT", "R6", f"claim {c.get('field')} has availability {c.get('availability')!r}"))
            if c.get("availability") == "available" and not c.get("evidence_ids"):
                out.append(("VIOLATION", "R6", f"claim {c.get('field')} is available but cites no evidence"))
            if envelope_evidence is not None and any(i not in ids for i in c.get("evidence_ids") or []):
                out.append(("FORMAT", "R6", f"claim {c.get('field')} cites evidence that is not in the envelope"))
            if c.get("availability") != "available" and c.get("value") not in (None, [], {}):
                out.append(("VIOLATION", "R6", f"claim {c.get('field')} carries a value although it is {c.get('availability')}"))
        for e in envelope_evidence or []:
            if not e.get("retrieved_at"):
                out.append(("WARN", "R6", "evidence record without retrieved_at"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--report")
    ap.add_argument("--write-clean", help='write a copy where "" website/employees become null (nothing else changes)')
    a = ap.parse_args()

    objs = [json.loads(l) for l in Path(a.input).read_text(encoding="utf-8").splitlines() if l.strip()]
    findings: dict[str, list] = {}
    for o in objs:
        p = o.get("profile") or o
        key = f"{p.get('name')} ({p.get('organisation_number')})"
        if key in findings:  # duplicate organisation in the input: never let one row hide another
            key += f" #{len(findings) + 1}"
        findings[key] = check(p, o.get("claims") if "profile" in o else None, o.get("evidence") if "profile" in o else None)

    count = Counter((s, r) for fs in findings.values() for s, r, _ in fs)
    hard = [(n, f) for n, fs in findings.items() for f in fs if f[0] in ("VIOLATION", "FORMAT")]
    print(f"Checked {len(objs)} companies\n")
    for n, (s, r, m) in hard:
        print(f"[{s}] {r} {n}: {m}")
    if hard:
        print()
    by_msg = defaultdict(list)
    for n, fs in findings.items():
        for s, r, m in fs:
            if s == "WARN":
                by_msg[(r, m)].append(n)
    for (r, m), names in by_msg.items():
        print(f"[WARN] {r} {len(names)} companies: {m}")
    if a.report:
        rep = json.loads(Path(a.report).read_text(encoding="utf-8"))
        nav, val = rep.get("nav_job_feed") or {}, rep.get("validation") or {}
        if not val.get("passed", True):
            print("[VIOLATION] report: validation did not pass")
            count[("VIOLATION", "report")] += 1
        if not nav.get("enabled"):
            print("[WARN] R3 NAV job feed is disabled, so hiring coverage is limited to verified websites")
        elif not nav.get("ads_indexed"):
            print(f"[WARN] R3 NAV enabled but ads_indexed=0 (pages_read={nav.get('pages_read')}, error={nav.get('error')})")
    print("\nSummary:", {f"{s}:{r}": c for (s, r), c in sorted(count.items())} or "no findings")

    if a.write_clean:
        with open(a.write_clean, "w", encoding="utf-8") as fh:
            for o in objs:
                p = o.get("profile") or o
                for k in ("website", "employees"):
                    if p.get(k) == "":
                        p[k] = None
                fh.write(json.dumps(o, ensure_ascii=False) + "\n")
        print(f"Wrote {a.write_clean}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())