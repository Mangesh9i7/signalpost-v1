"""Quick LLM check: python check_llm.py   (makes at most ONE tiny API call).

Also builds the fact sheet directly, so a broken llm_synthesis.py shows its real traceback
instead of a bare "build-error".
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from norway_company_agent.llm_synthesis import build_facts, synthesise  # noqa: E402

profile = {
    "organisation_number": "923609016", "name": "TEST AS", "legal_form": "AS", "municipality": "OSLO",
    "industry_code": "62.010", "industry_label": "Programmeringstjenester", "employees": 5,
    "evidence": {"registry": {"status": "available", "source_url": "https://data.brreg.no/", "retrieved_at": "2026-01-01T00:00:00Z"}},
}

# 1. Build the fact sheet on its own: any bug in llm_synthesis.py shows up here with a full traceback.
try:
    facts, ctx, unknowns = build_facts(profile)
    print("fact sheet     : OK,", len(facts), "fact(s),", len(unknowns), "unknown(s)")
except Exception:  # noqa: BLE001
    print("fact sheet     : FAILED - fix this first (traceback below)")
    traceback.print_exc()
    sys.exit(1)

# 2. Full synthesis (uses the LLM only if a key is configured).
result = synthesise(profile)
print("generated_by   :", result["generated_by"])
print("fallback_reason:", result.get("fallback_reason"))
print("error          :", result.get("error"))
print("summary        :", result["summary"])
