"""Show what the NAV feed really returns, so the cause of ads_indexed=0 is confirmed, not guessed.

Run next to nav_jobs.py:
    set SIGNALPOST_NAV_PUBLIC_TOKEN=1        (PowerShell: $env:SIGNALPOST_NAV_PUBLIC_TOKEN="1")
    python nav_diagnose.py 5                 (number of pages to read; default 5)
"""
import collections
import json
import sys
import urllib.parse

import importlib.util
import pathlib

# Always load the SAME nav_jobs.py the pipeline uses (src\norway_company_agent\nav_jobs.py), not a stray copy.
_here = pathlib.Path(__file__).resolve().parent
_path = _here / "src" / "norway_company_agent" / "nav_jobs.py"
if not _path.exists():
    _path = _here / "nav_jobs.py"
_spec = importlib.util.spec_from_file_location("nav_jobs", _path)
n = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(n)
print("using", _path)

pages = int(sys.argv[1]) if len(sys.argv) > 1 else 5
holder = {"token": n._token(30)}
url = n._base() + "/api/v1/feed"
status = collections.Counter()
total = 0
for i in range(pages):
    body, _ = n._get(url, 30, holder)
    items = body.get("items") or []
    total += len(items)
    if i == 0:
        print("top-level keys:", list(body))
        print("items on page 1:", len(items))
        if items:
            print("item keys:", list(items[0]))
            print("sample _feed_entry:", json.dumps(items[0].get("_feed_entry"), ensure_ascii=False)[:600])
    for it in items:
        status[str((it.get("_feed_entry") or {}).get("status"))] += 1
    dates = [(it.get("_feed_entry") or {}).get("sistEndret") or it.get("date_modified") for it in items]
    print(f"page {i + 1}: n={len(items)} first={dates[0] if dates else None} last={dates[-1] if dates else None}")
    nxt = body.get("next_url")
    if not nxt:
        print("feed ended (no next_url)")
        break
    url = urllib.parse.urljoin(n._base() + "/", nxt)
print(f"\nread {total} entries; status counts: {dict(status)}")