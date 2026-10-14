# Signalpost Company Research Agent

A research agent for Norwegian companies, built for the
[Builderr Signalpost challenge](https://builderr.ai/challenges/signalpost). Give it a list of Norwegian
organisation numbers and it will:

1. read the official facts from the Brønnøysund registers,
2. add the permitted public signals (the registry-declared website, hiring, public activity),
3. attach a source, retrieval time and content hash to every claim,
4. write a short, cited synthesis (what the company does, what changed, what is unknown),
5. emit **exactly one terminal result envelope per input company**, whatever happens.

> **Design priority.** A missing fact is acceptable. A fact published under the **wrong company** is not
> (the challenge blocks qualification for a material wrong-company match). Every website-derived fact passes an
> exact-entity identity gate first. When in doubt the agent answers `not_available` or `ambiguous`.

---

### 1.8 Official run (the single command)

```bash

uv sync --frozen
uv run python scripts/run_competition_batch.py \
  --organisations <official-batch.jsonl> \
  --bulk brreg-enheter.csv \
  --profiles-output out/official-profiles.jsonl \
  --output out/official-envelopes.jsonl \
  --report out/official-report.json \
  --run-id official-<date> \
  --expected-count <N>


```

```bash

  # 1.6 Human-readable viewer

uv run python build_signalpost_viewer.py \
  --input out/official-envelopes.jsonl \
  --report out/official-report.json \
  --output out/smoke-viewer.html

```

Replace the three placeholders before running (do not type the `<` `>` characters):

- `<official-batch.jsonl>`: the batch file Builderr supplies.
- `<date>`: any unique label, for example `official-2026-10-13`.
- `<N>`: the number of companies in that batch.
- Cheak out folder for the output

Exit code `0` means every input row received a valid terminal envelope. If the run is interrupted, rerun the same
command with `--resume` added.

## Contents

1. [Quick start](#1-quick-start)
2. [How the project maps to the challenge rules](#2-how-the-project-maps-to-the-challenge-rules)
3. [How a run works](#3-how-a-run-works)
4. [Output: the result envelope](#4-output-the-result-envelope)
5. [Sources and identity policy](#5-sources-and-identity-policy)
6. [Hiring and public activity](#6-hiring-and-public-activity)
7. [LLM synthesis](#7-llm-synthesis)
8. [Configuration reference](#8-configuration-reference)
9. [Cost, requests and time](#9-cost-requests-and-time)
10. [Testing and compliance check](#10-testing-and-compliance-check)
11. [Troubleshooting](#11-troubleshooting)
12. [Project layout](#12-project-layout)
13. [Scripts](#13-scripts)
14. [Third-party sources, models and licences](#14-third-party-sources-models-and-licences)
15. [Known limitations](#15-known-limitations)
16. [Audit changelog](#16-audit-changelog)
17. [Submission checklist](#17-submission-checklist)

---

## 1. Quick start

**Requirements**

- Python 3.12+ and [`uv`](https://github.com/astral-sh/uv).
- No LLM key is required. Without one the agent uses its deterministic writer; a key only enables the optional
  LLM rewrite of the summary (section 7).
- Network access to `data.brreg.no` and `builderr.ai` (data downloads and live registry calls).

The commands in 1.1 to 1.8 are for macOS / Linux. The same steps for Windows are in 1.9.

### 1.1 Get the project and install dependencies

```bash
# install uv once (skip if `uv --version` already works)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
cd signalpost-v1

# install the exact versions pinned in uv.lock
uv sync --frozen
```

### 1.2 Try it offline (no network, no key)

Test run

```bash
uv run python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

### 1.3 Get the data files

```bash
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz
```

### 1.4 Configure the LLM (optional, Gemini)

```bash
cp .env.example .env
nano .env
```

Set these values in `.env`:

```ini
SIGNALPOST_LLM_API_KEY=your-gemini-api-key
SIGNALPOST_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
SIGNALPOST_LLM_MODEL=gemini-3.1-flash-lite
SIGNALPOST_NAV_PUBLIC_TOKEN="1"
```

### 1.5 Local 100-company smoke test

`smoke-companies.jsonl` (20 sample companies) is already in the repository, so the 100-company batch is written to
a different file, `smoke-100.jsonl`. The agent never chooses its own companies; `select_entry_batch.py` and the
smoke files are for local testing only.

```bash
# 1. build a 100-company test batch from the public universe
uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 \
  --output smoke-100.jsonl

# 2. run the agent on it
uv run python scripts/run_competition_batch.py \
  --organisations smoke-100.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100

  # 1.6 Human-readable viewer

uv run python build_signalpost_viewer.py \
  --input out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --output out/smoke-viewer.html

```

### 1.7 Optional flags

| Flag                       | Purpose                                                                                                                                                 |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--bulk brreg-enheter.csv` | use a Brreg bulk snapshot (csv or csv.gz) instead of one live API call per company. If the file is unreadable the run falls back to the live API        |
| `--expected-count N`       | validation fails (exit 1) unless exactly N rows were supplied. Outputs are still written                                                                |
| `--time-budget-s SECONDS`  | wall-clock budget (env `SIGNALPOST_TIME_BUDGET_S`). Companies not processed in time get a terminal `budget_exhausted` envelope instead of being dropped |
| `--workers 8`              | thread-pool size (default 8)                                                                                                                            |
| `--checkpoint-every 200`   | how often profiles are saved                                                                                                                            |
| `--resume`                 | continue an interrupted run                                                                                                                             |
| `--modules a,b,c`          | run a subset of modules                                                                                                                                 |

### 1.9 Windows commands

**Install and unpack (1.1)**

```powershell
# install uv once (skip if `uv --version` already works), then reopen PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```powershell
cd signalpost-v1

# install the exact versions pinned in uv.lock
uv sync --frozen
```

**Try it offline (1.2)**

```powershell
uv run python scripts/run_refresh_replay.py `
  --manifest tests/fixtures/refresh-snapshots.json `
  --output out/refresh-demo.json
```

**Get the data files (1.3)**

```powershell
curl.exe -L "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv" -o brreg-enheter.csv
curl.exe -L "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz" -o signalpost-universe.jsonl.gz

```

**Configure the LLM, optional (1.4)**

```powershell
Copy-Item .env.example .env
```

**Local 100-company smoke test (1.5)**

```powershell
# 1. build a 100-company test batch from the public universe
uv run python select_entry_batch.py `
  --universe signalpost-universe.jsonl.gz `
  --count 100 `
  --output smoke-100.jsonl

# 2. run the agent on it
uv run python scripts/run_competition_batch.py `
  --organisations smoke-100.jsonl `
  --bulk brreg-enheter.csv `
  --profiles-output out/smoke-profiles.jsonl `
  --output out/smoke-envelopes.jsonl `
  --report out/smoke-report.json `
  --run-id smoke-001 `
  --expected-count 100

#Human-readable viewer (1.6)**

uv run python build_signalpost_viewer.py `
  --input out/smoke-envelopes.jsonl `
  --report out/smoke-report.json `
  --output out/smoke-viewer.html
```

---

## 2. How the project maps to the challenge rules

| Challenge rule                                                                          | How the project meets it                                                                                                                                                                                         |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Read the batch supplied at run time; never choose own companies                         | the batch is a required argument; there is no built-in company list in the run path                                                                                                                              |
| Process the full batch, including more than 100                                         | thread pool, resumable, no size limit; `--expected-count` is optional                                                                                                                                            |
| Exactly one terminal envelope per input company                                         | input lines that are invalid become a `failed` envelope, duplicates collapse to one row, a crashed step becomes a `source_error` module, an exhausted time budget becomes `budget_exhausted`. Nothing is dropped |
| States `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed` | every claim uses exactly this vocabulary                                                                                                                                                                         |
| Never replace missing values with zero                                                  | unknown employees, website, accounts are `null` with `not_available`. A headcount is only reported when Brreg states one (`harRegistrertAntallAnsatte`)                                                          |
| Source, retrieval date and reporting period for every claim                             | each claim cites evidence records with `source_url`, `retrieved_at`, `reporting_period`, `content_sha256`, `claim_span`                                                                                          |
| No wrong-company match                                                                  | exact-entity gate for websites; NAV ads accepted only when `employer.orgnr` equals the company; job postings for another employer withheld; redirects to another domain withheld                                 |
| Refresh is idempotent                                                                   | `refresh.py` diffs two snapshots; re-running the same snapshot yields no changes (`scripts/run_refresh_replay.py` checks this)                                                                                   |
| Fixed time and resource budget                                                          | `--time-budget-s`, bounded crawls (page, byte and wall-clock limits), LLM spend and call caps                                                                                                                    |
| LLM budget; no personal credentials                                                     | only `SIGNALPOST_LLM_API_KEY` is read (never `OPENAI_API_KEY`, `GROQ_API_KEY`...), no key is shipped, the LLM is optional                                                                                        |
| Safe URL handling                                                                       | public addresses only (also across redirects), robots.txt honoured, size and time caps, XML entity expansion off                                                                                                 |
| Secret handling                                                                         | no `.env` in the project, `.env` is git-ignored, the key is never written to a report (only the variable name)                                                                                                   |
| Installs on a clean machine with one declared step; one run command                     | `uv sync --frozen` and the command in section 1                                                                                                                                                                  |
| Pinned dependencies                                                                     | `uv.lock`                                                                                                                                                                                                        |
| Declare models, APIs and licences                                                       | section 14                                                                                                                                                                                                       |

## 3. How a run works

```
 organisation numbers (txt / json / jsonl)
          │  tolerant reader: bad line -> failed envelope, duplicate -> collapsed
          ▼
 initial profile per company
   bulk snapshot row (if --bulk)      or      empty profile filled from the live Brreg record
          │
          ▼  per company, in a thread pool. Every step is isolated: a crash marks that module
          │  `source_error`; the row is never lost. After the time budget: `budget_exhausted`.
   1. official Brreg APIs   registry_live · financials · roles · group · locations
   2. website               registry `hjemmeside` only · robots.txt · SSRF-safe fetch · same-domain redirects only
                            homepage + up to 4 identity pages + up to 2 careers/news pages + 1 RSS/Atom feed
   3. identity gate         exact legal-entity proof, else the site is withheld
   4. public_activity       dated items from the verified site (JSON-LD, <time>, meta, RSS)
   5. hiring                JobPosting markup / careers listings (+ optional NAV feed)
   6. synthesis             sourced facts -> deterministic writer, optional LLM rewrite
          │
          ▼
 terminal envelope  ──►  validation (count, uniqueness, terminal states, contract warnings)
          │
          ▼
 envelopes.jsonl · profiles.jsonl · report.json
```

---

## 4. Output: the result envelope

One JSON object per company (one line of `envelopes.jsonl`). It satisfies `OUTPUT_CONTRACT.md` and keeps a few
extra keys for the project's own tools.

```jsonc
{
  "run_id": "smoke-001",
  "organisation_number": "923609016",
  "state": "complete", // entity-level terminal state
  "started_at": "...",
  "completed_at": "...",
  "modules": {
    // per-module terminal state
    "registry": {
      "state": "complete",
      "retry_count": 0,
      "final_timestamp": "...",
    },
    "website": { "state": "blocked_robots", "...": "..." },
  },
  "run": {
    "run_id": "smoke-001",
    "started_at": "...",
    "completed_at": "...",
    "terminal_status": "completed",
  },
  "claims": [
    {
      "field": "official_website",
      "value": "https://example.no/",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-website"],
      "retrieved_at": "...",
      "reporting_period": null,
    },
    {
      "field": "employees",
      "value": null,
      "availability": "not_available",
      "confidence": 0.0,
      "evidence_ids": ["ev-registry"],
      "retrieved_at": "...",
      "reporting_period": null,
    },
  ],
  "evidence": [
    {
      "id": "ev-website",
      "field": "website",
      "source_url": "https://example.no/",
      "source_class": "company_owned",
      "retrieved_at": "...",
      "reporting_period": null,
      "content_sha256": "...",
      "claim_span": "...",
    },
  ],
  "changes": [],
  "errors": [],
  "operations": {
    "requests": 11,
    "runtime_ms": 1840,
    "third_party_cost_usd": 0.0001,
  },
  "profile": { "...": "full normalised profile including the synthesis" },
}
```

**Claims emitted:** `legal_name`, `legal_form`, `industry`, `municipality`, `employees`, `official_website`,
`annual_accounts`, `leadership`, `workplaces`, `group_structure`, `hiring`, `public_activity`, `synthesis`.

**Availability vocabulary**

| Evidence status                                                          | Claim `availability` | Meaning                                                               |
| ------------------------------------------------------------------------ | -------------------- | --------------------------------------------------------------------- |
| `available`                                                              | `available`          | found, with evidence                                                  |
| `not_found`, `not_available`                                             | `not_available`      | searched, nothing exists (or nothing could be read)                   |
| `blocked`                                                                | `blocked`            | robots.txt or policy refused access                                   |
| `not_applicable`                                                         | `not_applicable`     | the question does not apply (for example an invalid input row)        |
| website retrieved but identity unproven, or redirected to another domain | `ambiguous`          | value withheld to avoid a wrong-company match                         |
| `source_error`, `failed`, `budget_exhausted`                             | `failed`             | an error or cut-off on this entry (see `errors` and the module state) |

A checked-and-empty source is told apart from a source that was not checked by the claim `note`
(for example _"Company careers page states no open positions"_ versus _"Not checked: no verified website"_).

**Module states** (used by validation): `complete`, `not_found`, `not_applicable`, `blocked_robots`,
`blocked_policy`, `source_error`, `budget_exhausted`, `submission_error`, `not_available`, `ambiguous`, `failed`.
An envelope set is valid only if every state is terminal and the count and uniqueness match.

**`changes`** is empty on a first run. Change detection between two snapshots lives in `refresh.py` and is
demonstrated by `scripts/run_refresh_replay.py`.

---

## 5. Sources and identity policy

- Official facts come from the Brønnøysund Register Centre (NLOD 2.0). Contact details (e-mail, phone) in the
  registry row are **not** stored, because for sole proprietors they are personal data.
- The website is taken **only** from the registry field `hjemmeside`. No search engine, no directory and no
  domain guessing is used. If the registry has no website the claim is `not_available`.
- **Identity gate** (`identity.py`): a website is publishable only on exact proof: the organisation number as a
  stand-alone token in the homepage identity text (score 1.0), or all legal-name tokens together in the homepage
  identity evidence (0.95). Parked/for-sale pages, partial name overlap and business sports clubs pointing at an
  operating company's site are rejected. Social links must pass their own gate.
- A homepage that redirects to a **different registered domain** is withheld (`ambiguous`).
- Hiring and public-activity facts come only from sites that passed the gate. The optional NAV feed is the one
  exception because each ad is confirmed by organisation number.
- **The LLM never decides identity.** It only rewrites a summary from facts that already passed the gates.
- Evidence hashes belong to the page the evidence URL points at.

Crawler behaviour: user agent `builderr-signalpost-poc/0.1 (+https://builderr.ai)`; `robots.txt` honoured per
RFC 9309 (no file or a 4xx answer: allowed; an explicit disallow, a 5xx answer or an unreachable file: not
allowed); private, loopback and link-local addresses blocked, including across redirects; homepage limit 2 MB,
other pages 1 MB, feeds 500 KB, plus a wall-clock limit per download; the public-suffix list is read from the
installed package (no extra network request).

---

## 6. Hiring and public activity

The crawler fetches the homepage, up to 4 identity pages, and up to **2 extra pages reserved for careers/news**,
plus one RSS/Atom feed on the same registered domain. `signals.py` reads each page without further requests.

| Signal                         | How it is read                                                                                                             | Confidence |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------- | ---------- |
| Job posting                    | schema.org `JobPosting` JSON-LD: title, `datePosted`, `validThrough`, location                                             | 0.9        |
| Job posting (heuristic)        | links under the company's own careers section whose text looks like a job title                                            | 0.6        |
| "No open positions"            | explicit statement on the careers page, reported as `not_available` and noted as _checked, none open_                      | n/a        |
| Careers page / ATS portal only | recorded in the note (Webcruiter, Jobylon, Teamtailor, ...); individual openings are not fetched                           | n/a        |
| Dated activity                 | JSON-LD `NewsArticle`/`Article`/`BlogPosting`, `<time datetime>` list items, `article:published_time` meta, RSS/Atom items | 0.8 to 0.9 |
| Undated news section           | news/press pages exist but no readable date, `available` at confidence 0.5                                                 | 0.5        |
| Social profile links only      | **not** counted as activity                                                                                                | n/a        |

Precision rules: an activity item needs a **title and a date**; dates before 2000 or in the future are rejected;
postings whose `validThrough` has passed are **not** counted as open; a posting naming a different hiring
organisation than the company is withheld (the count of withheld postings is recorded); feeds are parsed with
entity expansion and network access disabled.

## 7. LLM synthesis

Goal (12 points): _what the company does, what changed, what is unknown, with sources._

1. **Fact sheet.** `llm_synthesis.build_facts` turns the profile into numbered facts (`F1`, `F2`, ...). Each fact
   points at its evidence record (URL, retrieval time, hash). If no registry record could be read there are **no**
   facts and nothing is asserted about the entity.
2. **Deterministic writer (always on).** Produces `summary`, `what_changed` (year-on-year change in revenue and
   annual result from the two latest company-level accounts), `unknowns` and `sources`. No key, no network.
3. **Optional LLM rewrite.** If `SIGNALPOST_LLM_API_KEY` is set, an OpenAI-compatible chat model rewrites **only**
   the `summary` from the facts. The rewrite is accepted only if it cites existing fact ids, every sentence's
   numbers appear in the facts it cites, and it contains no name, URL or e-mail address that no fact states.
   Otherwise the deterministic text is kept and the reason is counted in `report.llm.fallback_reasons`.
4. **Safety.** Provider quirks (`max_tokens` vs `max_completion_tokens`, unsupported `temperature` or
   `response_format`) are handled automatically; 429/5xx are retried; a bad key or unknown model switches the LLM
   off for the rest of the run; spend is reserved per call so the hard limit holds with parallel workers; the key
   is only ever sent over HTTPS (plain HTTP only to a local test server). When the time budget is spent the
   deterministic writer is used.

`synthesis` fields: `summary`, `what_changed`, `unknowns`, `sources`, `generated_by` (`llm` | `deterministic`),
`model`, `fallback_reason`, `llm_cost_usd`.

---

---

## 9. Cost, requests and time

- **Third-party cost.** Only the optional LLM costs money: one short call per company (a fact sheet in, about 70
  words out). At `gpt-4o-mini` prices that is a small fraction of a cent per company. The report records the
  estimated spend (`report.llm`, `operations.third_party_cost_usd`).
- **Requests per company (estimate).** 1 to 5 official Brreg calls (`registry_live`, `financials`, `roles`,
  `group`, `locations`) plus, when a registry website exists, roughly 6 to 14 HTTP requests. `financial_history`
  is off by default because that endpoint is throttled to one request per 2.1 s.
- **Official limits.** The public challenge page does not state the dollar budget, request cap or time limit.
  The defaults `6.50` / `8.00` for the LLM limits come from the original project and are **unverified**. Confirm
  the real numbers with Builderr, set the limits, and use `--time-budget-s`. Measure `operations.requests` and
  `operations.runtime_s` in a smoke run and extrapolate to the official batch size.

---

## 10. Testing and compliance check

```bash
uv run python -m unittest tests.test_poc tests.test_signals tests.test_fixes

```

- `tests.test_poc`: registry normalisation, identity gate, batch contract, refresh diff, sampling, sentiment, ...
- `tests.test_signals`: date parsing, JobPosting / news / feed extraction, identity-safe hiring and activity
  against a local test website, and the NAV feed against a local fake server.
- `tests.test_fixes`: regression tests for every audit fix (robots.txt semantics, cross-domain redirects,
  expired postings, employees never defaulted to 0, tolerant input, time budget, LLM key handling, hallucination
  guard, no secrets or search-engine code in the repository, the full runner with a faked network).

## The tests need no internet and no API key.

---

## 12. Project layout

```
signalpost-v1/
├── README.md  LICENSE  OUTPUT_CONTRACT.md
├── pyproject.toml  uv.lock  .env.example  .gitignore
├── signalpost_compliance_check.py   audit a finished run against rules R1-R6
├── build_signalpost_viewer.py       static HTML viewer (desktop and mobile)
├── check_llm.py                     one-call LLM configuration check
├── select_entry_batch.py            reproducible LOCAL test batch from the public universe
├── nav_diagnose.py nav_feed_info.py nav_prefetch.py     NAV feed helpers
├── smoke-companies.jsonl            20 sample companies for a local test
├── src/norway_company_agent/
│   ├── batch.py                 inputs, profiles, envelopes, contract view, validation
│   ├── official.py              Brreg APIs and normalisers (entity, accounts, roles, group, subunits)
│   ├── sampling.py              Brreg bulk reader (plain or gzip), deterministic samples
│   ├── website.py               robots-aware, SSRF-safe crawler, social links, feed fetch
│   ├── identity.py              exact-entity website and social identity gate
│   ├── signals.py               JobPosting / dated-item / feed parsing (pure functions)
│   ├── site_activity.py         hiring and public_activity evidence records
│   ├── nav_jobs.py              optional NAV job feed (organisation-number confirmed)
│   ├── llm_synthesis.py         fact sheet, deterministic writer, optional LLM rewrite, budget
│   ├── evidence.py  http.py  operations.py
│   ├── refresh.py  snapshots.py     change detection between snapshots
│   ├── research.py  workspace.py    deterministic question answering over profiles
│   └── external_*.py  sentiment.py  scrapy_crawler.py  crawl_events.py   offline experiments, not in the default run
├── scripts/                     see section 13
├── tests/                       unit and regression tests and fixtures
├── docs/                        design notes
└── data/universe-metadata.json  frozen universe description
```

---

## 13. Scripts

**Core (used by the official run)**

| Script                             | Purpose                                                     |
| ---------------------------------- | ----------------------------------------------------------- |
| `scripts/run_competition_batch.py` | the batch run described in section 1                        |
| `signalpost_compliance_check.py`   | rule audit of a finished run                                |
| `build_signalpost_viewer.py`       | static HTML viewer                                          |
| `scripts/run_refresh_replay.py`    | replay old/new snapshots through the production normalisers |

---

## 14. Third-party sources, models and licences

| Source / component                                                                          | Use                                              | Terms                                         |
| ------------------------------------------------------------------------------------------- | ------------------------------------------------ | --------------------------------------------- |
| Brønnøysund Register Centre (Enhetsregisteret, Regnskapsregisteret, roles, group, subunits) | identity, accounts, roles, structure, workplaces | NLOD 2.0                                      |
| Company websites (registry-linked only)                                                     | description, news, careers                       | robots.txt respected; exact-entity gate first |
| NAV Arbeidsplassen job feed (optional, off by default)                                      | job ads                                          | NAV terms of use                              |

---

## 15. Known limitations

- **Coverage ceiling.** Website-derived fields exist only for companies whose registry entry declares a
  website. Most small `AS` companies have none (7 of 100 in the earlier public smoke batch), so hiring and
  public activity will be empty for most companies unless the NAV feed is enabled.
- **Heuristic job links** (confidence 0.6) can include non-job pages on unusual sites; JSON-LD postings (0.9)
  are reliable. ATS portals are noted but their listings are not fetched.
- **Public activity is company-owned**: it says what the company publishes, not what others say.
- **The NAV connector** was written from NAV's documentation and tested only against a local fake server and one
  earlier timeout. Try it on a small batch before relying on it. A name-indexed feed misses ads whose
  `businessName` differs from the legal name.
- **DNS rebinding.** Addresses are checked before the request and on redirects, but the check and the connection
  resolve the name separately; a hostile DNS server could in theory answer differently the second time.
- **Resource limits are unconfirmed** (section 9).
- **Live behaviour not re-verified in the audit.** The fixes were verified with the unit and regression tests
  and a faked network. The audit environment could not reach `data.brreg.no`, so run a smoke test on your own
  machine before submitting.
