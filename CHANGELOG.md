# Changelog

## 0.2.0 — 2026-10-25

- Add an optional LangChain tool callback handler with local JSONL recording, concurrent run matching and argument filtering; the base CLI retains zero runtime dependencies.
- Include a local LangChain tool demonstration and integration documentation with explicit field mapping and handled-error limitations.
- Reject report outputs that alias the input trace through hard links or symbolic links before writing; unrelated report files can still be replaced.

## 0.1.0 — 2026-10-23

- Analyze a normalized JSONL tool-call trace offline, including concurrent and out-of-order events.
- Report repeated calls, evidence of failed retries, missing/orphan results and partial timing.
- Enforce call-count, error-count and observed-duration budgets in CI.
- Record local synchronous and asynchronous tools, including concurrent and cancelled calls, without recording output bodies.
- Include strict input handling, English/Chinese docs and scripted examples.

Initial alpha; no native vendor trace import or model-quality evaluation.
