"""One-time (and occasional) full read of the NAV feed into the local cache. Run before the pipeline:

    PowerShell:  $env:SIGNALPOST_NAV_PUBLIC_TOKEN = "1"
                 python nav_prefetch.py

Safe to stop (Ctrl+C) and re-run: it resumes from the saved position. Later pipeline runs only read new changes.
"""
import logging
import os

os.environ.setdefault("SIGNALPOST_NAV_BUDGET_S", "86400")  # no time limit for the prefetch
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
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

n.reset()
index = n._build_index()
print("stats:", n.get_stats())
print("cache file:", n._cache_path().resolve())
print("RESULT:", "partial, run again" if index["error"] else "feed fully read")