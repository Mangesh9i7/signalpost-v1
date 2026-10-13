# External discovery and sentiment

External intelligence is now 55/100 in the revised competition rubric. Individual connectors remain
quarantined until their data-rights and held-out gates pass; planned or experimental output earns zero.

## Missing-website discovery: not used

The submitted agent takes a company's website **only** from the registry field `hjemmeside`
([Brønnøysund entity registry](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html),
[NLOD 2.0](https://data.norge.no/nlod/en/2.0)). If the registry has none, the website is `not_available`.

Earlier experiments found sites through a search-engine API and by guessing domains from the company name.
That code (`discovery.py`, `website_discovery.py`, `scripts/run_brave_discovery.py`) has been removed from the
project: websites found by search or name-guessing are outside the project's source rules, and
`signalpost_compliance_check.py` (rule R2/R5) flags any envelope that contains one. A regression test
(`tests/test_fixes.py`) fails if such code or a search-engine endpoint reappears.

## Norwegian company sentiment

The preferred first comparator is
[`NOSIBLE/financial-sentiment-v1.2-base`](https://huggingface.co/NOSIBLE/financial-sentiment-v1.2-base)
(Apache-2.0). It is finance-oriented and multilingual, but its published Norwegian result is based on a
small translation-derived slice, so it is only a hypothesis until tested on exact-company Norwegian news.
`run_sentiment_model.py` pins the model revision, uses the required system prompt with thinking disabled,
constrains generation to one of the three allowed label tokens, and preserves the input evidence metadata.
The checked-in three-snippet output is a synthetic runtime smoke test, not evaluation evidence.

The native-Norwegian comparator is
[`ltg/norbert3-base_sentence-sentiment`](https://huggingface.co/ltg/norbert3-base_sentence-sentiment)
(CC BY 4.0). Its source domain is reviews rather than company events. Pin and inspect its remote model code
before use. The larger NorBERT variant may be added only if it materially improves the same frozen corpus.

[NoReC](https://github.com/ltgoslo/norec) and
[`ltg/norec_sentence`](https://huggingface.co/datasets/ltg/norec_sentence) are useful research references,
but the text is CC BY-NC 4.0 and is not the commercial POC corpus. No clean, native, human-labelled Norwegian
company-news corpus with clear commercial reuse rights was found.

POC sentiment gate:

- at least 300 recent exact-company snippets, independently labelled and adjudicated, with positive,
  neutral, and negative support; include mixed items when the product intends to publish that class;
- exact-entity errors: zero;
- evidence support: 100%, including source URL, retrieval time, content hash, and claim span;
- positive and negative precision at least 90%, macro-F1 at least 0.80, and coverage at least 50%; and
- company-owned marketing copy always rejected as independent sentiment.

For production claims, use at least 600 entity-event bundles with national/local, Bokmål/Nynorsk, sector,
name-collision, negation, allegation, and stale-item stress cases. A company-level rollup requires at least
two independent publishers; otherwise expose only dated item-level context or abstain.
