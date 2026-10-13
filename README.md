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

Licence: [MIT](LICENSE).

---

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

# unpack the project and enter it
unzip signalpost-v1.zip
cd signalpost-v1

# install the exact versions pinned in uv.lock
uv sync --frozen
```

### 1.2 Try it offline (no network, no key)

Replays stored registry snapshots through the production code.

```bash
uv run python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

### 1.3 Get the data files

Two files, both saved in the project root. Confirm the URLs on the
[challenge page](https://builderr.ai/challenges/signalpost) if a download fails.

| File                           | What it is                                                                                         | Needed for                                                                      |
| ------------------------------ | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `brreg-enheter.csv`            | Brreg bulk snapshot of all registered units. Plain CSV or gzip; the loader detects which           | `--bulk` (optional; without it the agent makes one live Brreg call per company) |
| `signalpost-universe.jsonl.gz` | public company universe from the challenge. **Do not unzip it**, the tools read the `.gz` directly | building local test batches (1.5) only                                          |

```bash
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

# check that both files arrived and are not empty
ls -lh brreg-enheter.csv signalpost-universe.jsonl.gz
```

### 1.4 Configure the LLM (optional, Gemini)

Skip this step to run without an LLM. The agent reads **one** key variable, `SIGNALPOST_LLM_API_KEY`, and talks to
any OpenAI-compatible HTTPS endpoint. Google's Gemini API offers one, so a Gemini key works by changing three
values. Create `.env` in the project root (**never commit it**; it is git-ignored):

```bash
cp .env.example .env
nano .env        # or any editor
```

Set these values in `.env`:

```ini
SIGNALPOST_LLM_API_KEY=your-gemini-api-key
SIGNALPOST_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
SIGNALPOST_LLM_MODEL=gemini-3.1-flash-lite

# Dollars per 1M tokens, from Google's pricing page for the model you chose.
# The built-in defaults (0.15 / 0.60) are gpt-4o-mini prices, so the spend estimate
# and the soft/hard limits are wrong for Gemini until you set these.
SIGNALPOST_LLM_PRICE_INPUT=
SIGNALPOST_LLM_PRICE_OUTPUT=
```

Notes:

- `GEMINI_API_KEY` and `GOOGLE_API_KEY` are **ignored** on purpose; only `SIGNALPOST_LLM_API_KEY` is read.
- Model IDs change. `gemini-2.0-flash` is retired and `gemini-2.5-flash` is scheduled for shutdown on
  16 October 2026; a retired ID returns `HTTP 404`. `gemini-3.5-flash` is the larger stable option. To list the IDs
  your key can use:

  ```bash
  curl "https://generativelanguage.googleapis.com/v1beta/openai/models" -H "Authorization: Bearer YOUR_GEMINI_KEY"
  ```

- A personal Gemini key is fine for local smoke tests. For the **official run** use the key and budget Builderr
  specifies (sections 8 and 9).
- If you see many `HTTP 429` responses (free-tier rate limits), run with fewer workers, for example `--workers 2`.

Check the key with one tiny call:

```bash
uv run python check_llm.py
```

You want `generated_by: llm`. Anything else prints the reason: `no-key`, `api-error` (`HTTP 401` = wrong key,
`HTTP 404` = model ID not available to the key) or `rejected:...`.

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

# 3. rule audit of the finished run
uv run python signalpost_compliance_check.py \
  --input out/smoke-envelopes.jsonl \
  --report out/smoke-report.json
```

**Expected:** exit code `0` (check with `echo $?`), exactly 100 envelopes, `validation.passed = true`, and no
`VIOLATION` from the compliance check. Open `out/smoke-report.json` and read:

- `validation`: all five checks `true`
- `llm`: provider host, model, key source (variable name only), calls, spend, `fallback_reasons`
- `operations`: requests, bytes, p50/p95 latency, runtime, third-party cost
- `nav_job_feed`: only meaningful when the optional NAV feed is enabled (section 6)

### 1.6 Human-readable viewer

```bash
uv run python build_signalpost_viewer.py \
  --input out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --output out/smoke-viewer.html
```

Open `out/smoke-viewer.html` in a browser. It is a single self-contained file (no network, no external fonts) and
shows the summary, what changed, the unknowns and the exact sources behind every statement, on desktop and mobile.
Website links are shown only when the website passed the identity gate.

### 1.7 Optional flags

| Flag                       | Purpose                                                                                                                                                 |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--bulk brreg-enheter.csv` | use a Brreg bulk snapshot (csv or csv.gz) instead of one live API call per company. If the file is unreadable the run falls back to the live API        |
| `--expected-count N`       | validation fails (exit 1) unless exactly N rows were supplied. Outputs are still written                                                                |
| `--time-budget-s SECONDS`  | wall-clock budget (env `SIGNALPOST_TIME_BUDGET_S`). Companies not processed in time get a terminal `budget_exhausted` envelope instead of being dropped |
| `--workers 8`              | thread-pool size (default 8)                                                                                                                            |
| `--checkpoint-every 25`    | how often profiles are saved                                                                                                                            |
| `--resume`                 | continue an interrupted run                                                                                                                             |
| `--modules a,b,c`          | run a subset of modules                                                                                                                                 |

`--organisations` accepts a `.txt` (one number per line), `.json` (list) or `.jsonl` (objects with
`organisation_number`) file. The official run reads whatever batch Builderr supplies.

### 1.8 Official run (the single command)

```bash
uv run python scripts/run_competition_batch.py \
  --organisations <official-batch.jsonl> \
  --bulk brreg-enheter.csv \
  --profiles-output out/official-profiles.jsonl \
  --output out/official-envelopes.jsonl \
  --report out/official-report.json \
  --run-id official-<date> \
  --expected-count <N>
```

Replace the three placeholders before running (do not type the `<` `>` characters):

- `<official-batch.jsonl>`: the batch file Builderr supplies.
- `<date>`: any unique label, for example `official-2026-10-13`.
- `<N>`: the number of companies in that batch.

Exit code `0` means every input row received a valid terminal envelope. If the run is interrupted, rerun the same
command with `--resume` added.

### 1.9 Windows commands

The same steps as 1.1 to 1.8, in the same order, for **Windows PowerShell**. Run them from the project folder.
Two Windows differences: use `curl.exe` (plain `curl` in PowerShell is an alias of `Invoke-WebRequest` and does not
accept `-L` / `-o`), and a line continues with a backtick `` ` `` as the very last character of the line.

**Install and unpack (1.1)**

```powershell
# install uv once (skip if `uv --version` already works), then reopen PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# unpack the project and enter it
Expand-Archive -Path signalpost-v1.zip -DestinationPath .
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

# check that both files arrived and are not empty
Get-ChildItem brreg-enheter.csv, signalpost-universe.jsonl.gz | Select-Object Name, Length
```

**Configure the LLM, optional (1.4)**

```powershell
Copy-Item .env.example .env
notepad .env        # paste the same values as in 1.4 (Gemini key, base URL, model)
```

```powershell
# list the model IDs your key can use (optional)
curl.exe "https://generativelanguage.googleapis.com/v1beta/openai/models" -H "Authorization: Bearer YOUR_GEMINI_KEY"

# check the key with one tiny call: you want generated_by: llm
uv run python check_llm.py
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

# 3. rule audit of the finished run
uv run python signalpost_compliance_check.py `
  --input out/smoke-envelopes.jsonl `
  --report out/smoke-report.json

# exit code of the last command: 0 = ok
echo $LASTEXITCODE
```

**Human-readable viewer (1.6)**

```powershell
uv run python build_signalpost_viewer.py `
  --input out/smoke-envelopes.jsonl `
  --report out/smoke-report.json `
  --output out/smoke-viewer.html
```

**Official run, the single command (1.8)**

PowerShell:

```powershell
uv run python scripts/run_competition_batch.py `
  --organisations <official-batch.jsonl> `
  --bulk brreg-enheter.csv `
  --profiles-output out/official-profiles.jsonl `
  --output out/official-envelopes.jsonl `
  --report out/official-report.json `
  --run-id official-<date> `
  --expected-count <N>
```

Command Prompt (`cmd`), where the line continuation is `^`:

```bat
uv run python scripts/run_competition_batch.py ^
  --organisations <official-batch.jsonl> ^
  --bulk brreg-enheter.csv ^
  --profiles-output out/official-profiles.jsonl ^
  --output out/official-envelopes.jsonl ^
  --report out/official-report.json ^
  --run-id official-<date> ^
  --expected-count <N>
```

Replace `<official-batch.jsonl>`, `<date>` and `<N>` exactly as described in 1.8. To resume an interrupted run,
add `--resume` to the same command.

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

Rules the project owner added on top (checked by `signalpost_compliance_check.py`):

| Id  | Rule                                                                                                                                        |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| R1  | `employees` = Brreg `antallAnsatte` as an integer, else `null` (never a defaulted 0)                                                        |
| R2  | website = only the registry `hjemmeside`, else `null`; evidence must come from that same domain; no websites found by search or by guessing |
| R3  | hiring = only the NAV job feed (organisation-number confirmed) or the verified registry website                                             |
| R4  | public activity = only from the verified registry website                                                                                   |
| R5  | no search engines anywhere in the evidence URLs                                                                                             |
| R6  | availability vocabulary respected; a claim that is not `available` carries no value                                                         |

---

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

### Optional NAV Arbeidsplassen job feed

Official, free feed of Norwegian job ads (it excludes Finn.no). **Off by default.** Enable with:

```ini
SIGNALPOST_NAV_PUBLIC_TOKEN=1        # NAV's public, account-free test token (rotates)
```

Using it means accepting NAV's terms (<https://arbeidsplassen.nav.no/vilkar-api>). The feed list carries only a
business _name_, so the agent indexes active ads by normalised name once per run and accepts an ad **only if
`employer.orgnr` equals the company's organisation number**. The first read can be large; it is capped by
`SIGNALPOST_NAV_BUDGET_S` (default 300 s) and by the run's `--time-budget-s`, saved to a local cache
(`.signalpost_nav_cache.json`, git-ignored) and resumed on the next run.

> `SIGNALPOST_NAV_TOKEN` (a token registered to **you**) also works, but it is a credential tied to your own
> account at another service. Do not use it for an official run unless Builderr tells you to.

---

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

## 8. Configuration reference

All settings are environment variables. For local experiments they can be put in a project-root `.env`
(see `.env.example`; **never commit it**). Shell variables always win over `.env`.

| Variable                                                              | Default                                | Purpose                                                                                           |
| --------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `SIGNALPOST_LLM_API_KEY`                                              | none                                   | the only key variable that is read. Use the key Builderr supplies. Placeholder values are ignored |
| `SIGNALPOST_LLM_BASE_URL`                                             | `https://api.openai.com/v1`            | any OpenAI-compatible HTTPS endpoint                                                              |
| `SIGNALPOST_LLM_MODEL`                                                | `gpt-4o-mini`                          | model name                                                                                        |
| `SIGNALPOST_LLM_SOFT_LIMIT` / `_HARD_LIMIT`                           | `6.50` / `8.00`                        | dollars. **Set both to the budget Builderr gives you** (see section 9)                            |
| `SIGNALPOST_LLM_MAX_CALLS`                                            | `1100`                                 | call cap                                                                                          |
| `SIGNALPOST_LLM_PRICE_INPUT` / `_OUTPUT`                              | `0.15` / `0.60`                        | dollars per 1M tokens, used for the estimate                                                      |
| `SIGNALPOST_LLM_TIMEOUT`                                              | `25`                                   | seconds per call                                                                                  |
| `SIGNALPOST_LLM_DISABLE`                                              | unset                                  | `1` forces the deterministic writer                                                               |
| `SIGNALPOST_TIME_BUDGET_S`                                            | unset                                  | same as `--time-budget-s`                                                                         |
| `SIGNALPOST_NAV_PUBLIC_TOKEN`                                         | unset                                  | `1` enables the NAV job feed with the public token                                                |
| `SIGNALPOST_NAV_TOKEN`                                                | unset                                  | your own NAV token (not for official runs, see section 6)                                         |
| `SIGNALPOST_NAV_MAX_PAGES` / `_TIMEOUT` / `_BUDGET_S` / `_START_DAYS` | `5000` / `30` / `300` / `190`          | NAV feed tuning                                                                                   |
| `SIGNALPOST_NAV_CACHE` / `_BASE_URL`                                  | `.signalpost_nav_cache.json` / NAV URL | cache file and endpoint                                                                           |

`OPENAI_API_KEY`, `GROQ_API_KEY` and similar variables are deliberately **ignored**.

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

The tests need no internet and no API key.

**Compliance check of a finished run**

```bash
uv run python signalpost_compliance_check.py --input out/envelopes.jsonl --report out/report.json
```

Prints `VIOLATION` / `FORMAT` / `WARN` findings for rules R1 to R6 above and exits `1` on any violation. Run it
before every submission.

---

## 11. Troubleshooting

| Symptom                                                             | Cause / fix                                                                                            |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `generated_by: deterministic`, `fallback_reason: no-key`            | no usable key; this is normal when no LLM is configured                                                |
| `unsafe-base-url`                                                   | `SIGNALPOST_LLM_BASE_URL` is plain `http://` to a remote host; use HTTPS                               |
| `api-error` with `HTTP 401`                                         | wrong key; the LLM is switched off for the run (`llm_disabled_reason`)                                 |
| `api-error` with `HTTP 404`                                         | model name not available to the key; set `SIGNALPOST_LLM_MODEL`                                        |
| `rejected:number-not-in-cited-fact` or `rejected:term-not-in-facts` | the model wrote something the facts do not state; the deterministic text was used (expected, harmless) |
| every `website` is `not_found`                                      | the registry has no `hjemmeside` for most small companies; this is expected (section 15)               |
| website `blocked` with a robots note                                | robots.txt disallows the agent, or could not be read (5xx or unreachable)                              |
| website `ambiguous` with "redirects to a different domain"          | the registry address now points at another domain; withheld on purpose                                 |
| many `source_error` on `registry`                                   | the machine cannot reach `data.brreg.no`; check the network allow-list                                 |
| `budget_exhausted` modules                                          | `--time-budget-s` was reached; raise it or run with `--resume`                                         |
| run interrupted                                                     | rerun the same command with `--resume`                                                                 |

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
| `check_llm.py`                     | verify the LLM configuration                                |

**Local-only helpers:** `select_entry_batch.py` (local test batches), `nav_*.py` (NAV feed diagnostics).

**Offline experiments (not part of the default run, not needed for the submission):**
`scripts/build_evidence_viewer.py` (older viewer), `score_competition_v3.py`, `score_company_completeness.py`,
`evaluate_research_agent.py`, `evaluate_external_footprint.py`, `ask_agent.py`, `run_scrapy_websites.py`
(needs the `crawler` extra), `run_annual_report_workforce_connector.py`, `run_sentiment_model.py` (needs the
`sentiment` extra), `extract_company_site_news.py`, `extract_company_site_activity.py`, `apply_identity_gate.py`,
`apply_verified_site_seeds.py`, `build_verified_observations.py`, `normalize_social_links.py`.

Removed during the audit: all search-engine and domain-guessing discovery code (see section 16).

---

## 14. Third-party sources, models and licences

| Source / component                                                                          | Use                                                | Terms                                                         |
| ------------------------------------------------------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------- |
| Brønnøysund Register Centre (Enhetsregisteret, Regnskapsregisteret, roles, group, subunits) | identity, accounts, roles, structure, workplaces   | NLOD 2.0                                                      |
| Company websites (registry-linked only)                                                     | description, news, careers                         | robots.txt respected; exact-entity gate first                 |
| NAV Arbeidsplassen job feed (optional, off by default)                                      | job ads                                            | NAV terms of use                                              |
| OpenAI-compatible chat model (optional)                                                     | rewrite of the summary only, default `gpt-4o-mini` | provider terms; key supplied by Builderr for the official run |

Python dependencies (pinned in `uv.lock`): beautifulsoup4, extruct, lxml, pydantic, pypdf, tldextract,
trafilatura, python-dotenv. Their licences have **not** been audited here; list them in your submission.
Not used: LinkedIn, Facebook/Instagram/Meta, Glassdoor, Indeed, Finn.no scraping, search engines, or any
credential tied to a personal account.

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

---

## 16. Audit changelog

Issues found in `signalpost-v1.zip` and what was done. Severity: **R** = breaks a challenge or project rule,
**B** = bug, **H** = hardening.

| #   | Sev | Finding                                                                                                                                                                                                   | Fix                                                                                                                                                                              |
| --- | --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | R   | A real `.env` with an API key (and the NAV token switch) was inside the zip; no `.gitignore` existed although the README claimed `out/` was ignored                                                       | `.env` and `out/` removed, `.gitignore` added, `.env.example` rewritten, regression test fails if a `.env` or a key-like string appears. **Rotate the key that was in the zip.** |
| 2   | R   | LLM defaulted to a personal Gemini endpoint with a mismatched OpenAI model name; it read several provider variables (`OPENAI_API_KEY`, `GROQ_API_KEY`...) and any `.env` found from the working directory | one variable (`SIGNALPOST_LLM_API_KEY`), neutral OpenAI-compatible default, HTTPS-only key transmission, project-root `.env` only                                                |
| 3   | R   | Websites could be discovered by name-guessing (`website_discovery.py`, also broken: missing import) and by the Brave search API (`discovery.py`, `run_brave_discovery.py`)                                | modules and script deleted, docs corrected, tests replaced by guard tests                                                                                                        |
| 4   | R   | The run needed a separate bulk-file download step and a matching `--expected-count`; a mismatch aborted before any output                                                                                 | `--bulk` optional (live API fallback), count check no longer aborts, all outputs always written                                                                                  |
| 5   | R   | One malformed or duplicated line in the batch raised and killed the whole run                                                                                                                             | tolerant reader: `failed` envelope for bad rows, duplicates collapsed                                                                                                            |
| 6   | R   | No time budget: a slow run is not scored                                                                                                                                                                  | `--time-budget-s` with terminal `budget_exhausted` envelopes, hard stop with grace period, NAV read capped by the budget                                                         |
| 7   | R   | Homepage redirects to another domain were accepted, breaking "evidence from the registry domain" and risking a wrong-company fact                                                                         | such pages are withheld as `ambiguous`                                                                                                                                           |
| 8   | B   | Expired job postings were still reported as open when every posting had expired                                                                                                                           | expired postings are dropped and noted                                                                                                                                           |
| 9   | B   | Job postings naming another hiring organisation were counted for this company                                                                                                                             | withheld and counted in `value.withheld`                                                                                                                                         |
| 10  | B   | The identity gate matched the organisation number after stripping all non-digits from the whole page, so unrelated digits could combine into a match                                                      | stand-alone token match (with space/dot grouping)                                                                                                                                |
| 11  | B   | `harRegistrertAntallAnsatte=false` rows could yield a headcount; stub profiles claimed `bankrupt=false` and `website=""`                                                                                  | unknown stays `null`; `employees` only when registered                                                                                                                           |
| 12  | B   | A company with no registry record still got a synthesis stating it is "a registered entity in Norway"                                                                                                     | no facts, no assertions, synthesis claim `not_available`                                                                                                                         |
| 13  | B   | `get_cost_stats`, `fallback_reason` and `model` were commented out, so the report's `llm` section and `third_party_cost_usd` the README promised were missing; `check_llm.py` printed `None`              | restored and wired into the report                                                                                                                                               |
| 14  | B   | Any robots.txt failure counted as permission to crawl; an unsafe secondary URL discarded the already fetched homepage                                                                                     | RFC 9309 semantics; secondary-page errors are isolated                                                                                                                           |
| 15  | B   | NAV: the first `orgnr` found anywhere in an ad was used; `retrieved_at` for NAV-only evidence came from an unrelated website record                                                                       | `employer.orgnr` is required; correct provenance                                                                                                                                 |
| 16  | B   | `public_activity` evidence pointed at the homepage URL and hash even when the item came from a news page                                                                                                  | evidence points at the page the newest item came from                                                                                                                            |
| 17  | H   | LLM summary was checked only for numbers, so names, products and URLs could be invented                                                                                                                   | every URL, e-mail and capitalised name must appear in the facts                                                                                                                  |
| 18  | H   | `tldextract` may download the public-suffix list at run time; downloads had no wall-clock limit; the registry URL field could contain several addresses or credentials                                    | offline suffix list, bounded reads, `normalize_homepage` hardened                                                                                                                |
| 19  | H   | Registry contact details (e-mail, phone) were stored in every profile; the bulk file was read fully into memory to hash it; `csv.Sniffer` guessed delimiters                                              | contact fields dropped, streaming hash, header-based delimiter                                                                                                                   |
| 20  | H   | Viewers loaded Google Fonts although documented as "no network"; the compliance checker flagged any host containing words such as "brave"                                                                 | fonts removed, search engines matched by domain, new rule R6                                                                                                                     |
| 21  | H   | README out of sync with the code (defaults, variables, files that do not exist)                                                                                                                           | rewritten                                                                                                                                                                        |

---

## 17. Submission checklist

- [ ] Rotate any key that was ever inside a zip or repository.
- [ ] `uv sync --frozen` works on a clean machine; `uv run python -m unittest tests.test_poc tests.test_signals tests.test_fixes` passes.
- [ ] A smoke run on your machine: exit code 0, `validation.passed = true`, compliance check shows no `VIOLATION`.
- [ ] Confirm the dollar, request and time budgets with Builderr; set `SIGNALPOST_LLM_*_LIMIT` and `--time-budget-s`.
- [ ] No `.env`, no keys and no `out/` directory in the repository; the commit hash you submit is the one you tested.
- [ ] Declare models, APIs and licences (section 14), expected cost per run, contact details.
- [ ] Update the copyright line in `LICENSE` if the holder should be someone else.
