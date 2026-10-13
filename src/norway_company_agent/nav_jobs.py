from __future__ import annotations

import email.utils
import gzip
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("signalpost.nav")

_lock = threading.Lock()
_build_guard = threading.Lock()  # serialise the first-time index build across worker threads
_index: dict[str, Any] | None = None
_failed: tuple[float, dict[str, Any]] | None = None  # (monotonic time, failed index): retried after a cooldown
_FAIL_COOLDOWN_S = 30
# If the feed could not be reached at all (0 pages read), retrying inside the same run only stalls every worker
# again (one failed build costs ~2 minutes of timeouts). Do not retry for this long.
_UNREACHABLE_COOLDOWN_S = 3600
_deadline: float | None = None  # time.monotonic() value after which no further feed pages are read
_stats = {"requests": 0, "pages_read": 0, "ads_indexed": 0, "detail_fetches": 0, "matches": 0, "error": None}
_LEGAL_SUFFIX = {"as", "asa", "ans", "da", "enk", "sa", "ks", "nuf", "norge", "norway", "ba", "stiftelse", "foretak"}


def _base() -> str:
    return os.getenv("SIGNALPOST_NAV_BASE_URL", "https://pam-stilling-feed.nav.no").rstrip("/")


def enabled() -> bool:
    return bool(os.getenv("SIGNALPOST_NAV_TOKEN", "").strip()) or os.getenv("SIGNALPOST_NAV_PUBLIC_TOKEN", "") == "1"


def get_stats() -> dict[str, Any]:
    with _lock:
        return {**_stats, "enabled": enabled()}


def set_deadline(monotonic_deadline: float | None) -> None:
    """Let the run's overall time budget cap the (otherwise generous) feed-reading budget."""
    global _deadline
    with _lock:
        _deadline = monotonic_deadline


def reset() -> None:
    global _index, _failed, _deadline
    with _lock:
        _index = None
        _failed = None
        _deadline = None
        _stats.update(requests=0, pages_read=0, ads_indexed=0, detail_fetches=0, matches=0, error=None)


def normalise_name(name: Any) -> str:
    text = unicodedata.normalize("NFKC", str(name or "")).casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    words = [w for w in text.split() if w]
    while words and words[-1] in _LEGAL_SUFFIX:
        words.pop()
    return " ".join(words)


def _token(timeout: float) -> str:
    token = os.getenv("SIGNALPOST_NAV_TOKEN", "").strip()
    if token:
        return token
    last: Exception | None = None
    for attempt in range(3):  # the public-token endpoint can time out once in a while: retry before giving up
        with _lock:
            _stats["requests"] += 1
        request = urllib.request.Request(_base() + "/api/publicToken", headers={"User-Agent": "builderr-signalpost-poc/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=min(timeout, 20)) as response:
                raw_text = response.read().decode("utf-8").strip()
            match = re.search(r"(eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+)", raw_text)
            return match.group(1) if match else raw_text.strip('"').strip("'")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        time.sleep(2 ** attempt)
    assert last is not None
    raise last


def _nap(seconds: float, give_up_at: float | None) -> None:
    """Sleep, but never past `give_up_at` (a time.monotonic() value)."""
    if give_up_at is not None:
        seconds = min(seconds, max(0.0, give_up_at - time.monotonic()))
    if seconds > 0:
        time.sleep(seconds)


def _get(url: str, timeout: float, holder: dict[str, str], extra: dict[str, str] | None = None,
         give_up_at: float | None = None) -> tuple[Any, dict[str, str]]:
    """GET with retries. Retries timeouts/429/5xx with backoff; refreshes the public token once on 401/403.

    `holder` is {"token": ...}; it is updated in place when the token is refreshed.
    """
    last: Exception | None = None
    refreshed = False
    for attempt in range(4):
        if attempt and give_up_at is not None and time.monotonic() >= give_up_at:
            break  # the build's time budget is used up: stop retrying
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {holder['token']}", "Accept": "application/json", "Accept-Encoding": "gzip",
                     "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)", **(extra or {})},
        )
        with _lock:
            _stats["requests"] += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)  # pages are large JSON; compression makes the full read much faster
                return json.loads(raw.decode("utf-8")), dict(response.headers)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403) and not refreshed and not os.getenv("SIGNALPOST_NAV_TOKEN", "").strip():
                holder["token"] = _token(timeout)  # public tokens expire; get a fresh one once
                refreshed = True
                continue
            if exc.code in (429, 500, 502, 503, 504):
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                _nap(min(delay, 30), give_up_at)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            _nap(2 ** attempt, give_up_at)
    assert last is not None
    raise last


def _find(node: Any, key: str) -> Any:
    """First value stored under `key` anywhere in a nested JSON structure (robust to wrapper names)."""
    if isinstance(node, dict):
        if key in node and node[key] not in (None, ""):
            return node[key]
        for child in node.values():
            found = _find(child, key)
            if found not in (None, ""):
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find(child, key)
            if found not in (None, ""):
                return found
    return None


def _employer_orgnr(detail: Any) -> Any:
    """The advertiser's organisation number: `employer.orgnr` first, any other `orgnr` only as a last resort."""
    employer = _find(detail, "employer")
    if isinstance(employer, dict) and employer.get("orgnr") not in (None, ""):
        return employer["orgnr"]
    if isinstance(employer, dict):
        return None  # an employer block without an orgnr is unconfirmed; never borrow another field
    return _find(detail, "orgnr")


def _cache_path() -> Path:
    return Path(os.getenv("SIGNALPOST_NAV_CACHE", ".signalpost_nav_cache.json"))


def _start_days() -> int:
    """How far back to read. NAV: an ad can never be active for more than 6 months, so ~30 days is complete."""
    return int(os.getenv("SIGNALPOST_NAV_START_DAYS", "30"))


def _load_cache(start_days: int) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        if data.get("v") == 2 and data.get("base") == _base() and data.get("start_days") == start_days and data.get("resume_url"):
            cutoff = (date.today() - timedelta(days=start_days)).isoformat()
            data["active"] = {  # an ad cannot stay active longer than the window, so older entries are stale
                k: v for k, v in (data.get("active") or {}).items()
                if str((v.get("_feed_entry") or {}).get("sistEndret") or "9999")[:10] >= cutoff
            }
            return data
    except Exception:  # noqa: BLE001 - a missing, old-format or corrupt cache just means a fresh read
        pass
    return None


def _save_cache(active: dict[str, Any], resume_url: str, start_days: int) -> None:
    path = _cache_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps({"v": 2, "base": _base(), "start_days": start_days, "resume_url": resume_url, "active": active}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not save NAV cache: %s", exc)


def _build_index() -> dict[str, Any]:
    """Read the NAV feed (oldest first) and keep only currently ACTIVE ads, indexed by normalised employer name.

    The first request uses NAV's documented If-Modified-Since header to start 1 month back (ads cannot be active longer). Progress is saved to a
    cache file (resume_url + active ads), so later runs only read what changed since. A cap, time budget or error
    marks the index partial via index["error"], so lookups never report "no postings" from an incomplete read.
    """
    timeout = float(os.getenv("SIGNALPOST_NAV_TIMEOUT", "100"))
    max_pages = int(os.getenv("SIGNALPOST_NAV_MAX_PAGES", "500"))
    budget = float(os.getenv("SIGNALPOST_NAV_BUDGET_S", "300"))
    with _lock:
        if _deadline is not None:
            budget = max(0.0, min(budget, _deadline - time.monotonic()))
    index: dict[str, Any] = {"by_name": {}, "holder": None, "error": None, "timeout": timeout}
    start_days = _start_days()
    cache = _load_cache(start_days)
    active: dict[str, Any] = dict(cache["active"]) if cache else {}
    start_url = cache["resume_url"] if cache else _base() + "/api/v1/feed"
    # Documented by NAV: If-Modified-Since on the first request starts the feed at that time (RFC 1123 date).
    first_headers: dict[str, str] | None = None if cache else {
        "If-Modified-Since": email.utils.formatdate((datetime.now(timezone.utc) - timedelta(days=start_days)).timestamp(), usegmt=True)}
    resume = start_url
    pages = entries = 0
    statuses: Counter = Counter()
    complete = False
    started = time.monotonic()
    stage = "token request"
    try:
        index["holder"] = {"token": _token(timeout)}
        stage = "feed page"
        url: str | None = start_url
        while url and pages < max_pages:
            if time.monotonic() - started > budget:
                index["error"] = f"partial index: time budget {budget:.0f}s reached after {pages} pages (progress saved, run again)"
                break
            body, _ = _get(url, timeout, index["holder"], first_headers, give_up_at=started + budget)
            first_headers = None  # only the first request carries the start time; the rest follow next_url
            pages += 1
            items = body.get("items") or []
            if pages == 1 and items:
                log.info("NAV feed: first page covers %s .. %s", (items[0].get("_feed_entry") or {}).get("sistEndret"),
                         (items[-1].get("_feed_entry") or {}).get("sistEndret"))
            for item in items:
                entry = item.get("_feed_entry") or {}
                uuid = str(entry.get("uuid") or item.get("id") or "")
                if not uuid:
                    continue
                status = str(entry.get("status") or "").upper()
                statuses[status or "MISSING"] += 1
                if status == "ACTIVE":  # later entries replace earlier ones
                    active[uuid] = {"id": item.get("id"), "url": item.get("url"), "title": item.get("title"), "_feed_entry": entry}
                else:
                    active.pop(uuid, None)  # no longer active
            entries += len(items)
            nxt = body.get("next_url")
            resume = urllib.parse.urljoin(_base() + "/", nxt) if nxt and items else url
            with _lock:
                _stats["pages_read"] = pages
            if pages % 25 == 0:
                _save_cache(active, resume, start_days)
                log.info("NAV feed: pages=%s entries=%s active=%s at=%s rate=%.0f entries/s", pages, entries, len(active),
                         (items[-1].get("_feed_entry") or {}).get("sistEndret") if items else None,
                         entries / max(time.monotonic() - started, 1))
            if not nxt or not items:
                complete = True
                break
            url = resume
        if not complete and not index["error"]:
            index["error"] = f"partial index: page cap {max_pages} reached, feed not fully read (progress saved, run again)"
        if complete and start_days < 183 and not index["error"]:
            index["error"] = f"partial index: only the last {start_days} days of the feed were read (an ad can be active for up to 6 months)"
    except Exception as exc:  # noqa: BLE001 - never fail the run because of an optional source
        index["error"] = f"partial index after {pages} pages: {type(exc).__name__}: {str(exc)[:120]}" + (" (progress saved)" if pages else "") + f" [stage: {stage}]"
    if pages:
        _save_cache(active, resume, start_days)

    index["pages"] = pages
    for uuid, ad in active.items():
        name = normalise_name(ad["_feed_entry"].get("businessName"))
        if name:
            index["by_name"].setdefault(name, []).append({"uuid": uuid, "url": ad.get("url"), "item": ad})
    indexed = sum(len(v) for v in index["by_name"].values())
    log.info("NAV feed: pages=%s entries=%s statuses=%s active_indexed=%s resumed=%s error=%s",
             pages, entries, dict(statuses), indexed, bool(cache), index["error"])
    with _lock:
        _stats["ads_indexed"] = indexed
        _stats["error"] = index["error"]
    return index


def _ensure_index() -> dict[str, Any]:
    global _index, _failed
    with _lock:
        if _index is not None:
            return _index
        if _failed:
            cooldown = _FAIL_COOLDOWN_S if _failed[1].get("pages") else _UNREACHABLE_COOLDOWN_S
            if time.monotonic() - _failed[0] < cooldown:
                return _failed[1]  # failed recently: do not hammer the feed, retry after the cooldown
    built = _build_index()  # callers hold _build_guard, so only one thread builds
    with _lock:
        if built["error"] and not built["by_name"]:
            _failed = (time.monotonic(), built)  # never cache a failed build as the final answer
        else:
            _index, _failed = built, None
        return built


def lookup(organisation_number: str, legal_name: str | None) -> dict[str, Any]:
   
    result: dict[str, Any] = {"ads": [], "error": None, "source_url": _base() + "/api/v1/feed"}
    if not enabled():
        result["error"] = "NAV feed not enabled"
        return result
    if not legal_name:
        return result
    with _build_guard:
        index = _ensure_index()
    if index.get("error") and not index["by_name"]:
        result["error"] = index["error"]
        return result
    wanted = re.sub(r"\D", "", str(organisation_number))
    for candidate in (index["by_name"].get(normalise_name(legal_name)) or [])[:8]:
        detail = candidate["item"]
        orgnr = _employer_orgnr(detail)
        if orgnr is None and candidate.get("url"):
            try:
                with _lock:
                    _stats["detail_fetches"] += 1
                detail, _ = _get(urllib.parse.urljoin(_base() + "/", str(candidate["url"])), index["timeout"], index["holder"])
                orgnr = _employer_orgnr(detail)
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
                continue
        if re.sub(r"\D", "", str(orgnr or "")) != wanted:
            continue  # same name, different company: never publish
        expires = str(_find(detail, "expires") or "")[:10]
        if expires and expires < date.today().isoformat():
            continue  # expired, even if the feed has not marked it inactive yet
        title = _find(detail, "title") or (candidate["item"].get("title"))
        link = _find(detail, "link") or _find(detail, "sourceurl") or urllib.parse.urljoin(_base() + "/", str(candidate.get("url") or ""))
        place = _find(detail, "city") or _find(detail, "municipal")
        result["ads"].append({
            "title": str(title or "")[:200],
            "url": link,
            "date_posted": str(_find(detail, "published") or "")[:10] or None,
            "valid_through": str(_find(detail, "expires") or "")[:10] or None,
            "location": place,
            "hiring_organisation": (_find(detail, "employer") or {}).get("name") if isinstance(_find(detail, "employer"), dict) else None,
            "method": "NAV Arbeidsplassen feed (orgnr-confirmed)",
        })
        with _lock:
            _stats["matches"] += 1
    if result["ads"]:
        result["error"] = None
    elif index.get("error"):
        result["error"] = index["error"]  # nothing found, but the feed was only partly read
    return result