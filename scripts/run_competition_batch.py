#!/usr/bin/env python3
"""Run the Signalpost company research agent on a batch of Norwegian organisation numbers.

One command, one terminal result envelope per input company:

    uv run python scripts/run_competition_batch.py --organisations batch.jsonl --output out/envelopes.jsonl

Everything else is optional (see --help). The run never aborts because of a single bad company, a missing
bulk file or an exhausted time budget: every input row still gets an envelope.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from norway_company_agent import llm_synthesis, nav_jobs  # noqa: E402
from norway_company_agent.batch import (  # noqa: E402
    apply_live_registry,
    invalid_input_profile,
    profile_complete_for_modules,
    profiles_from_bulk,
    profiles_without_bulk,
    read_organisation_inputs,
    terminal_envelope,
    validate_envelopes,
)
from norway_company_agent.evidence import evidence as make_evidence, utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.llm_synthesis import get_cost_stats, set_batch_size, synthesise  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.site_activity import ensure_hiring_placeholder, extract_public_activity  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402

DEFAULT_MODULES = "registry,accounting_obligation,registry_live,financials,roles,group,locations,website,hiring,public_activity"
LOCAL_MODULES = {"registry", "accounting_obligation", "website", "hiring", "public_activity"}
GRACE_SECONDS = 90.0  # how long in-flight companies may overrun the time budget before they are cut off


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signalpost batch run: one terminal envelope per organisation number")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL or text file with the organisation numbers to research")
    parser.add_argument("--output", default="out/envelopes.jsonl", help="terminal envelope JSONL (default: out/envelopes.jsonl)")
    parser.add_argument("--profiles-output", help="full profiles JSONL (default: <output dir>/profiles.jsonl)")
    parser.add_argument("--report", help="run report JSON (default: <output dir>/report.json)")
    parser.add_argument("--run-id", help="run identifier (default: run-<UTC timestamp>)")
    parser.add_argument("--bulk", help="optional Brreg bulk snapshot (csv or csv.gz); without it every company is read from the live Brreg API")
    parser.add_argument("--expected-count", type=int, help="fail validation unless exactly this many companies were supplied (default: no check beyond the input itself)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=200)
    parser.add_argument("--resume", action="store_true", help="continue an interrupted run from the profiles file")
    parser.add_argument("--modules", default=DEFAULT_MODULES, help="comma-separated modules to run")
    parser.add_argument("--time-budget-s", type=float, default=None,
                        help="wall-clock budget in seconds (env SIGNALPOST_TIME_BUDGET_S). When it is used up, companies not yet "
                             "processed receive a terminal `budget_exhausted` envelope instead of being dropped")
    args = parser.parse_args(argv)
    out_dir = Path(args.output).parent
    args.profiles_output = args.profiles_output or str(out_dir / "profiles.jsonl")
    args.report = args.report or str(out_dir / "report.json")
    args.run_id = args.run_id or "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.time_budget_s is None and os.getenv("SIGNALPOST_TIME_BUDGET_S", "").strip():
        try:
            args.time_budget_s = float(os.environ["SIGNALPOST_TIME_BUDGET_S"])
        except ValueError:
            pass
    args.workers = max(1, args.workers)
    args.checkpoint_every = max(1, args.checkpoint_every)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started_at = utc_now()
    run_started = time.monotonic()
    deadline = run_started + args.time_budget_s if args.time_budget_s else None
    nav_jobs.set_deadline(deadline)
    llm_synthesis.reset_state()

    organisation_inputs = read_organisation_inputs(args.organisations, strict=False)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    invalid = {item["organisation_number"] for item in organisation_inputs if item.get("invalid_input")}
    expected_count = args.expected_count if args.expected_count is not None else len(orgs)
    set_batch_size(len(orgs))

    # --- initial profiles: bulk snapshot when given and readable, otherwise the live API --------------------
    notes: list[str] = []
    valid_orgs = [org for org in orgs if org not in invalid]
    registry_metadata: dict
    if args.bulk:
        try:
            profiles_by_org, registry_metadata = {}, {}
            profiles, registry_metadata = profiles_from_bulk(args.bulk, valid_orgs)
            profiles_by_org = {p["organisation_number"]: p for p in profiles}
            registry_metadata["registry_source"] = "bulk_snapshot"
        except Exception as exc:  # noqa: BLE001 - a bad bulk file must not cost the whole batch
            notes.append(f"bulk snapshot unusable ({type(exc).__name__}: {str(exc)[:120]}); using the live Brreg API")
            profiles, registry_metadata = profiles_without_bulk(valid_orgs)
            profiles_by_org = {p["organisation_number"]: p for p in profiles}
    else:
        profiles, registry_metadata = profiles_without_bulk(valid_orgs)
        profiles_by_org = {p["organisation_number"]: p for p in profiles}
    profiles_by_org.update({org: invalid_input_profile(org) for org in invalid})
    profiles = [profiles_by_org[org] for org in orgs]

    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations.get(profile["organisation_number"], {}):
                profile[key] = annotations[profile["organisation_number"]][key]

    requested_modules = [m.strip() for m in args.modules.split(",") if m.strip()]
    fetch_modules = set(requested_modules) - LOCAL_MODULES
    operations: dict = {"requests": 0, "bytes": 0, "latencies_ms": []}

    def out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def record_failure(profile: dict, module: str, exc: BaseException) -> None:
        """A step crashed: keep the row, mark the module as a terminal source_error."""
        message = f"{type(exc).__name__}: {exc}"[:200]
        profile.setdefault("evidence", {})[module] = make_evidence(
            module, "source_error", "pipeline_error", "urn:signalpost:pipeline", note=message
        )
        profile.setdefault("run_errors", []).append({"module": module, "message": message})

    def mark_unfinished(profile: dict, status: str, note: str) -> None:
        """Give every requested module that has no result yet a terminal status."""
        evidence_map = profile.setdefault("evidence", {})
        for module in set(requested_modules) | {"registry", "accounting_obligation"}:
            current = evidence_map.get(module)
            if current is None or current.get("status") == "not_fetched":
                evidence_map[module] = make_evidence(module, status, "pipeline_budget" if status == "budget_exhausted" else "input_validation",
                                                     "urn:signalpost:pipeline", note=note)
        if profile.get("evidence", {}).get("registry", {}).get("status") == "budget_exhausted":
            profile.setdefault("run_errors", []).append({"module": "registry", "message": note})

    def finish(profile: dict, started: float, metrics: list, website_metrics: dict) -> tuple[dict, dict]:
        profile["synthesis"] = synthesise(profile, allow_llm=not out_of_time())
        metric = {
            "requests": len(metrics) + website_metrics["requests"],
            "bytes": sum(getattr(item, "bytes_received", 0) for item in metrics) + website_metrics["bytes"],
            "latencies_ms": [getattr(item, "elapsed_ms", 0) for item in metrics] + website_metrics["latencies_ms"],
        }
        profile["run_metrics"] = {**metric, "runtime_ms": int((time.monotonic() - started) * 1000)}
        return profile, metric

    def enrich(profile: dict) -> tuple[dict, dict]:
        started = time.monotonic()
        profile.setdefault("evidence", {})
        metrics: list = []
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}

        if profile.get("invalid_input"):
            mark_unfinished(profile, "not_applicable", "The input line is not a valid organisation number")
            return finish(profile, started, metrics, website_metrics)
        if out_of_time():
            mark_unfinished(profile, "budget_exhausted", "Run time budget reached before this company was processed")
            return finish(profile, started, metrics, website_metrics)

        # 1. Official Brreg modules (the live entity record is always read when the profile still needs it)
        needs_live = (profile["evidence"].get("registry") or {}).get("status") != "available"
        wanted = set(fetch_modules) | ({"registry_live"} if needs_live else set())
        try:
            records, metrics = fetch_official_modules(profile["organisation_number"], wanted, keep_raw=True)
            profile["evidence"].update(records)
        except Exception as exc:  # noqa: BLE001 - one bad company must not kill the batch
            for module in sorted(wanted):
                if module not in profile["evidence"]:
                    record_failure(profile, module, exc)
        try:
            apply_live_registry(profile)  # no bulk row (or no bulk file): name the company from the live record
        except Exception as exc:  # noqa: BLE001
            record_failure(profile, "registry", exc)
        if "registry_live" not in requested_modules:
            profile["evidence"].pop("registry_live", None)

        # 2. Website (registry `hjemmeside` only) + exact-entity identity gate
        if "website" in requested_modules:
            if out_of_time():
                mark_unfinished(profile, "budget_exhausted", "Run time budget reached before the website step")
            else:
                try:
                    website_record, website_metrics = fetch_website(profile.get("website"))
                    gated = apply_website_identity_gate(profile, website_record)
                    profile["evidence"]["website"] = gated.get("website", website_record)
                except Exception as exc:  # noqa: BLE001
                    website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
                    record_failure(profile, "website", exc)

        # 3. Public activity: only from a website that passed the identity gate
        if "public_activity" in requested_modules and "public_activity" not in profile["evidence"]:
            try:
                profile["evidence"]["public_activity"] = extract_public_activity(profile)
            except Exception as exc:  # noqa: BLE001
                record_failure(profile, "public_activity", exc)

        # 4. Hiring: verified website and/or the NAV feed (orgnr-confirmed)
        if "hiring" in requested_modules and "hiring" not in profile["evidence"]:
            if out_of_time():
                mark_unfinished(profile, "budget_exhausted", "Run time budget reached before the hiring step")
            else:
                try:
                    profile["evidence"]["hiring"] = ensure_hiring_placeholder(profile)
                except Exception as exc:  # noqa: BLE001
                    record_failure(profile, "hiring", exc)

        # 5. Evidence-grounded synthesis (LLM optional; never raises; budget enforced inside)
        return finish(profile, started, metrics, website_metrics)

    # --- resume ------------------------------------------------------------------------------------------
    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        try:
            prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
            in_batch = [item for item in prior if item.get("organisation_number") in set(orgs)]
            if len(in_batch) != len(prior):
                notes.append(f"resume file held {len(prior) - len(in_batch)} profile(s) from another batch; ignored")
            state = {item["organisation_number"]: item for item in in_batch if profile_complete_for_modules(item, requested_modules)}
        except (OSError, ValueError, KeyError) as exc:
            notes.append(f"resume file unreadable ({type(exc).__name__}); starting from scratch")
        resumed_profiles = len(state)

    pending_profiles = [p for p in profiles if p["organisation_number"] not in state]

    # --- run ---------------------------------------------------------------------------------------------
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(enrich, profile): profile for profile in pending_profiles}
    outstanding = set(futures)
    completed = 0
    abandoned = 0
    hard_stop = (deadline + GRACE_SECONDS) if deadline else None
    try:
        while outstanding:
            timeout = None if hard_stop is None else max(0.0, hard_stop - time.monotonic())
            done, outstanding = wait(outstanding, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:  # grace period over: stop waiting for stuck companies
                break
            for future in done:
                completed += 1
                try:
                    profile, metric = future.result()
                except Exception as exc:  # noqa: BLE001 - last line of defence, the row is still emitted
                    profile = futures[future]
                    for module in requested_modules:
                        if module not in profile.get("evidence", {}):
                            record_failure(profile, module, exc)
                    profile.setdefault("synthesis", synthesise(profile))
                    metric = {"requests": 0, "bytes": 0, "latencies_ms": []}
                state[profile["organisation_number"]] = profile
                operations["requests"] += metric["requests"]
                operations["bytes"] += metric["bytes"]
                operations["latencies_ms"].extend(metric["latencies_ms"])
                if completed % args.checkpoint_every == 0:
                    write_jsonl(profiles_output, [state[org] for org in orgs if org in state])
    finally:
        for future in outstanding:  # companies still running or queued after the hard stop
            future.cancel()
            profile = futures[future]
            abandoned += 1
            try:
                snapshot = copy.deepcopy(profile)
            except Exception:  # noqa: BLE001 - the worker was mutating it; fall back to what is certain
                snapshot = {"organisation_number": profile["organisation_number"], "evidence": {}}
            mark_unfinished(snapshot, "budget_exhausted", "Processing was cut off when the run time budget ran out")
            snapshot["synthesis"] = synthesise(snapshot, allow_llm=False)
            state[snapshot["organisation_number"]] = snapshot
        pool.shutdown(wait=False, cancel_futures=True)

    # --- outputs -----------------------------------------------------------------------------------------
    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)

    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None

    nav_stats = nav_jobs.get_stats()
    llm_stats = get_cost_stats()
    operations["requests"] += nav_stats["requests"]
    operations["third_party_cost_usd"] = llm_stats["estimated_cost_usd"]
    operations["runtime_s"] = round(time.monotonic() - run_started, 1)
    exhausted = sum(1 for e in envelopes if any(m.get("state") == "budget_exhausted" for m in e["modules"].values()))

    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "input_rows": len(orgs),
        "expected_count": expected_count,
        "emitted_envelopes": len(envelopes),
        "invalid_inputs": sorted(invalid),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "time_budget_s": args.time_budget_s,
        "companies_with_budget_exhausted_modules": exhausted,
        "companies_cut_off": abandoned,
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
        "llm": llm_stats,
        "nav_job_feed": nav_stats,
        "notes": notes,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("run_id", "input_rows", "emitted_envelopes", "validation", "operations", "notes")}, indent=2))
    code = 0 if validation["passed"] else 1
    if abandoned:  # worker threads may still be blocked in a network call: do not wait for them
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
