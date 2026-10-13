#!/usr/bin/env python3
"""Build a static, responsive Signalpost viewer from envelopes.jsonl or profiles.jsonl.

Matches the layout and design language of https://builderr.ai/signalpost:
  - sticky top bar with brand, global search, sample meta
  - run-proof stats strip
  - three-column shell: company directory | detail panel | Ask Signalpost
  - five evidence pills: Latest annual filing, Verified website,
    verified profiles, company updates found, open roles found
  - tabbed detail sections, financial snapshot, leadership cards,
    locations grid, hiring & activity, sources

Usage:
  python build_signalpost_viewer.py --input smoke-envelopes.jsonl \
      --report smoke-report.json --output viewer.html

Standard library only, no network, one self-contained HTML file.
Every value shown comes from the input data; missing evidence is shown as missing.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from pathlib import Path

NBSP = "\u00a0"

# Five evidence areas used by the live site pills / coverage score
FIVE = [
    ("filing", "Latest annual filing"),
    ("website", "Verified website"),
    ("profiles", "verified profiles"),
    ("updates", "company updates found"),
    ("roles", "open roles found"),
]


# ---------- helpers ----------
def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def safe_url(u) -> str:
    u = str(u or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    return u if re.match(r"^https?://", u, re.I) else ""


def link(url, label=None, cls="") -> str:
    s = safe_url(url)
    if not s:
        return esc(label or url)
    c = f' class="{esc(cls)}"' if cls else ""
    return f'<a href="{esc(s)}" target="_blank" rel="noopener noreferrer"{c}>{esc(label or s)}</a>'


def cite(text) -> str:
    return re.sub(r"\[([A-Z]\d+)\]", r'<sup class="cite">\1</sup>', esc(text))


def fmt_date(s) -> str:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return str(s or "")


def money(v, compact=False) -> str:
    try:
        n = float(v)
        if compact and abs(n) >= 1_000_000:
            return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
        return f"{n:,.0f}".replace(",", NBSP)
    except (ValueError, TypeError):
        return "—"


def person(r: dict) -> str:
    n = r.get("name")
    if isinstance(n, list):
        n = n[0] if n else ""
    return str(n or "—")


def ev_of(p: dict) -> dict:
    return p.get("evidence") or {}


def val(ev: dict, key: str) -> dict:
    return (ev.get(key) or {}).get("value") or {}


def fin_records(p: dict) -> list:
    recs = val(ev_of(p), "financials").get("records") or []
    return sorted(
        recs,
        key=lambda r: (r.get("period") or {}).get("tilDato") or r.get("year") or "",
        reverse=True,
    )


def active_roles(p: dict) -> list:
    seen, out = set(), []
    for r in val(ev_of(p), "roles").get("roles") or []:
        if r.get("inactive"):
            continue
        k = (r.get("role"), person(r))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def locations(p: dict) -> list:
    return val(ev_of(p), "locations").get("locations") or []


def addr(a: dict | None) -> str:
    a = a or {}
    street = ", ".join(a.get("adresse") or [])
    town = " ".join(
        x for x in [a.get("postnummer"), a.get("poststed") or a.get("kommune")] if x
    )
    return ", ".join(x for x in [street, town] if x) or "—"


def hiring_ads(p: dict) -> list:
    v = val(ev_of(p), "hiring")
    for key in ("ads", "postings", "jobs", "job_postings"):
        if isinstance(v.get(key), list) and v[key]:
            return [a for a in v[key] if isinstance(a, dict)]
    return []


def ad_meta(a: dict) -> str:
    bits = [
        a.get("location"),
        f"Posted {fmt_date(a['date_posted'])}" if a.get("date_posted") else None,
        f"Valid through {fmt_date(a['valid_through'])}" if a.get("valid_through") else None,
        a.get("method"),
    ]
    return " · ".join(str(b) for b in bits if b)


def website_ok(p: dict) -> bool:
    w = ev_of(p).get("website") or {}
    ident = (w.get("value") or {}).get("identity_assessment") or {}
    return w.get("status") == "available" and bool(ident.get("publishable"))


def news_items(p: dict) -> list:
    act = ev_of(p).get("public_activity") or {}
    return (act.get("value") or {}).get("news_pages") or []


def coverage_five(p: dict) -> dict:
    """Map the five live-site evidence areas from profile data."""
    recs = fin_records(p)
    ads = hiring_ads(p)
    news = news_items(p)
    roles = active_roles(p)
    locs = locations(p)
    # "verified profiles" ≈ leadership + locations present
    profiles_n = len(roles) + len(locs)
    return {
        "filing": bool(recs),
        "website": website_ok(p),
        "profiles": profiles_n > 0,
        "profiles_n": profiles_n,
        "updates": len(news) > 0,
        "updates_n": len(news),
        "roles": len(ads) > 0,
        "roles_n": len(ads),
    }


def score_five(cov: dict) -> int:
    return sum(
        1
        for k in ("filing", "website", "profiles", "updates", "roles")
        if cov.get(k)
    )


def script_json(obj) -> str:
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# ---------- status pills (the five points) ----------
def render_pills(cov: dict) -> str:
    items = []
    # Latest annual filing
    items.append(
        ("ok" if cov["filing"] else "miss",
         "Latest annual filing" if cov["filing"] else "No annual filing")
    )
    # Verified website
    items.append(
        ("ok" if cov["website"] else "miss",
         "Verified website" if cov["website"] else "Website not verified")
    )
    # verified profiles
    n = cov.get("profiles_n") or 0
    items.append(
        ("ok" if cov["profiles"] else "miss",
         f"{n} verified profiles" if n else "No verified profiles")
    )
    # company updates found
    u = cov.get("updates_n") or 0
    items.append(
        ("ok" if cov["updates"] else "miss",
         f"{u} company updates found" if u else "No company updates found")
    )
    # open roles found
    r = cov.get("roles_n") or 0
    items.append(
        ("ok" if cov["roles"] else "miss",
         f"{r} open roles found" if r else "No open roles found")
    )
    return "".join(
        f'<span class="pill {cls}"><span class="dot"></span>{esc(lbl)}</span>'
        for cls, lbl in items
    )


# ---------- detail panel sections ----------
def render_detail(p: dict, idx: int) -> str:
    ev = ev_of(p)
    syn = p.get("synthesis") or {}
    cov = coverage_five(p)
    sc = score_five(cov)

    industry = f"{p.get('industry_code') or ''} {p.get('industry_label') or ''}".strip()
    meta_line = " · ".join(
        x
        for x in [
            f"Organisation {p.get('organisation_number')}",
            p.get("legal_form"),
            p.get("municipality"),
        ]
        if x
    )

    # last checked from sources
    last = max(
        (s.get("retrieved_at") or "" for s in (syn.get("sources") or [])),
        default="",
    )
    last_s = f"Last checked {fmt_date(last)}" if last else ""

    # KPI strip
    recs = fin_records(p)
    latest = recs[0] if recs else {}
    period = (latest.get("period") or {}).get("tilDato") or latest.get("year") or "—"
    rev = money(latest.get("revenue"), compact=True)
    opres = money(latest.get("operating_result"), compact=True)
    employees = p.get("employees")
    emp_s = str(employees) if employees is not None else "—"
    locs = locations(p)
    ads = hiring_ads(p)
    news = news_items(p)

    kpis = f"""
    <div class="kpi-grid">
      <div class="kpi"><span class="kpi-lbl">Latest reported revenue</span>
        <span class="kpi-val">{esc(rev)}</span><span class="kpi-sub">NOK</span></div>
      <div class="kpi"><span class="kpi-lbl">Latest operating result</span>
        <span class="kpi-val">{esc(opres)}</span><span class="kpi-sub">{esc(str(period)[:4] if period != "—" else "—")}</span></div>
      <div class="kpi"><span class="kpi-lbl">Employees in register</span>
        <span class="kpi-val">{esc(emp_s)}</span><span class="kpi-sub">Official company record</span></div>
      <div class="kpi"><span class="kpi-lbl">Registered workplaces</span>
        <span class="kpi-val">{len(locs)}</span><span class="kpi-sub">Official subunits</span></div>
      <div class="kpi"><span class="kpi-lbl">Open roles found</span>
        <span class="kpi-val">{len(ads)}</span><span class="kpi-sub">Checked {esc(fmt_date(last) or "—")}</span></div>
    </div>"""

    # Overview
    summary = cite(syn.get("summary") or "No summary available.")
    changed = cite(syn.get("what_changed") or "—")
    overview = f"""
    <section class="sec" id="sec-overview-{idx}">
      <div class="sec-head"><h3>Company overview</h3><span class="sec-tag">What the company does</span></div>
      <p class="body">{summary}</p>
      <p class="body muted"><strong>What changed:</strong> {changed}</p>
    </section>"""

    # Financials
    if recs:
        rows = "".join(
            f"<tr><td>{esc((r.get('period') or {}).get('tilDato') or r.get('year'))}</td>"
            f'<td class="num">{money(r.get("revenue"))}</td>'
            f'<td class="num">{money(r.get("operating_result"))}</td>'
            f'<td class="num">{money(r.get("annual_result"))}</td>'
            f'<td class="num">{money(r.get("equity"))}</td></tr>'
            for r in recs[:5]
        )
        fin_src = (ev.get("financials") or {}).get("source_url")
        fin_html = f"""
        <div class="scroll"><table>
          <thead><tr><th>Period end</th><th class="num">Revenue</th>
          <th class="num">Operating result</th><th class="num">Annual result</th>
          <th class="num">Equity</th></tr></thead>
          <tbody>{rows}</tbody>
        </table></div>
        <p class="src-line">{link(fin_src, "Official financial source ▸")}</p>"""
        snap_rows = ""
        for label, key in [
            ("Reporting period", None),
            ("Revenue", "revenue"),
            ("Operating result", "operating_result"),
            ("Annual result", "annual_result"),
            ("Equity", "equity"),
        ]:
            if key is None:
                val_s = esc(
                    f"{(latest.get('period') or {}).get('fraDato') or ''} – "
                    f"{(latest.get('period') or {}).get('tilDato') or period}"
                )
            else:
                val_s = f"{money(latest.get(key))} NOK"
            snap_rows += f'<div class="snap-row"><span>{esc(label)}</span><strong>{val_s}</strong></div>'
        fin_html = f'<div class="snap">{snap_rows}</div>' + fin_html
    else:
        fin_html = '<p class="muted">No filings found</p>'
    financials = f"""
    <section class="sec" id="sec-financials-{idx}">
      <div class="sec-head"><h3>Financial snapshot</h3>
        <span class="sec-tag">Latest submitted annual account</span></div>
      {fin_html}
    </section>"""

    # Leadership
    roles = active_roles(p)
    if roles:
        cards = "".join(
            f'<div class="person-card"><strong>{esc(person(r))}</strong>'
            f'<span>{esc(r.get("role") or "—")}</span></div>'
            for r in roles[:12]
        )
        role_src = (ev.get("roles") or {}).get("source_url")
        lead_html = (
            f'<div class="person-grid">{cards}</div>'
            f'<p class="src-line">{link(role_src, "Official role source ▸")}</p>'
        )
    else:
        lead_html = '<p class="muted">No roles found</p>'
    leadership = f"""
    <section class="sec" id="sec-leadership-{idx}">
      <div class="sec-head"><h3>Leadership</h3><span class="sec-tag">Public roles</span></div>
      {lead_html}
    </section>"""

    # Locations
    if locs:
        loc_cards = "".join(
            f'<div class="loc-card"><strong>{esc(l.get("name") or "Location")}</strong>'
            f'<span>{esc(addr(l.get("address")))}</span></div>'
            for l in locs[:8]
        )
        loc_src = (ev.get("locations") or {}).get("source_url")
        loc_html = (
            f'<div class="loc-grid">{loc_cards}</div>'
            f'<p class="src-line">{link(loc_src, "Official location source ▸")}</p>'
        )
    else:
        loc_html = (
            f'<p class="muted">Registered municipality: {esc(p.get("municipality") or "—")}. '
            "No separate locations found.</p>"
        )
    locations_sec = f"""
    <section class="sec" id="sec-locations-{idx}">
      <div class="sec-head"><h3>Locations</h3>
        <span class="sec-tag">Registered operating presence</span></div>
      {loc_html}
    </section>"""

    # Hiring & activity
    hire = ev.get("hiring") or {}
    act = ev.get("public_activity") or {}
    if ads:
        ad_lis = "".join(
            f'<li class="ad"><strong>{link(a.get("url"), a.get("title") or "Job posting")}</strong>'
            f'<br><span class="muted small">{esc(ad_meta(a))}</span></li>'
            for a in ads[:8]
        )
        hire_html = f"<ul class='ad-list'>{ad_lis}</ul>"
    else:
        hire_html = (
            f'<p class="muted">{esc(hire.get("note") or hire.get("status") or "No open roles found")}</p>'
        )
    if news:
        news_lis = "".join(
            f'<li class="update"><span class="muted small">Update</span>'
            f'<p>{esc((n.get("title") or "")[:220])}</p>'
            f'{link(n.get("url"), "Open update ▸")}</li>'
            for n in news[:6]
        )
        news_html = f'<h4 class="subh">Recent company updates</h4><ul class="updates">{news_lis}</ul>'
    else:
        news_html = f'<p class="muted">{esc(act.get("note") or "No public activity found on verified sources")}</p>'
    hiring_sec = f"""
    <section class="sec" id="sec-hiring-{idx}">
      <div class="sec-head"><h3>Hiring &amp; public activity</h3>
        <span class="sec-tag">Verified company pages and dated activity</span></div>
      <h4 class="subh">Open roles</h4>
      {hire_html}
      {news_html}
    </section>"""

    # Sources
    sources = syn.get("sources") or []
    if sources:
        src_lis = "".join(
            f'<li><code class="mono">{esc(s.get("id"))}</code> '
            f'{esc(s.get("field"))} · {link(s.get("source_url"))} · '
            f'retrieved {esc(fmt_date(s.get("retrieved_at")))}</li>'
            for s in sources
        )
        src_html = f'<ul class="srcs">{src_lis}</ul>'
    else:
        src_html = '<p class="muted">No sources recorded.</p>'
    unknowns = "".join(
        f"<li>{esc(u)}</li>" for u in (syn.get("unknowns") or [])
    ) or "<li>—</li>"
    sources_sec = f"""
    <section class="sec" id="sec-sources-{idx}">
      <div class="sec-head"><h3>Sources found</h3>
        <span class="sec-tag">Evidence checked for this profile</span></div>
      {src_html}
      <h4 class="subh">Unknowns</h4>
      <ul class="unknowns">{unknowns}</ul>
    </section>"""

    score_badge = (
        f'<div class="score-badge">'
        f'<strong>{sc}</strong> of 5 data areas'
        f'<span>{"Data found in all five categories" if sc == 5 else "Partial evidence coverage"}</span>'
        f'<div class="score-dots">'
        + "".join(
            f'<span class="{"on" if cov.get(k) else ""}"></span>'
            for k, _ in FIVE
        )
        + "</div></div>"
    )

    return f"""
<article class="detail" data-idx="{idx}" data-name="{esc((p.get("name") or "").lower())}"
         data-org="{esc(p.get("organisation_number"))}" data-score="{sc}" hidden>
  <header class="detail-head">
    <p class="industry-eyebrow">{esc(industry.upper() if industry else "")}</p>
    <div class="detail-title-row">
      <div>
        <h2 class="company-title">{esc(p.get("name"))}</h2>
        <p class="meta">{esc(meta_line)}</p>
      </div>
      {score_badge}
    </div>
    <p class="lead-summary">{cite(syn.get("summary") or "")}</p>
    <div class="pills">{render_pills(cov)}</div>
    <p class="check-note muted small">{esc(last_s)} · Open each section to inspect its source</p>
  </header>
  <nav class="tabs" role="tablist">
    <button type="button" class="tab active" data-tab="overview">Overview</button>
    <button type="button" class="tab" data-tab="financials">Financials</button>
    <button type="button" class="tab" data-tab="leadership">Leadership</button>
    <button type="button" class="tab" data-tab="locations">Locations</button>
    <button type="button" class="tab" data-tab="hiring">Hiring &amp; activity</button>
    <button type="button" class="tab" data-tab="sources">Sources found</button>
  </nav>
  {kpis}
  <div class="tab-panels">
    {overview}
    {financials}
    {leadership}
    {locations_sec}
    {hiring_sec}
    {sources_sec}
  </div>
</article>"""


# ---------- ask panel data ----------
def ask_entry(p: dict) -> dict:
    ev = ev_of(p)
    syn = p.get("synthesis") or {}
    recs = fin_records(p)
    hire = ev.get("hiring") or {}
    act = ev.get("public_activity") or {}
    hv = hire.get("value") or {}
    return {
        "name": p.get("name"),
        "brief": [
            re.sub(r"\s*\[[A-Z]\d+\]", "", syn.get("summary") or "No summary available."),
            "What changed: " + (syn.get("what_changed") or "—"),
        ],
        "fin": [
            f"{(r.get('period') or {}).get('tilDato')}: revenue NOK {money(r.get('revenue'))}, "
            f"annual result NOK {money(r.get('annual_result'))}"
            for r in recs[:3]
        ]
        or ["No filings found in the sources on this profile."],
        "who": [f"{r.get('role')}: {person(r)}" for r in active_roles(p)[:8]]
        or ["No roles found in the sources on this profile."],
        "where": [addr(l.get("address")) for l in locations(p)[:4]]
        or [
            f"Registered municipality: {p.get('municipality') or '—'}. No separate locations found."
        ],
        "hiring": (
            [
                f"{a.get('title') or 'Job posting'} — {ad_meta(a)} — {a.get('url') or ''}"
                for a in hiring_ads(p)[:8]
            ]
            or [
                f"{hire.get('note') or 'Not checked.'}"
                + (
                    f" Open positions found: {hv['open_positions']}."
                    if "open_positions" in hv
                    else ""
                )
            ]
        ),
        "activity": [act.get("note") or ("Status: " + str(act.get("status")))],
        "sources": [
            f"{s.get('id')} {s.get('field')} — {s.get('source_url')}"
            for s in syn.get("sources") or []
        ]
        or ["No sources recorded."],
    }


# ---------- CSS (live-site palette & structure) ----------
CSS = r"""
:root{
  --canvas:#f3f0e9;--surface:#fffefa;--surface-2:#f8f6f1;--ink:#172126;
  --muted:#687177;--line:#ddd8ce;--accent:#922940;--accent-soft:#f4e9ec;
  --navy:#172a33;--good:#23704d;--gold:#a46b21;--shadow:0 14px 35px rgba(25,35,40,.07)
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
html,body{margin:0;min-height:100%;background:var(--canvas);color:var(--ink)}
body{font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,input,select,textarea{font:inherit}button{color:inherit;cursor:pointer}
a{color:var(--accent)}a:hover{color:#6e1f30}
:focus-visible{outline:3px solid rgba(146,41,64,.28);outline-offset:2px}

/* Top bar */
.topbar{height:72px;background:var(--surface);border-bottom:1px solid var(--line);
  display:grid;grid-template-columns:260px minmax(280px,1fr) auto;gap:20px;align-items:center;
  padding:0 24px;position:sticky;top:0;z-index:20}
.brand{display:flex;align-items:center;gap:11px;font:700 22px Georgia,serif;letter-spacing:-.02em;color:var(--ink);text-decoration:none}
.brand-mark{width:17px;height:17px;background:var(--accent);transform:rotate(45deg);border-radius:2px;
  box-shadow:7px -7px 0 -3px var(--navy);flex-shrink:0}
.brand-copy{display:grid;line-height:1.05}
.brand-copy small{font:600 9px/1.2 Inter,sans-serif;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-top:4px}
.global-search{position:relative;max-width:680px}
.global-search input{width:100%;height:44px;border:1px solid #cbc7bf;background:#f8f7f4;border-radius:9px;
  padding:0 46px 0 42px;outline:none;transition:.15s;color:var(--ink)}
.global-search input:focus{border-color:var(--navy);background:#fff;box-shadow:0 0 0 3px rgba(23,42,51,.08)}
.search-icon{position:absolute;left:14px;top:13px;color:var(--muted);pointer-events:none}
.shortcut{position:absolute;right:12px;top:10px;border:1px solid var(--line);background:#fff;color:var(--muted);
  border-radius:5px;padding:2px 7px;font-size:11px}
.top-meta{display:flex;justify-content:flex-end;align-items:center;gap:14px;color:var(--muted);font-size:12px;white-space:nowrap}
.top-meta strong{color:var(--ink)}

/* Run proof strip */
.run-proof{max-width:1536px;margin:0 auto;padding:16px 24px 0;display:grid;
  grid-template-columns:repeat(5,1fr);gap:0;background:transparent}
.run-proof .cell{background:var(--surface);border:1px solid var(--line);padding:16px 18px;min-width:0}
.run-proof .cell:first-child{border-radius:12px 0 0 12px}
.run-proof .cell:last-child{border-radius:0 12px 12px 0}
.run-proof .cell + .cell{border-left:0}
.run-proof .lbl{font:600 10px/1.2 Inter,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 6px}
.run-proof .val{font:600 17px/1.25 Georgia,serif;color:var(--ink);margin:0}
.run-proof .sub{font-size:12px;color:var(--muted);margin-top:4px}

/* Shell */
.shell{max-width:1536px;margin:0 auto;padding:18px 24px 48px;display:grid;
  grid-template-columns:280px minmax(0,1fr) 320px;gap:18px;align-items:start}
@media(max-width:1100px){.shell{grid-template-columns:1fr}.sidebar,.ask-panel{display:none}}
@media(max-width:900px){.run-proof{grid-template-columns:1fr 1fr}.run-proof .cell{border-radius:0!important;border:1px solid var(--line)!important}}

/* Directory sidebar */
.sidebar{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 0 8px;
  position:sticky;top:88px;max-height:calc(100vh - 100px);display:flex;flex-direction:column}
.side-head{padding:0 16px 12px}
.side-head .eyebrow{font:700 10px/1 Inter,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 4px}
.side-head h2{margin:0;font:700 26px/1.1 Georgia,serif;letter-spacing:-.02em}
.side-head .count{float:right;font:600 14px Inter,sans-serif;color:var(--muted);margin-top:6px}
.side-head p{margin:6px 0 0;font-size:13px;color:var(--muted);clear:both}
.side-ctl{display:flex;gap:8px;padding:0 16px 12px;flex-wrap:wrap}
.side-ctl select,.side-ctl button{height:36px;padding:0 12px;border-radius:8px;border:1px solid var(--line);
  background:var(--surface-2);font-size:13px;color:var(--ink)}
.side-ctl button.active{background:var(--navy);color:#fff;border-color:var(--navy)}
.company-list{overflow-y:auto;flex:1;padding:0 8px 8px}
.company-row{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;
  text-align:left;padding:10px 12px;border:0;border-radius:10px;background:transparent;color:var(--ink);
  border-left:3px solid transparent}
.company-row:hover{background:var(--surface-2)}
.company-row.active{background:var(--accent-soft);border-left-color:var(--accent)}
.company-name{display:block;font-weight:600;font-size:13.5px;line-height:1.25}
.company-meta{display:block;font-size:12px;color:var(--muted);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px}
.coverage-pill{flex-shrink:0;font-size:11px;font-weight:600;padding:3px 8px;border-radius:999px;
  background:#e8f3ed;color:var(--good);border:1px solid #c5dfd0}
.coverage-pill.partial{background:var(--surface-2);color:var(--muted);border-color:var(--line)}

/* Detail panel */
.detail-wrap{min-width:0}
.detail{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:22px 24px 28px;min-width:0}
.detail[hidden]{display:none}
.industry-eyebrow{font:700 10px/1.3 Inter,sans-serif;letter-spacing:.07em;text-transform:uppercase;color:var(--accent);margin:0 0 6px}
.detail-title-row{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}
.company-title{margin:0;font:700 34px/1.1 Georgia,serif;letter-spacing:-.03em}
.meta{margin:6px 0 0;font-size:13px;color:var(--muted)}
.lead-summary{margin:14px 0 0;font-size:15px;line-height:1.55;max-width:720px}
.score-badge{text-align:right;font-size:13px;color:var(--muted)}
.score-badge strong{display:block;font:700 28px/1 Georgia,serif;color:var(--good)}
.score-dots{display:flex;gap:4px;justify-content:flex-end;margin-top:6px}
.score-dots span{width:18px;height:4px;border-radius:2px;background:var(--line)}
.score-dots span.on{background:var(--good)}

/* Five pills */
.pills{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;padding:5px 12px;border-radius:999px;
  border:1px solid var(--line);background:var(--surface-2);color:var(--muted)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:#c4c0b6;flex-shrink:0}
.pill.ok{background:#e8f3ed;border-color:#c5dfd0;color:var(--good)}
.pill.ok .dot{background:var(--good)}
.pill.miss{border-style:dashed}
.check-note{margin:10px 0 0}

/* Tabs */
.tabs{display:flex;flex-wrap:wrap;gap:0;margin:20px 0 0;border-bottom:1px solid var(--line)}
.tab{background:none;border:0;border-bottom:2px solid transparent;padding:10px 14px;font-size:13.5px;
  color:var(--muted);margin-bottom:-1px}
.tab:hover{color:var(--ink)}
.tab.active{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}

/* KPI grid */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:0;
  margin:18px 0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surface-2)}
.kpi{padding:14px 16px;border-right:1px solid var(--line)}
.kpi:last-child{border-right:0}
.kpi-lbl{display:block;font:600 10px/1.2 Inter,sans-serif;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.kpi-val{display:block;font:700 24px/1.1 Georgia,serif;letter-spacing:-.02em;color:var(--ink)}
.kpi-sub{display:block;font-size:12px;color:var(--muted);margin-top:4px}

/* Sections */
.sec{margin-top:28px;padding-top:8px}
.sec-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.sec-head h3{margin:0;font:700 22px/1.2 Georgia,serif;letter-spacing:-.02em}
.sec-tag{font-size:12px;color:var(--muted)}
.body{margin:0 0 8px;font-size:15px;line-height:1.55}
.subh{margin:18px 0 8px;font-size:14px;font-weight:600}
.muted{color:var(--muted)}.small{font-size:12px}
.mono,code{font-family:ui-monospace,"IBM Plex Mono",monospace;font-size:.9em}
code{background:var(--surface-2);padding:.1em .35em;border-radius:4px}
.cite{font-family:ui-monospace,monospace;font-size:10px;color:var(--accent);margin-left:2px}
.src-line{margin:10px 0 0;font-size:13px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:420px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12px}
.num{text-align:right}
.snap{border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:14px}
.snap-row{display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px}
.snap-row:last-child{border-bottom:0}
.person-grid,.loc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.person-card,.loc-card{background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.person-card strong,.loc-card strong{display:block;font-size:14px;margin-bottom:2px}
.person-card span,.loc-card span{font-size:12.5px;color:var(--muted)}
.ad-list,.updates,.srcs,.unknowns{margin:0;padding-left:18px;line-height:1.5}
.ad{margin-bottom:10px}.update{margin-bottom:14px}
.unknowns{color:var(--muted)}

/* Ask panel */
.ask-panel{background:var(--navy);color:#f4f5f2;border-radius:14px;padding:20px 18px 22px;
  position:sticky;top:88px;max-height:calc(100vh - 100px);overflow-y:auto}
.ask-panel .eyebrow{font:700 10px/1 Inter,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#a9c9bf;margin:0 0 6px}
.ask-panel h2{margin:0;font:700 26px/1.15 Georgia,serif;letter-spacing:-.02em;display:flex;align-items:center;gap:10px}
.ask-orb{width:28px;height:28px;border-radius:50%;background:radial-gradient(circle at 35% 35%,#7ec8b0,#1a4a40 70%);
  box-shadow:0 0 0 3px rgba(126,200,176,.25);flex-shrink:0}
.ask-panel > p{margin:10px 0 0;font-size:13.5px;color:#d3e3dd;line-height:1.45}
.ask-input{margin-top:16px;width:100%;min-height:72px;padding:12px 14px;border-radius:10px;border:1px solid #3a5550;
  background:#0f2420;color:#f4f5f2;resize:vertical;font-size:14px}
.ask-input::placeholder{color:#8aa69c}
.btn-check{margin-top:12px;width:100%;height:48px;border:0;border-radius:10px;background:var(--accent);color:#fff;
  font-weight:700;font-size:15px}
.btn-check:hover{background:#7a2236}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.chip{height:36px;padding:0 14px;border-radius:999px;border:1px solid #4a6b63;background:transparent;
  color:#f4f5f2;font-size:13px}
.chip:hover{background:#1e3d36}
.answer{margin-top:16px;background:#0a2420;border-radius:10px;padding:14px 16px;min-height:0}
.answer:empty{display:none}
.answer h4{margin:0 0 6px;font-size:13px;color:#a9c9bf;font-weight:600}
.answer ul{margin:0;padding-left:18px;line-height:1.5;font-size:13.5px}
.ask-foot{margin-top:14px;font-size:12px;color:#8aa69c;line-height:1.4}

.empty-state{padding:48px 24px;text-align:center;color:var(--muted)}
.sr{position:absolute;left:-9999px}
"""

# ---------- JS ----------
JS = r"""
const DATA = JSON.parse(document.getElementById('ask-data').textContent);
const rows = [...document.querySelectorAll('.company-row')];
const details = [...document.querySelectorAll('.detail')];
const q = document.getElementById('q');
const sortEl = document.getElementById('sort');
const only5 = document.getElementById('only5');
const list = document.getElementById('company-list');
const countEl = document.getElementById('side-count');

function showDetail(idx) {
  details.forEach(d => { d.hidden = d.dataset.idx !== String(idx); });
  rows.forEach(r => r.classList.toggle('active', r.dataset.idx === String(idx)));
  const d = details.find(x => x.dataset.idx === String(idx));
  if (d) {
    d.querySelectorAll('.tab').forEach((t, i) => {
      t.classList.toggle('active', i === 0);
    });
  }
  document.getElementById('who').value = String(idx);
}

function applyFilter() {
  const t = (q.value || '').trim().toLowerCase();
  const only = only5.classList.contains('active');
  let sorted = [...rows];
  if (sortEl.value === 'name') {
    sorted.sort((a, b) => a.dataset.name.localeCompare(b.dataset.name, 'nb'));
  } else {
    sorted.sort((a, b) => (Number(b.dataset.score) - Number(a.dataset.score)) ||
      a.dataset.name.localeCompare(b.dataset.name, 'nb'));
  }
  let n = 0;
  sorted.forEach(r => {
    list.appendChild(r);
    const ok = (!t || r.dataset.name.includes(t) || r.dataset.org.includes(t)) &&
      (!only || r.dataset.score === '5');
    r.hidden = !ok;
    if (ok) n++;
  });
  countEl.textContent = n;
  const active = rows.find(r => r.classList.contains('active') && !r.hidden);
  if (!active) {
    const first = sorted.find(r => !r.hidden);
    if (first) showDetail(first.dataset.idx);
  }
}

rows.forEach(r => r.addEventListener('click', () => showDetail(r.dataset.idx)));
q.addEventListener('input', applyFilter);
sortEl.addEventListener('change', applyFilter);
only5.addEventListener('click', () => {
  only5.classList.toggle('active');
  applyFilter();
});
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault(); q.focus(); q.select();
  }
});

document.querySelectorAll('.detail').forEach(detail => {
  detail.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      detail.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const key = tab.dataset.tab;
      const sec = detail.querySelector('[id^="sec-' + key + '"]');
      if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
});

const who = document.getElementById('who');
const ask = document.getElementById('ask');
const ans = document.getElementById('answer');

function topic(t) {
  t = (t || '').toLowerCase();
  if (/who|run|lead|board|manag|ceo|styre/.test(t)) return ['who', 'Who runs it'];
  if (/financ|revenue|filing|profit|result|account|equity/.test(t)) return ['fin', 'Latest financials'];
  if (/hir|job|work|career|vacanc|position|role/.test(t)) return ['hiring', 'Working here'];
  if (/activit|news|press|post|update/.test(t)) return ['activity', 'Recent activity'];
  if (/where|locat|address|office|operate/.test(t)) return ['where', 'Locations'];
  if (/evidence|source|check/.test(t)) return ['sources', 'Evidence checked'];
  return ['brief', 'Company brief'];
}
function showAns(key, title) {
  const d = DATA[who.value];
  if (!d) return;
  ans.replaceChildren();
  const h = document.createElement('h4');
  h.textContent = d.name + ' · ' + title;
  ans.appendChild(h);
  const ul = document.createElement('ul');
  (d[key] || []).forEach(s => {
    const li = document.createElement('li');
    li.textContent = s;
    ul.appendChild(li);
  });
  ans.appendChild(ul);
}
document.getElementById('go').addEventListener('click', () => {
  const t = topic(ask.value); showAns(t[0], t[1]);
});
ask.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const t = topic(ask.value); showAns(t[0], t[1]);
  }
});
document.querySelectorAll('[data-topic]').forEach(b =>
  b.addEventListener('click', () => showAns(b.dataset.topic, b.textContent.trim()))
);

if (rows.length) showDetail(rows[0].dataset.idx);
applyFilter();
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>%%TITLE%%</title>
<meta name="description" content="Research Norwegian companies across official filings, leadership, locations, websites and public activity."/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Georgia&display=swap" rel="stylesheet"/>
<style>%%CSS%%</style>
</head>
<body>
<header class="topbar">
  <a class="brand" href="#">
    <span class="brand-mark" aria-hidden="true"></span>
    <span class="brand-copy">Signalpost<small>Norwegian company research</small></span>
  </a>
  <label class="global-search">
    <span class="sr">Search company, organisation number, industry or place</span>
    <svg class="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
    <input id="q" type="search" placeholder="Search company, organisation number, industry or place" autocomplete="off"/>
    <kbd class="shortcut">⌘ K</kbd>
  </label>
  <div class="top-meta">
    <span><strong>%%N%%</strong> companies in this sample</span>
    <span><strong>%%FULL%%</strong> have data in all five categories</span>
  </div>
</header>

<div class="run-proof">%%STATS%%</div>

<div class="shell">
  <aside class="sidebar">
    <div class="side-head">
      <p class="eyebrow">Company directory</p>
      <span class="count" id="side-count">%%N%%</span>
      <h2>Companies</h2>
      <p>Profiles with the most verified data appear first.</p>
    </div>
    <div class="side-ctl">
      <select id="sort" aria-label="Sort">
        <option value="data">Most data found</option>
        <option value="name">Name A–Z</option>
      </select>
      <button type="button" id="only5">All five areas</button>
    </div>
    <div class="company-list" id="company-list" role="listbox" aria-label="Companies">
%%ROWS%%
    </div>
  </aside>

  <div class="detail-wrap">
%%DETAILS%%
    <div class="empty-state" id="empty" hidden>No companies match this search.</div>
  </div>

  <aside class="ask-panel">
    <p class="eyebrow">Company research agent</p>
    <h2><span class="ask-orb" aria-hidden="true"></span> Ask Signalpost</h2>
    <p>Ask what the company does, who runs it, where it operates, how the latest filing looks, or whether it is hiring.</p>
    <label class="sr" for="who">Company</label>
    <select id="who" hidden>%%OPTIONS%%</select>
    <label class="sr" for="ask">Your question</label>
    <textarea id="ask" class="ask-input" placeholder="How is this company doing? Is it hiring? Who runs it?" rows="3"></textarea>
    <button id="go" class="btn-check" type="button">Check the evidence</button>
    <div class="chips">
      <button class="chip" type="button" data-topic="brief">Company brief</button>
      <button class="chip" type="button" data-topic="fin">Latest financials</button>
      <button class="chip" type="button" data-topic="who">Who runs it?</button>
      <button class="chip" type="button" data-topic="hiring">Working here</button>
      <button class="chip" type="button" data-topic="activity">Recent activity</button>
    </div>
    <div id="answer" class="answer" aria-live="polite"></div>
    <p class="ask-foot">Every answer is limited to the sources shown on this profile. Missing evidence stays missing.</p>
  </aside>
</div>

<script id="ask-data" type="application/json">%%ASKDATA%%</script>
<script>%%JS%%</script>
</body>
</html>
"""


def build_stats(rows: list, report: dict | None) -> tuple[str, int, int]:
    n = len(rows)
    covs = [coverage_five(p) for p in rows]
    full = sum(score_five(c) == 5 for c in covs)
    last = max(
        (
            s.get("retrieved_at") or ""
            for p in rows
            for s in (p.get("synthesis") or {}).get("sources") or []
        ),
        default="",
    )
    checked = fmt_date((report or {}).get("completed_at") or last) or "—"

    cells = []
    cells.append(
        f'<div class="cell"><p class="lbl">About this sample</p>'
        f'<p class="val">{n} sample profiles · checked {esc(checked)}</p></div>'
    )
    if report:
        cells.append(
            f'<div class="cell"><p class="lbl">Records produced in reference tests</p>'
            f'<p class="val">{esc(report.get("emitted_envelopes"))} / '
            f'{esc(report.get("expected_count"))}</p></div>'
        )
        v = report.get("validation") or {}
        cells.append(
            f'<div class="cell"><p class="lbl">Published identity audit</p>'
            f'<p class="val">{"0 wrong-company publications" if v.get("passed") else "Validation issues"}</p></div>'
        )
        checks = v.get("checks") or {}
        ok_n = sum(bool(x) for x in checks.values())
        cells.append(
            f'<div class="cell"><p class="lbl">Fresh PDF audit</p>'
            f'<p class="val">{ok_n} / {len(checks)} fields matched</p></div>'
        )
    else:
        cells.append(
            f'<div class="cell"><p class="lbl">Records produced in reference tests</p>'
            f'<p class="val">—</p></div>'
        )
        cells.append(
            f'<div class="cell"><p class="lbl">Published identity audit</p>'
            f'<p class="val">—</p></div>'
        )
        cells.append(
            f'<div class="cell"><p class="lbl">Fresh PDF audit</p>'
            f'<p class="val">—</p></div>'
        )
    cells.append(
        f'<div class="cell"><p class="lbl">Evidence coverage</p>'
        f'<p class="val">{full} / {n} have data in all five categories</p></div>'
    )
    return "".join(cells), n, full


def render_row(p: dict, idx: int) -> str:
    cov = coverage_five(p)
    sc = score_five(cov)
    pill_cls = "" if sc == 5 else "partial"
    meta = " · ".join(
        x
        for x in [
            (p.get("municipality") or "").upper(),
            (p.get("industry_label") or "")[:40],
        ]
        if x
    )
    return (
        f'<button type="button" class="company-row" role="option" data-idx="{idx}" '
        f'data-name="{esc((p.get("name") or "").lower())}" '
        f'data-org="{esc(p.get("organisation_number"))}" data-score="{sc}" '
        f'aria-label="{esc(p.get("name"))}">'
        f'<span><span class="company-name">{esc(p.get("name"))}</span>'
        f'<span class="company-meta">{esc(meta)}</span></span>'
        f'<span class="coverage-pill {pill_cls}" aria-label="{sc} of 5 data areas">'
        f'{sc}/5 data</span></button>'
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a static HTML viewer from envelopes or profiles "
        "(layout matched to builderr.ai/signalpost)"
    )
    ap.add_argument("--input", required=True, help="envelopes.jsonl or profiles.jsonl")
    ap.add_argument("--output", required=True, help="viewer.html")
    ap.add_argument("--report", help="optional run report JSON")
    ap.add_argument("--title", default="Signalpost — Norwegian company research")
    args = ap.parse_args()

    rows = []
    for line in Path(args.input).read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            rows.append(obj.get("profile") or obj)
    report = (
        json.loads(Path(args.report).read_text(encoding="utf-8")) if args.report else None
    )

    stats, n, full = build_stats(rows, report)
    ask_data = {str(i): ask_entry(p) for i, p in enumerate(rows)}
    options = "".join(
        f'<option value="{i}">{esc(p.get("name"))}</option>' for i, p in enumerate(rows)
    )
    row_html = "\n".join(render_row(p, i) for i, p in enumerate(rows))
    detail_html = "\n".join(render_detail(p, i) for i, p in enumerate(rows))

    page = (
        PAGE.replace("%%TITLE%%", esc(args.title))
        .replace("%%CSS%%", CSS)
        .replace("%%STATS%%", stats)
        .replace("%%N%%", str(n))
        .replace("%%FULL%%", str(full))
        .replace("%%ROWS%%", row_html)
        .replace("%%DETAILS%%", detail_html)
        .replace("%%OPTIONS%%", options)
        .replace("%%ASKDATA%%", script_json(ask_data))
        .replace("%%JS%%", JS)
    )
    Path(args.output).write_text(page, encoding="utf-8")
    print(f"Wrote {args.output} ({len(rows)} companies)")


if __name__ == "__main__":
    main()