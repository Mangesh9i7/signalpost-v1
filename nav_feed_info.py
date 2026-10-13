"""Print the NAV feed's own description fields (not the ads). Run once; paste the output back.

    PowerShell:  $env:SIGNALPOST_NAV_PUBLIC_TOKEN = "1"
                 python nav_feed_info.py
"""
import importlib.util
import json
import pathlib

_here = pathlib.Path(__file__).resolve().parent
_path = _here / "src" / "norway_company_agent" / "nav_jobs.py"
if not _path.exists():
    _path = _here / "nav_jobs.py"
_spec = importlib.util.spec_from_file_location("nav_jobs", _path)
n = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(n)

holder = {"token": n._token(30)}
body, headers = n._get(n._base() + "/api/v1/feed", 30, holder)
info = {k: v for k, v in body.items() if k != "items"}
print(json.dumps(info, indent=2, ensure_ascii=False)[:3000])
print("\nresponse headers of interest:",
      {k: v for k, v in headers.items() if k.lower() in ("content-encoding", "content-length", "content-type", "link", "x-ratelimit-remaining")})
