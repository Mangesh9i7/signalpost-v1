from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .llm_synthesis import synthesise
from .official import ACCOUNTING_OBLIGATION_SOURCE, BRREG_ENTITY, accounting_obligation_assessment
from .sampling import iter_bulk, sha256_file

# Allowed result states of the challenge, used for every claim `availability`
BRREG_ENTITY_URL = BRREG_ENTITY
AVAILABILITY_STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
# Registry columns holding contact details. They are not needed for any claim, and for sole proprietors
# they are personal data, so they are dropped before the row is stored as evidence.
CONTACT_FIELDS = {"epostadresse", "telefon", "mobil", "telefaks", "Epostadresse", "Telefon", "Mobil"}


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
    # Official Signalpost availability states
    "not_available",
    "ambiguous",
    "failed",
}


def read_organisation_inputs(path: str | Path, *, strict: bool = True) -> list[dict[str, Any]]:
    """Read the batch Builderr supplies (txt, json or jsonl).

    strict=True (library default) raises on a malformed or duplicated number. The runner uses strict=False so
    that one bad line can never abort a whole batch: an unusable entry becomes a record flagged
    `invalid_input`, which still receives a terminal `failed` envelope, and duplicates collapse to one.
    """
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    seen: set[str] = set()
    for value in values:
        raw = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(raw or "") if character.isdigit())
        invalid = len(org) != 9
        if invalid:
            if strict:
                raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
            org = org or str(raw or "").strip()[:40] or "UNKNOWN"
        if org in seen:
            if strict:
                raise ValueError("Organisation-number input contains duplicates")
            continue
        seen.add(org)
        record = {"organisation_number": org}
        if invalid:
            record["invalid_input"] = True
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def _stub_profile(org: str, registry_record: dict[str, Any], accounting_record: dict[str, Any]) -> dict[str, Any]:
    """A profile with nothing known yet. Unknown is None, never False/0/""."""
    return {
        "organisation_number": org,
        "name": None,
        "legal_form": None,
        "employees": None,
        "bankrupt": None,
        "liquidating": None,
        "municipality": None,
        "industry_code": None,
        "industry_label": None,
        "website": None,
        "latest_submitted_accounts": None,
        "evidence": {"registry": registry_record, "accounting_obligation": accounting_record},
    }


def invalid_input_profile(org: str) -> dict[str, Any]:
    """Terminal profile for an input line that is not a usable organisation number (no network is used)."""
    note = "The supplied value is not a 9-digit Norwegian organisation number"
    profile = _stub_profile(
        org,
        evidence("registry", "failed", "input_validation", "urn:signalpost:input", note=note),
        evidence("accounting_obligation", "not_applicable", "input_validation", "urn:signalpost:input", note=note),
    )
    profile["invalid_input"] = True
    return profile


def profiles_without_bulk(organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Profiles that are filled from the live Brreg API later (no bulk file needed)."""
    requested = list(organisation_numbers)
    profiles = [
        _stub_profile(
            org,
            evidence("registry", "not_fetched", "official_registry_live", BRREG_ENTITY_URL.format(org=org),
                     note="Filled from the live registry API"),
            evidence("accounting_obligation", "not_fetched", "official_rule_interpretation", ACCOUNTING_OBLIGATION_SOURCE),
        )
        for org in requested
    ]
    return profiles, {"registry_snapshot_sha256": None, "registry_source": "live_api", "requested": len(requested),
                      "selected": 0, "missing_from_snapshot": [], "registry_rows_scanned": 0}


def apply_live_registry(profile: dict[str, Any]) -> dict[str, Any]:
    """Fill a profile from the live Brreg record when the bulk snapshot had no row (or no bulk was used).

    The `registry` evidence then holds the live JSON body (without contact details), so employees and website
    can be audited against the very same source. A profile whose registry evidence is already available is
    left untouched.
    """
    evidence_map = profile.setdefault("evidence", {})
    if (evidence_map.get("registry") or {}).get("status") == "available":
        evidence_map.get("registry_live", {}).pop("raw_body", None)
        return profile
    live = evidence_map.get("registry_live") or {}
    raw_body = live.pop("raw_body", None)
    if live.get("status") == "available" and isinstance(raw_body, dict):
        value = live.get("value") or {}
        code = (value.get("industry") or {}).get("kode")
        address = value.get("business_address") or {}
        updates = {
            "name": value.get("name"), "legal_form": value.get("legal_form"), "employees": value.get("employees"),
            "bankrupt": value.get("bankrupt"), "liquidating": value.get("liquidating"),
            "website": value.get("website") or None, "latest_submitted_accounts": value.get("latest_submitted_accounts"),
            "municipality": address.get("kommune"), "municipality_number": address.get("kommunenummer"),
            "industry_code": code, "industry_label": (value.get("industry") or {}).get("beskrivelse"),
        }
        for key, new in updates.items():
            if new not in (None, ""):
                profile[key] = new
        body = {k: v for k, v in raw_body.items() if k not in CONTACT_FIELDS}
        evidence_map["registry"] = evidence(
            "registry", "available", "official_registry_live", live.get("source_url") or BRREG_ENTITY_URL.format(org=profile["organisation_number"]),
            value=body, retrieved_at=live.get("retrieved_at"), content_sha256=live.get("content_sha256"),
            source_row_key=profile["organisation_number"], note="Live Brreg record (no bulk snapshot row)",
        )
        evidence_map["accounting_obligation"] = accounting_obligation_assessment(profile)
    elif (evidence_map.get("registry") or {}).get("status") == "not_fetched":
        status = "not_found" if live.get("status") == "not_found" else "source_error"
        evidence_map["registry"] = evidence(
            "registry", status, "official_registry_live", BRREG_ENTITY_URL.format(org=profile["organisation_number"]),
            note=live.get("note") or "The live registry record could not be read", retrieved_at=live.get("retrieved_at"),
        )
        evidence_map["accounting_obligation"] = evidence(
            "accounting_obligation", "not_applicable", "official_rule_interpretation", ACCOUNTING_OBLIGATION_SOURCE,
            note="No registry record, so the accounting obligation cannot be assessed")
    return profile


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_sha256 = sha256_file(path)
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = {k: v for k, v in profile.pop("raw", {}).items() if k not in CONTACT_FIELDS}
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    for org in missing:
        # A company absent from the frozen snapshot must still get a terminal envelope.
        found[org] = {
            "organisation_number": org,
            "name": None,
            "legal_form": None,
            "employees": None,
            "bankrupt": None,
            "liquidating": None,
            "municipality": None,
            "industry_code": None,
            "industry_label": None,
            "website": None,
            "latest_submitted_accounts": None,
            "evidence": {
                "registry": evidence(
                    "registry",
                    "not_found",
                    "official_registry_bulk",
                    "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    note="Organisation number not present in the registry snapshot",
                    retrieved_at=retrieved_at,
                    content_sha256=snapshot_sha256,
                    source_row_key=org,
                ),
                "accounting_obligation": evidence(
                    "accounting_obligation",
                    "not_applicable",
                    "official_rule_interpretation",
                    ACCOUNTING_OBLIGATION_SOURCE,
                    note="No registry row in the snapshot, so the accounting obligation cannot be assessed",
                    retrieved_at=retrieved_at,
                ),
            },
        }
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(requested) - len(missing),
        "missing_from_snapshot": missing[:50],
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    if status == "budget_exhausted":
        return "budget_exhausted"
    if status in {"not_available", "ambiguous", "failed"}:
        return status
    return "submission_error"


_AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "not_available": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "ambiguous": "ambiguous",
    "source_error": "failed",
    "budget_exhausted": "failed",
    "failed": "failed",
}


def _availability(record: dict[str, Any] | None) -> str:
    return _AVAILABILITY.get((record or {}).get("status"), "failed")


def _ev_id(field: str) -> str:
    return f"ev-{field}"


def _span(field: str, record: dict[str, Any], profile: dict[str, Any]) -> str:
    value = record.get("value") or {}
    if field == "registry":
        return f"{profile.get('name')}, organisation number {profile.get('organisation_number')}"[:200]
    if field == "financials":
        rows = value.get("records") or [] if isinstance(value, dict) else []
        period = (rows[0].get("period") or {}) if rows and isinstance(rows[0], dict) else {}
        return f"Annual accounts, period {period.get('fraDato')} to {period.get('tilDato')}"[:200] if period else "Annual accounts register response"
    if field == "website":
        return str(value.get("title") or value.get("description") or record.get("note") or record.get("source_url") or "")[:200]
    if value:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)[:200]
        except (TypeError, ValueError):
            pass
    return str(record.get("note") or f"{field}: {record.get('status')}")[:200]


def _reporting_period(field: str, record: dict[str, Any]) -> str | None:
    if record.get("as_of"):
        return str(record["as_of"])
    value = record.get("value")
    if field == "financials" and isinstance(value, dict):
        rows = [r for r in (value.get("records") or []) if isinstance(r, dict)]
        periods = [r.get("period") for r in rows if isinstance(r.get("period"), dict)]
        if periods:
            latest = max(periods, key=lambda p: str(p.get("tilDato") or ""))
            return f"{latest.get('fraDato')}/{latest.get('tilDato')}"
    return None


def contract_view(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build contract-style `claims`, `evidence` and `errors` from a finished profile (pure, no network)."""
    records = profile.get("evidence") or {}
    evidence_items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for field, record in records.items():
        if not isinstance(record, dict):
            continue
        source_class = record.get("source_class") or record.get("source_type")
        if field == "website":
            source_class = "company_owned"
        evidence_items.append({
            "id": _ev_id(field),
            "field": field,
            "source_url": record.get("source_url"),
            "source_class": source_class,
            "retrieved_at": record.get("retrieved_at"),
            "reporting_period": _reporting_period(field, record),
            "content_sha256": record.get("content_sha256"),
            "claim_span": _span(field, record, profile),
        })
        if record.get("status") in {"source_error", "failed"}:
            errors.append({"module": field, "code": record.get("status"), "message": str(record.get("note") or "")[:200]})

    def claim(field: str, value: Any, source: str, *, record: dict[str, Any] | None = None,
              availability: str | None = None, confidence: float | None = None,
              evidence_ids: list[str] | None = None, note: str | None = None) -> dict[str, Any]:
        record = record if record is not None else records.get(source) or {}
        avail = availability or _availability(record)
        if confidence is None:
            confidence = 0.99 if avail == "available" else 0.0
        item = {
            "field": field,
            "value": value if avail == "available" else None,
            "availability": avail,
            "confidence": confidence,
            "evidence_ids": evidence_ids if evidence_ids is not None else ([_ev_id(source)] if source in records else []),
            "retrieved_at": record.get("retrieved_at"),
            "reporting_period": _reporting_period(source, record),
        }
        if note or record.get("note"):
            item["note"] = str(note or record.get("note"))[:300]
        return item

    claims: list[dict[str, Any]] = []
    reg_src = "registry" if (records.get("registry") or {}).get("status") == "available" else (
        "registry_live" if (records.get("registry_live") or {}).get("status") == "available" else "registry")
    reg_ok = (records.get(reg_src) or {}).get("status") == "available"
    claims.append(claim("legal_name", profile.get("name"), reg_src))
    claims.append(claim("legal_form", profile.get("legal_form"), reg_src))
    label = str(profile.get("industry_label") or "").strip()
    industry_known = reg_ok and label.casefold() not in {"", "uoppgitt"}
    claims.append(claim("industry", f"{profile.get('industry_code')} {label}" if industry_known else None, reg_src,
                        availability="available" if industry_known else ("not_available" if reg_ok else None)))
    claims.append(claim("municipality", profile.get("municipality") or None, reg_src,
                        availability="available" if reg_ok and profile.get("municipality") else ("not_available" if reg_ok else None)))
    claims.append(claim("employees", profile.get("employees"), reg_src,
                        availability="available" if reg_ok and profile.get("employees") is not None else ("not_available" if reg_ok else None)))

    web = records.get("website") or {}
    wvalue = web.get("value") or {}
    assessment = wvalue.get("identity_assessment") or {}
    if web.get("status") == "available" and assessment.get("publishable"):
        claims.append(claim("official_website", wvalue.get("final_url") or web.get("source_url"), "website",
                            confidence=float(assessment.get("score") or 0.9)))
    elif web.get("status") == "available":
        claims.append(claim("official_website", None, "website", availability="ambiguous",
                            confidence=float(assessment.get("score") or 0.0),
                            note="Registry-linked site retrieved but exact legal-entity identity not established; withheld"))
    else:
        claims.append(claim("official_website", None, "website"))

    fin = records.get("financials") or {}
    fin_rows = [r for r in ((fin.get("value") or {}).get("records") or []) if isinstance(r, dict)] if fin.get("status") == "available" else []
    if fin.get("status") == "available" and not fin_rows:
        claims.append(claim("annual_accounts", None, "financials", availability="not_available",
                            note="Register of Company Accounts returned no annual accounts"))
    else:
        claims.append(claim("annual_accounts", fin_rows[:3] or None, "financials"))

    roles = [r for r in ((records.get("roles") or {}).get("value") or {}).get("roles", []) if isinstance(r, dict) and not r.get("inactive")]
    roles_rec = records.get("roles") or {}
    if roles_rec.get("status") == "available" and not roles:
        claims.append(claim("leadership", None, "roles", availability="not_available", note="No active role holder returned"))
    else:
        claims.append(claim("leadership", [{"role": r.get("role"), "name": r.get("name")} for r in roles[:10]], "roles"))

    loc_rec = records.get("locations") or {}
    locations = ((loc_rec.get("value") or {}).get("locations") or []) if loc_rec.get("status") == "available" else []
    if loc_rec.get("status") == "available" and not locations:
        claims.append(claim("workplaces", None, "locations", availability="not_available",
                            note="Subunit register checked: no registered workplaces"))
    else:
        claims.append(claim("workplaces", [{"name": l.get("name"), "address": l.get("address")} for l in locations[:10]], "locations"))

    group = records.get("group") or {}
    claims.append(claim("group_structure", group.get("value"), "group",
                        availability="not_available" if group.get("status") == "available" and not group.get("value") else None))
    for field in ("hiring", "public_activity"):
        rec = records.get(field) or {}
        claims.append(claim(field, rec.get("value"), field, confidence=rec.get("confidence")))
    synthesis = profile.get("synthesis") or {}
    if synthesis:
        cited_fields = sorted({s.get("field") for s in synthesis.get("sources") or [] if s.get("field") in records})
        has_sources = bool(synthesis.get("sources"))
        claims.append({
            "field": "synthesis",
            "value": {k: synthesis.get(k) for k in ("summary", "what_changed", "unknowns")} if has_sources else None,
            "availability": "available" if has_sources else "not_available",
            "confidence": 0.9 if synthesis.get("generated_by") == "llm" else 0.95,
            "evidence_ids": [_ev_id(f) for f in cited_fields],
            "retrieved_at": None,
            "reporting_period": None,
            "note": f"generated_by={synthesis.get('generated_by')}",
        })
    return claims, evidence_items, errors


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    modules = list(modules)
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    try:
        claims, evidence_items, errors = contract_view(profile)
    except Exception as exc:  # never lose a row because of a presentation error
        claims, evidence_items, errors = [], [], [{"module": "contract_view", "code": "failed", "message": f"{type(exc).__name__}: {exc}"[:200]}]
    metrics = profile.get("run_metrics") or {}
    seen = {(e["module"], e["message"]) for e in errors}
    errors = errors + [
        {"module": e.get("module"), "code": "failed", "message": str(e.get("message"))[:200]}
        for e in profile.get("run_errors") or []
        if (e.get("module"), str(e.get("message"))[:200]) not in seen
    ]
    return {
        "run_id": run_id,
        "organisation_number": profile["organisation_number"],
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        # --- OUTPUT_CONTRACT.md view (additive; the keys above are kept for existing tooling) ---
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed" if entity_state == "complete" else "failed",
        },
        "claims": claims,
        "evidence": evidence_items,
        "changes": [],
        "errors": errors,
        "operations": {
            "requests": int(metrics.get("requests") or 0),
            "runtime_ms": int(metrics.get("runtime_ms") or 0),
            "third_party_cost_usd": float((profile.get("synthesis") or {}).get("llm_cost_usd") or 0.0),
        },
        "profile": profile,
    }


def finalise_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile["synthesis"] = synthesise(profile)
    # ensure hiring / public_activity modules exist even if empty
    evidence_map = profile.setdefault("evidence", {})
    if "hiring" not in evidence_map:
        evidence_map["hiring"] = {
            "field": "hiring",
            "status": "not_available",
            "source_type": "permitted_jobs",
            "note": "No permitted job source returned open positions for this entity",
            "value": None,
        }
    if "public_activity" not in evidence_map:
        evidence_map["public_activity"] = {
            "field": "public_activity",
            "status": "not_available",
            "source_type": "permitted_news",
            "note": "No dated public activity found in permitted sources",
            "value": None,
        }
    return profile


def contract_warnings(envelopes: list[dict[str, Any]]) -> list[str]:
    """Shape problems against OUTPUT_CONTRACT.md. Reported, but they never turn a complete run into a failure."""
    warnings: list[str] = []
    for item in envelopes:
        org = item.get("organisation_number")
        known = {e.get("id") for e in item.get("evidence") or []}
        for claim in item.get("claims") or []:
            field, state = claim.get("field"), claim.get("availability")
            if state not in AVAILABILITY_STATES:
                warnings.append(f"{org}: claim {field} has availability {state!r}")
            missing = [i for i in claim.get("evidence_ids") or [] if i not in known]
            if missing:
                warnings.append(f"{org}: claim {field} cites unknown evidence {missing}")
            if state == "available" and not claim.get("evidence_ids"):
                warnings.append(f"{org}: available claim {field} has no evidence")
            if state != "available" and claim.get("value") is not None:
                warnings.append(f"{org}: claim {field} carries a value although it is {state}")
        for evidence_item in item.get("evidence") or []:
            if not evidence_item.get("retrieved_at"):
                warnings.append(f"{org}: evidence {evidence_item.get('id')} has no retrieved_at")
    return warnings


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    warnings = contract_warnings(envelopes)
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states,
            "contract_warnings": warnings[:50], "contract_warning_count": len(warnings)}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    # A module that never ran (not_fetched) or was cut off by the time budget must be run again on resume.
    return all(module in records and records[module].get("status") not in {"not_fetched", "budget_exhausted"} for module in modules)