# Scheduler reliability — release design and local evidence, 2026-10-15

Base: `d082255b7419974f612663bc676bb3ff2cb1cb58`. The investigation and local verification
below preceded publication. Times below are UTC unless explicitly marked Kyiv.

## Facts and unknowns

| Evidence | Finding |
|---|---|
| [Scheduled run 38604420465](https://github.com/Raccoohh/channel-radar/actions/runs/38604420465), job 115991529688 | Started 17:10:46; at 17:11:30 GET health had returned ok and POST returned 409 with busy=true, attempted=0. Exit 22. |
| Production database snapshot from release investigation | Runs 34 (tgsn_ua) and 35 (durov) succeeded at 17:11:29.704 and 17:11:30.810. Thus POST overlapped the worker cycle, rather than demonstrating source failure. |
| Published code/config | Lifespan starts a worker immediately; collect_due holds a local lock across its cycle. Busy returned no correlation. Workflow treated any 409 as final failure. Render start command and render.yaml use --workers 1; worker_enabled defaults true. |
| [Scheduled run 38634190435](https://github.com/Raccoohh/channel-radar/actions/runs/38634190435), job 116093039864 | Started 20:41:30. At 20:43:30 and 20:45:40 curl reported 120002 ms timeout, zero bytes. Job exited 28 at 20:45:41 before collection POST. |
| Old workflow configuration | Health max-time=120, retry-delay=10, retry-max-time=180. This admitted two 120s requests (about 250s total); retry-max-time is not a hard end-to-end deadline. |
| Render Events / release logs | Latest deploy remained d082255, Live since 16:11, no subsequent manual restart/redeploy in Events. Startup command runs Alembic then Uvicorn. App startup at original deploy was successful. Events do not prove when a sleeping instance received the later GitHub traffic. |
| Application logs / later snapshot | Available 24h ERROR/Traceback searches had no matches. At 21:06 a new instance served requests and collected all three channels successfully; health was 200 at 21:08. Historical snapshots remained unchanged. |

**Confirmed root cause of the busy false alarm:** coordination protocol gap. The trigger
could not observe the already-running cycle and therefore failed despite its subsequent
success. Keeping the 409 signal is correct; waiting for its exact result is the missing part.

**Confirmed timeout symptom, unresolved underlying cause:** no health response arrived within
the two request budgets. Existing logs have no request-level connection/TLS/edge trace for that
interval. Render wake delay/routing, transient runner-to-service networking, startup migrations
or DB readiness are hypotheses, not proven causes. Absence of application exceptions does not
prove the application or hosting healthy during the interval. Increasing retries cannot by
itself prove a provider availability issue solved.

## Contract and bounded execution

POST retains its old completion summary/status. A busy response additionally identifies the
active whole-cycle UUID, allocated before its first await. GET /internal/collect/{run_id}
uses the same bearer authorization and returns running/success/failed plus the summary.
It never launches work or changes collection/database state. Errors/cancellation settle the
receipt as failed; status success requires no failed or skipped attempts. A later cycle cannot
overwrite the requested receipt. A failed receipt can include zero failed channel attempts
when the cycle itself was cancelled or failed before claim; state still fails the client.

Receipts are process-scoped, retained at most 30 minutes / 256 cycles. Restart, eviction or
cross-instance routing gives 404 and a failed confirmation. This is intentionally conservative:
404 does not mean ingestion failed, but the workflow cannot certify it. It does not silently
follow a new cycle or reacquire a lease. No database schema changes or token reuse.

The Python client uses one global monotonic deadline (660s), a readiness deadline (300s),
bounded safe GET retries with 2/4/8/15s backoff, 30s GET wall/inactivity limits and <=10s connect.
The single collection POST is capped at 480s and the remaining overall budget. A transport
failure after POST is ambiguous and fails without retry. On 409 the client polls only the
specified UUID. Terminal failure, missing/mismatched receipt, malformed response, auth error,
unconfirmed skip or deadline gives exit 1. Confirmed completion/no-due gives exit 0. 503 and
other real failures never become green by retrying POST. Redirects are not followed; tokens,
response bodies and exception details are not logged. GitHub job limit is 15 minutes including
checkout/setup/install. No continue-on-error or new third-party service.

## Verification and invariants

50 new offline regression cases; **189 total passed** on Python 3.14.6. Coverage for app plus
the scheduler client: **92%** (900 statements, 50 missed; 244 branches, 30 partial).
Ruff lint and format passed, 34 files. Existing 139 tests were not changed or removed.

New cases cover slow readiness, 503/502/408/429 and connection/read failures, hard wall deadline,
shared phase budget, permanent HTTP errors, no-due, success/failure summaries, busy transitions,
busy deadline, auth, malformed JSON, secret-safe logging, exact ID mismatch, restart/eviction,
retention bounds, cancellation, actual ASGI client/endpoint integration, preserved history,
duplicate prevention, and lease expiry/takeover fencing. Workflow invokes the tested client
and propagates its exit code. Telegram/Gemini are mocked; socket/DNS guards remain enabled.

Disposable PostgreSQL 18.4 at 127.0.0.1:54339/scheduler_reliability: upgrade/check, expanded
smoke, downgrade/upgrade/check passed. Smoke drives the actual ASGI status endpoint and client
over PostgreSQL for busy success/failure, plus prior concurrent claim, idempotency, snapshots,
rollback and AI cache checks. This used ASGITransport, not live HTTP or production data.
SQLite disposable migration cycle also passed. Local PostgreSQL stopped after checks.

No change to database models/migrations, parser, Gemini integration, body limits, DB thread
units, source limits, public UI or Render configuration. Collection claims and publish fencing
remain unchanged. The only collector behavioral addition is bounded cycle result observation.

## Release gate

Publication was authorized by the owner on 2026-10-15. Existing GitHub
RADAR_URL and SCHEDULER_TOKEN suffice. No new Render secret, tier, worker count or schema.
The CI smoke command now uses module execution so it can import the scheduler module reliably.

After authorization: CI must pass; deploy this backend/client pair and verify its SHA; check
authenticated status and preserved data; observe a **natural scheduled run after inactivity**.
Record health timings, receipt ID and terminal outcome, not only a green workflow badge.
A warm manual no-op is insufficient proof. During staggered deployment, an old backend busy
response lacks correlation and the new client fails explicitly; do not bypass that failure.

Remaining risks: provider outage beyond deadline, delayed GitHub schedule, transient receipt
loss on restart/rolling deploy, unknown POST result on disconnect, and no cross-instance result
recovery. These are visible failures, not a claim that cold start is fully solved.
