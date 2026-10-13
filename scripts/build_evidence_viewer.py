#!/usr/bin/env python3
"""Build a static, responsive Signalpost viewer from envelopes.jsonl or profiles.jsonl.

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

CATS = [("filings", "Filings"), ("leadership", "Leadership"), ("locations", "Locations"),
        ("website", "Website"), ("activity", "Activity")]
NBSP = "\u00a0"


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


def link(url, label=None) -> str:
    s = safe_url(url)
    if not s:
        return esc(label or url)
    return f'<a href="{esc(s)}" target="_blank" rel="noopener noreferrer">{esc(label or s)}</a>'


def cite(text) -> str:
    return re.sub(r"\[([A-Z]\d+)\]", r'<sup class="cite">\1</sup>', esc(text))


def fmt_date(s) -> str:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return str(s or "")


def money(v) -> str:
    try:
        return f"{float(v):,.0f}".replace(",", NBSP)
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
    return sorted(recs, key=lambda r: (r.get("period") or {}).get("tilDato") or "", reverse=True)


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
    town = " ".join(x for x in [a.get("postnummer"), a.get("poststed") or a.get("kommune")] if x)
    return ", ".join(x for x in [street, town] if x) or "—"


def website_ok(p: dict) -> bool:
    w = ev_of(p).get("website") or {}
    ident = (w.get("value") or {}).get("identity_assessment") or {}
    return w.get("status") == "available" and bool(ident.get("publishable"))


def coverage(p: dict) -> dict:
    ev = ev_of(p)
    return {
        "filings": bool(fin_records(p)),
        "leadership": bool(active_roles(p)),
        "locations": bool(locations(p)),
        "website": website_ok(p),
        "activity": (ev.get("public_activity") or {}).get("status") == "available",
    }


def status_label(entry: dict | None, ok: bool = True) -> str:
    s = (entry or {}).get("status") or "unknown"
    return s if ok else "withheld (identity not verified)"


# ---------- card ----------
def render_card(p: dict) -> str:
    ev = ev_of(p)
    syn = p.get("synthesis") or {}
    cov = coverage(p)
    score = sum(cov.values())

    tags = "".join(
        f'<span class="tag {"ok" if cov[k] else "miss"}">{lbl}{"" if cov[k] else " · missing"}</span>'
        for k, lbl in CATS)

    meta_bits = [f"{p.get('industry_code') or ''} {p.get('industry_label') or ''}".strip(),
                 p.get("municipality"), p.get("legal_form"),
                 f"{p['employees']} employees" if p.get("employees") is not None else None]
    meta = " · ".join(esc(b) for b in meta_bits if b)

    unknowns = "".join(f"<li>{esc(u)}</li>" for u in syn.get("unknowns") or []) or "<li>—</li>"
    sources = "".join(
        f'<li><code class="mono">{esc(s.get("id"))}</code> {esc(s.get("field"))} · '
        f'{link(s.get("source_url"))} · retrieved {esc(fmt_date(s.get("retrieved_at")))}</li>'
        for s in syn.get("sources") or []) or "<li>—</li>"

    # website
    web = ev.get("website") or {}
    wv = web.get("value") or {}
    if website_ok(p):
        site = (f'<p>Status: <code class="mono">{esc(web.get("status"))}</code></p>'
                f'<p>{link(wv.get("final_url") or web.get("source_url"))}</p>'
                + (f'<p class="muted">{esc(wv.get("title"))}</p>' if wv.get("title") else ""))
    else:
        withheld = web.get("status") == "available"
        site = (f'<p>Status: <code class="mono">'
                f'{esc("withheld (identity not verified)" if withheld else web.get("status"))}</code></p>'
                + (f'<p class="muted">{esc(web.get("note"))}</p>' if web.get("note") and not withheld else ""))

    # activity + hiring
    act = ev.get("public_activity") or {}
    news = (act.get("value") or {}).get("news_pages") or []
    news_html = "".join(f'<li>{link(n.get("url"), n.get("title") or n.get("url"))}</li>' for n in news[:5])
    activity = (f'<p>Status: <code class="mono">{esc(act.get("status"))}</code></p>'
                + (f"<ul>{news_html}</ul>" if news_html else
                   f'<p class="muted">{esc(act.get("note") or "None found on verified site")}</p>'))
    hire = ev.get("hiring") or {}
    hiring = (f'<p>Status: <code class="mono">{esc(hire.get("status"))}</code></p>'
              f'<p class="muted">{esc(hire.get("note"))}</p>')

    # leadership
    roles = active_roles(p)
    role_li = "".join(f"<li>{esc(r.get('role'))}: {esc(person(r))}</li>" for r in roles[:6])
    more = (f'<details><summary>{len(roles) - 6} more</summary><ul>'
            + "".join(f"<li>{esc(r.get('role'))}: {esc(person(r))}</li>" for r in roles[6:]) + "</ul></details>"
            ) if len(roles) > 6 else ""
    leadership = f"<ul>{role_li or '<li>—</li>'}</ul>{more}"

    # locations
    locs = locations(p)
    loc_li = "".join(f"<li>{esc(addr(l.get('address')))}</li>" for l in locs[:4])
    locs_html = f"<ul>{loc_li}</ul>" if loc_li else '<p class="muted">None found</p>'

    # group
    grp = ev.get("group") or {}
    gv = grp.get("value") or {}
    if grp.get("status") == "available" and gv:
        kids = gv.get("children") or []
        group = (f'<p>{esc(gv.get("navn"))} <span class="mono muted">({esc(gv.get("organisasjonsnummer"))})</span></p>'
                 f'<p class="muted">{len(kids)} related entit{"y" if len(kids) == 1 else "ies"} in group</p>')
    else:
        group = f'<p class="muted">{esc("No group structure found" if grp else "—")}</p>'

    # financials table
    recs = fin_records(p)[:3]
    if recs:
        rows = "".join(
            f'<tr><td>{esc((r.get("period") or {}).get("tilDato") or r.get("year"))}</td>'
            f'<td class="num">{money(r.get("revenue"))}</td><td class="num">{money(r.get("operating_result"))}</td>'
            f'<td class="num">{money(r.get("annual_result"))}</td><td class="num">{money(r.get("equity"))}</td></tr>'
            for r in recs)
        fin = (f'<div class="scroll"><table><thead><tr><th>Period end</th><th class="num">Revenue</th>'
               f'<th class="num">Operating result</th><th class="num">Annual result</th><th class="num">Equity</th></tr>'
               f'</thead><tbody>{rows}</tbody></table></div>'
               f'<p class="muted small">NOK · {link((ev.get("financials") or {}).get("source_url"), "source")}</p>')
    else:
        fin = '<p class="muted">No filings found</p>'

    return f"""
<article class="card" data-name="{esc((p.get('name') or '').lower())}" data-org="{esc(p.get('organisation_number'))}" data-score="{score}">
  <div>
    <h3>{esc(p.get("name"))} <span class="mono org">({esc(p.get("organisation_number"))})</span></h3>
    <p class="meta">{meta}</p>
  </div>
  <div class="tags" aria-label="Evidence coverage, {score} of 5">{tags}</div>
  <p class="body">{cite(syn.get("summary"))}</p>
  <p class="body"><strong>What changed:</strong> {cite(syn.get("what_changed")) or "—"}</p>
  <details open><summary>Unknowns</summary><ul class="unknowns">{unknowns}</ul></details>
  <details><summary>Sources for this summary</summary><ul class="srcs">{sources}</ul></details>
  <div class="secs">
    <div class="sec"><p class="lbl">Website</p>{site}</div>
    <div class="sec"><p class="lbl">Public activity</p>{activity}</div>
    <div class="sec"><p class="lbl">Hiring</p>{hiring}</div>
    <div class="sec"><p class="lbl">Leadership</p>{leadership}</div>
    <div class="sec"><p class="lbl">Locations</p>{locs_html}</div>
    <div class="sec"><p class="lbl">Group</p>{group}</div>
    <div class="sec wide"><p class="lbl">Recent financials</p>{fin}</div>
  </div>
</article>"""


# ---------- ask panel data (answers built only from each profile) ----------
def ask_entry(p: dict) -> dict:
    ev = ev_of(p)
    syn = p.get("synthesis") or {}
    recs = fin_records(p)
    hire = ev.get("hiring") or {}
    act = ev.get("public_activity") or {}
    hv = hire.get("value") or {}
    return {
        "name": p.get("name"),
        "brief": [re.sub(r"\s*\[[A-Z]\d+\]", "", syn.get("summary") or "No summary available."),
                  "What changed: " + (syn.get("what_changed") or "—")],
        "fin": [f"{(r.get('period') or {}).get('tilDato')}: revenue NOK {money(r.get('revenue'))}, "
                f"annual result NOK {money(r.get('annual_result'))}" for r in recs[:3]]
               or ["No filings found in the sources on this profile."],
        "who": [f"{r.get('role')}: {person(r)}" for r in active_roles(p)[:8]]
               or ["No roles found in the sources on this profile."],
        "where": [addr(l.get("address")) for l in locations(p)[:4]]
                 or [f"Registered municipality: {p.get('municipality') or '—'}. No separate locations found."],
        "hiring": [f"{hire.get('note') or 'Not checked.'}"
                   + (f" Open positions found: {hv['open_positions']}." if "open_positions" in hv else "")],
        "activity": [act.get("note") or ("Status: " + str(act.get("status")))],
        "sources": [f"{s.get('id')} {s.get('field')} — {s.get('source_url')}"
                    for s in syn.get("sources") or []] or ["No sources recorded."],
    }


def script_json(obj) -> str:
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


# ---------- page ----------
CSS = """
*{box-sizing:border-box}
body{margin:0;background:#F4F5F2;color:#14201C;font-family:'Schibsted Grotesk',system-ui,-apple-system,'Segoe UI',sans-serif;line-height:1.4}
a{color:#0F5C4D}a:hover{color:#0A3F35}
.wrap{max-width:1200px;margin:0 auto;padding:0 20px}
.mono,code{font-family:'IBM Plex Mono',ui-monospace,monospace}
code{font-size:.85em;background:#EEF2EF;padding:.1em .35em;border-radius:4px;overflow-wrap:anywhere}
header.top{background:#fff;border-bottom:1px solid #DCE1DD}
header.top .wrap{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding-top:14px;padding-bottom:14px}
.brand{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.brand b{font-size:24px;letter-spacing:-.02em;color:#0F5C4D}.brand span{font-size:14px;color:#4A5753}
.search{display:flex;align-items:center;gap:10px;min-height:44px;padding:0 14px;border:1px solid #B9C4BE;border-radius:999px;background:#fff;flex:0 1 340px;min-width:200px}
.search input{flex:1;border:0;outline:0;font:inherit;font-size:15px;min-width:0;background:transparent;color:#14201C}
.search:focus-within{border-color:#0F5C4D;box-shadow:0 0 0 3px #DDEFE8}
kbd{font-size:12px;color:#4A5753}
h1.hero{margin:0;font-size:clamp(30px,6vw,56px);line-height:1.06;letter-spacing:-.03em;max-width:820px}
.lead{margin:16px 0 0;font-size:18px;line-height:1.5;color:#4A5753;max-width:680px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:14px;margin-top:32px}
.stat b{display:block;font-size:clamp(32px,5vw,44px);letter-spacing:-.03em;color:#0F5C4D;line-height:1.1}
.stat span{font-size:15px}.stat small{display:block;font-size:13px;color:#4A5753;margin-top:4px}
.card{background:#fff;border:1px solid #DCE1DD;border-radius:14px;padding:22px;display:flex;flex-direction:column;gap:14px;min-width:0}
.card[hidden]{display:none}
.card h3{margin:0;font-size:20px;line-height:1.25;overflow-wrap:anywhere}
.org{font-size:14px;font-weight:400;color:#4A5753}.meta{margin:4px 0 0;font-size:14px;color:#4A5753}
.body{margin:0;font-size:15px;line-height:1.55}
.cite{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#0F5C4D;margin-left:2px}
.tags{display:flex;flex-wrap:wrap;gap:6px}
.tag{display:inline-block;font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid #B9C4BE}
.tag.ok{background:#DDEFE8;border-color:#0F5C4D;color:#0A3F35}.tag.miss{border-style:dashed;color:#4A5753}
details{font-size:14px}summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:500}
details ul{margin:0;padding-left:18px;line-height:1.5}.unknowns{color:#4A5753}.srcs{overflow-wrap:anywhere}
.secs{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:10px}
.sec{background:#F4F5F2;border-radius:10px;padding:12px 14px;min-width:0;overflow-wrap:anywhere}
.sec.wide{grid-column:1/-1}
.sec p{margin:0 0 4px;font-size:14px;line-height:1.5}.sec ul{margin:0;padding-left:18px;font-size:14px;line-height:1.5}
.lbl{font-size:12px!important;letter-spacing:.06em;text-transform:uppercase;color:#4A5753;font-weight:700;margin:0 0 6px!important}
.muted{color:#4A5753}.small{font-size:12px!important}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;min-width:460px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #DCE1DD;white-space:nowrap}.num{text-align:right}
.bar{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px}
.bar h2{margin:0;font-size:32px;letter-spacing:-.02em}.eyebrow{margin:0 0 4px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#4A5753;font-weight:700}
.ctl{display:flex;flex-wrap:wrap;gap:12px;align-items:center;font-size:14px}
select,.ctl input[type=text]{min-height:44px;padding:0 12px;border-radius:10px;border:1px solid #B9C4BE;background:#fff;font:inherit;font-size:14px;color:#14201C;max-width:100%}
.ctl label.chk{display:flex;align-items:center;gap:8px;min-height:44px}.ctl input[type=checkbox]{width:18px;height:18px;accent-color:#0F5C4D}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:18px;margin-top:24px}
.empty{margin-top:24px;color:#4A5753}
.ask{margin-top:64px;padding:clamp(20px,4vw,36px);border-radius:14px;background:#0F2F29;color:#F4F5F2;display:flex;flex-direction:column;gap:18px}
.ask h2{margin:0;font-size:32px;letter-spacing:-.02em}.ask .eyebrow{color:#A9C9BF}.ask p{margin:8px 0 0;color:#D3E3DD;line-height:1.5}
.askrow{display:flex;flex-wrap:wrap;gap:10px}.askrow select{flex:1 1 220px;min-height:48px}
.askrow input{flex:2 1 260px;min-height:48px;padding:0 16px;border-radius:10px;border:1px solid #A9C9BF;background:#fff;font:inherit;font-size:16px;min-width:0}
.btn{min-height:48px;padding:0 24px;border-radius:10px;border:0;background:#F4F5F2;color:#0F2F29;font:inherit;font-size:16px;font-weight:700;cursor:pointer}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{min-height:44px;padding:0 16px;border-radius:999px;border:1px solid #6E958A;background:transparent;color:#F4F5F2;font:inherit;font-size:14px;cursor:pointer}
.chip:hover,.btn:hover{background:#DDEFE8;color:#0F2F29}
.answer{background:#0A2420;border-radius:10px;padding:14px 18px;min-height:20px}.answer ul{margin:6px 0 0;padding-left:18px;line-height:1.55;overflow-wrap:anywhere}
.answer:empty{display:none}.answer h4{margin:0;font-size:14px;color:#A9C9BF}
.foot{margin:32px 0 0;font-size:13px;color:#4A5753;overflow-wrap:anywhere}
.sr{position:absolute;left:-9999px}
:focus-visible{outline:3px solid #0F5C4D;outline-offset:2px}
"""

JS = """
const DATA=JSON.parse(document.getElementById('ask-data').textContent);
const grid=document.getElementById('grid'),cards=[...grid.children];
const q=document.getElementById('q'),only5=document.getElementById('only5'),sort=document.getElementById('sort');
const count=document.getElementById('count'),empty=document.getElementById('empty');
function apply(){
  const t=q.value.trim().toLowerCase();
  cards.sort((a,b)=>sort.value==='name'?a.dataset.name.localeCompare(b.dataset.name,'nb'):(b.dataset.score-a.dataset.score)||a.dataset.name.localeCompare(b.dataset.name,'nb'));
  let n=0;
  cards.forEach(c=>{grid.appendChild(c);const ok=(!t||c.dataset.name.includes(t)||c.dataset.org.includes(t))&&(!only5.checked||c.dataset.score==='5');c.hidden=!ok;if(ok)n++;});
  count.textContent=n+' of '+cards.length+' companies shown';empty.hidden=n>0;
}
[q,only5,sort].forEach(e=>e.addEventListener('input',apply));apply();
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();q.focus();q.select();}});
const who=document.getElementById('who'),ask=document.getElementById('ask'),ans=document.getElementById('answer');
function topic(t){t=t.toLowerCase();
  if(/who|run|lead|board|manag|ceo|styre/.test(t))return['who','Who runs it'];
  if(/financ|revenue|filing|profit|result|account|equity/.test(t))return['fin','Latest financials'];
  if(/hir|job|work|career|vacanc|position/.test(t))return['hiring','Working here'];
  if(/activit|news|press|post/.test(t))return['activity','Recent activity'];
  if(/where|locat|address|office|operate/.test(t))return['where','Locations'];
  if(/evidence|source|check/.test(t))return['sources','Evidence checked'];
  return['brief','Company brief'];}
function show(key,title){
  const d=DATA[who.value];ans.replaceChildren();
  const h=document.createElement('h4');h.textContent=d.name+' · '+title;ans.appendChild(h);
  const ul=document.createElement('ul');(d[key]||[]).forEach(s=>{const li=document.createElement('li');li.textContent=s;ul.appendChild(li);});
  ans.appendChild(ul);}
document.getElementById('go').addEventListener('click',()=>{const t=topic(ask.value);show(t[0],t[1]);});
ask.addEventListener('keydown',e=>{if(e.key==='Enter'){const t=topic(ask.value);show(t[0],t[1]);}});
document.querySelectorAll('[data-topic]').forEach(b=>b.addEventListener('click',()=>show(b.dataset.topic,b.textContent)));
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>%%TITLE%%</title>

<style>%%CSS%%</style>
</head>
<body>
<header class="top"><div class="wrap">
  <div class="brand"><b>Signalpost</b><span>Norwegian company research</span></div>
  <label class="search"><span class="sr">Search companies by name or organisation number</span>
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
    <input id="q" type="search" placeholder="Search companies" autocomplete="off"/><kbd>⌘ K</kbd></label>
</div></header>
<main>
<section class="wrap" style="padding-top:48px">
  <h1 class="hero">Official filings, leadership, locations, websites and public activity.</h1>
  <p class="lead">Signalpost brings official Norwegian company records together with verified company websites and attributed public activity.</p>
  <div class="stats">%%STATS%%</div>
  <div class="tags" style="margin-top:18px">%%COVTAGS%%</div>
</section>
<section class="wrap" style="padding-top:56px">
  <div class="bar">
    <div><p class="eyebrow">Company directory</p><h2>Companies</h2>
      <p class="muted" style="margin:6px 0 0;font-size:15px">Profiles with the most verified data appear first. <span id="count"></span></p></div>
    <div class="ctl">
      <label for="sort">Sort</label>
      <select id="sort"><option value="data">Most data found</option><option value="name">Name A–Z</option></select>
      <label class="chk"><input id="only5" type="checkbox"/> All five areas</label>
    </div>
  </div>
  <div id="grid" class="grid">%%CARDS%%</div>
  <p id="empty" class="empty" hidden>No companies match this search.</p>
</section>
<section class="wrap">
  <div class="ask">
    <div><p class="eyebrow">Company research agent</p><h2>Ask Signalpost</h2>
      <p>Ask what the company does, who runs it, where it operates, how the latest filing looks, or whether it is hiring.</p></div>
    <div class="askrow">
      <label class="sr" for="who">Company</label><select id="who">%%OPTIONS%%</select>
      <label class="sr" for="ask">Your question</label><input id="ask" type="text" placeholder="Ask about this company" autocomplete="off"/>
      <button id="go" class="btn" type="button">Check the evidence</button>
    </div>
    <div class="chips">
      <button class="chip" type="button" data-topic="brief">Company brief</button>
      <button class="chip" type="button" data-topic="fin">Latest financials</button>
      <button class="chip" type="button" data-topic="who">Who runs it?</button>
      <button class="chip" type="button" data-topic="hiring">Working here</button>
      <button class="chip" type="button" data-topic="activity">Recent activity</button>
    </div>
    <div id="answer" class="answer" aria-live="polite"></div>
    <p style="font-size:14px">Every answer is limited to the sources shown on this profile. Missing evidence stays missing.</p>
  </div>
  <p class="foot">%%FOOT%%</p>
</section>
</main>
<script id="ask-data" type="application/json">%%ASKDATA%%</script>
<script>%%JS%%</script>
</body>
</html>
"""


def stat(big, label, small="") -> str:
    return f'<div class="card stat"><b>{esc(big)}</b><span>{esc(label)}</span>' + \
           (f"<small>{esc(small)}</small>" if small else "") + "</div>"


def build_stats(rows: list, report: dict | None) -> tuple[str, str, str]:
    n = len(rows)
    covs = [coverage(p) for p in rows]
    full = sum(all(c.values()) for c in covs)
    last = max((s.get("retrieved_at") or "" for p in rows
                for s in (p.get("synthesis") or {}).get("sources") or []), default="")
    stats = [stat(n, "companies in this sample", f"Checked {fmt_date((report or {}).get('completed_at') or last)}"),
             stat(full, "have data in all five categories", f"Evidence coverage: {full} / {n}")]
    foot = ""
    if report:
        v = report.get("validation") or {}
        checks = v.get("checks") or {}
        ops = report.get("operations") or {}
        stats.append(stat(f"{report.get('emitted_envelopes')} / {report.get('expected_count')}",
                          "envelopes emitted",
                          f"Validation {'passed' if v.get('passed') else 'FAILED'}: "
                          f"{sum(bool(x) for x in checks.values())} / {len(checks)} checks"))
        stats.append(stat(ops.get("requests", "—"), "requests this run",
                          f"p95 {ops.get('p95_ms', '—')} ms · runtime {ops.get('runtime_s', '—')} s"))
        reg = report.get("registry") or {}
        foot = (f"Run {esc(report.get('run_id'))} · registry snapshot sha256 "
                f"<span class='mono'>{esc(reg.get('registry_snapshot_sha256'))}</span> · "
                f"{esc(reg.get('registry_rows_scanned'))} registry rows scanned.")
    covtags = "".join(
        f'<span class="tag {"ok" if sum(c[k] for c in covs) == n and n else ""}">'
        f'{lbl} {sum(c[k] for c in covs)} / {n}</span>' for k, lbl in CATS)
    return "".join(stats), covtags, foot


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a static HTML viewer from envelopes or profiles")
    ap.add_argument("--input", required=True, help="envelopes.jsonl or profiles.jsonl")
    ap.add_argument("--output", required=True, help="viewer.html")
    ap.add_argument("--report", help="optional run report JSON (e.g. smoke-report.json)")
    ap.add_argument("--title", default="Signalpost — Norwegian company research")
    args = ap.parse_args()

    rows = []
    for line in Path(args.input).read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            rows.append(obj.get("profile") or obj)  # envelope or bare profile
    report = json.loads(Path(args.report).read_text(encoding="utf-8")) if args.report else None

    stats, covtags, foot = build_stats(rows, report)
    ask_data = {str(i): ask_entry(p) for i, p in enumerate(rows)}
    options = "".join(f'<option value="{i}">{esc(p.get("name"))}</option>' for i, p in enumerate(rows))

    page = (PAGE.replace("%%TITLE%%", esc(args.title)).replace("%%CSS%%", CSS)
            .replace("%%STATS%%", stats).replace("%%COVTAGS%%", covtags)
            .replace("%%CARDS%%", "\n".join(render_card(p) for p in rows))
            .replace("%%OPTIONS%%", options).replace("%%FOOT%%", foot)
            .replace("%%ASKDATA%%", script_json(ask_data)).replace("%%JS%%", JS))
    Path(args.output).write_text(page, encoding="utf-8")
    print(f"Wrote {args.output} ({len(rows)} companies)")


if __name__ == "__main__":
    main()