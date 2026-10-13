# Interview preparation

1. **Why a monolith?** Ten channels do not justify message brokers or microservices. FastAPI,
   a bounded collector and PostgreSQL provide independently testable modules in one deployment.
2. **What is a post's identity?** `(channel_id, message_id)`, enforced by a database unique
   constraint. Username normalization prevents duplicate channel identities.
3. **What makes ingestion idempotent?** Existing identities are updated, not inserted again.
   A new successful observation intentionally adds a snapshot; trigger retries honor due time.
4. **How are concurrent jobs handled?** A conditional UPDATE acquires a lease, then HTTP runs
   outside the transaction. A matching token and unexpired lease are required to publish.
5. **What happens after a crash?** Due dates remain in the DB. After five minutes the lease
   expires, another worker claims the channel, and the abandoned run becomes interrupted.
6. **Why both latest values and snapshots?** Latest values make list queries cheap; immutable
   observations preserve trends. Both are committed atomically to prevent inconsistent views.
7. **What does null mean?** The source did not expose a metric. It is not zero. Average views
   divides by posts with a known view count; the UI displays coverage of observed metrics.
8. **How does incremental collection handle busy channels?** It fetches up to four pages and
   persists a continuation cursor. A saved initial head prevents new arrivals from causing gaps.
9. **Does every old post get refreshed?** No. Only posts encountered in preview scans. This
   keeps free-tier traffic bounded; saved historical observations remain accessible.
10. **Why external scheduling?** Render sleeps. GitHub Actions wakes the service hourly; the
    in-process worker handles fast initial collection. Cron is best effort, not guaranteed.
11. **How is AI cost bounded?** Source character/post caps, output token limit, cached source hash,
    a 90-second global cooldown, daily attempt budget and no automatic provider retries.
12. **How do you handle hallucinations and prompt injection?** The prompt treats posts as data,
    requires attribution and separates interpretation. This reduces risk but is not a proof.
    Output is escaped; the model has no tools or secrets in its context.
13. **How do you test without a network?** Socket/DNS guards plus HTTPX mock/ASGI transports.
    Integration with disposable PostgreSQL runs separately from pytest.
14. **What protects against SSRF and XSS?** Strict username/host validation, fixed HTTPS Telegram
    URL, no redirects, and escaped plain text with a restrictive CSP.
15. **What would you improve for production scale?** Authentication/admission policy, retention,
    monitoring and provider alerts, more robust source drift detection, and a separate worker
    when measured throughput or hosting requirements justify it. No premature queue system.
