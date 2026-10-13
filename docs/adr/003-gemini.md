# ADR 003 — Bounded, cached Gemini channel digests

Status: implemented adapter; live account/model verification pending.

Use Gemini's documented free tier and a simple HTTP adapter rather than a provider SDK.
Inject that boundary in tests. Summarize actual text posts for a chosen period, attribute claims,
request message-ID citations, and separate facts reported by the channel from interpretation.

Bound input and output, cache by source/model/prompt hash, serialize admission, cap attempts
per UTC day, and retain errors for accounting. Never automatically retry a failed generation.
Missing keys, quota failures and malformed/blocked responses produce a clear unavailable state
while the dashboard stays operational. Public source text is transmitted; media is not.

Consequences: limited free quota and changing models require operational checks. No claim of
perfect factual grounding or prompt-injection immunity; content is untrusted and plain-text
output is escaped. Provider may use free-tier data to improve products. Swapping providers
requires implementing `generate(context)` rather than changing views or ingestion.
