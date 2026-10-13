# Channel Radar

**Live application: [Channel Radar](https://channel-radar-0o24.onrender.com)**

[GitHub repository](https://github.com/Raccoohh/channel-radar)

Public Telegram channel analytics built with Python, FastAPI, SQLAlchemy and Gemini.
Add `@durov` in the browser: the channel appears immediately, a durable collection job
fetches the public preview, and the page updates when real observations arrive.
No Telegram credentials, Bot API or user sessions are used.

## Run locally

Python 3.12+; the local implementation is tested on 3.14.6. CI also targets 3.12.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
cp .env.example .env
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. SQLite is a convenient **local-only** default.
Production rejects SQLite and requires managed PostgreSQL and an HTTPS public URL.
`requirements.lock` pins the tested dependency set, including test tools. No real secrets
belong in this file, GitHub source files or `.env.example`.

## Configuration

| Variable | Meaning / default |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL; Neon PostgreSQL with `sslmode=require` in production |
| `GEMINI_API_KEY` | Secret from a free-tier Google AI Studio project; blank means AI unavailable |
| `GEMINI_MODEL` | `gemini-3.8-flash`; configurable as provider availability changes |
| `SCHEDULER_TOKEN` | Random secret, at least 32 characters in production |
| `PUBLIC_URL` | Exact external origin, used to reject cross-origin POST requests |
| `COLLECTION_INTERVAL_SECONDS` | 3600; minimum 300 |
| `WORKER_ENABLED` | `true`; disable in controlled offline tests |
| `MAX_CHANNELS` | 10, public workspace admission cap |
| `AI_DAILY_LIMIT` | 20 attempts per UTC day across the whole workspace |
| `ENVIRONMENT` | `development` or `production` |

Set secrets in the hosting dashboard; never paste them into issue reports. The development
`.env` is ignored. Copy the Render-generated scheduler secret into a GitHub Actions secret
named `SCHEDULER_TOKEN`. A missing AI key is surfaced honestly; it does not generate a fake digest.

## Architecture and schema

A modular monolith avoids queue infrastructure and frontend build dependencies:

```text
Browser → FastAPI/Jinja views → SQLAlchemy → PostgreSQL
                   ↓                 ↑
           Digest service        Collector ← durable due/lease fields
                   ↓                 ↓
                 Gemini         https://t.me/s/<username>
GitHub Actions → authenticated /internal/collect
```

* `channels`: normalized unique username, source metadata, health, last attempt/success,
  due time, lease/fencing token and incremental catch-up state.
* `posts`: unique `(channel_id, message_id)`, plain text, UTC publication time, canonical
  Telegram URL and latest observed metrics. Indexed by channel/publication time.
* `post_metric_snapshots`, `channel_metric_snapshots`: append-only observations with UTC
  capture time. Indexed by entity/time. Null means unavailable, **not zero**.
* `collection_runs`: durable attempt status, observation count and safe error details.
* `ai_digests`: provider/model, period, source hash, text, status, source count and time.
  Includes failed attempts so quota accounting cannot be bypassed by repeated failures.
* `controls`: one row serializes channel admission and global AI budget decisions across
  processes. It is seeded by the migration, not lazily by concurrent requests.

Numeric summaries describe the **collected sample**, never all historical channel activity.
Average views excludes unknown values; total views sums latest observations of posts
published in the selected period. It is not unique reach or views gained during the period.
Subscriber charts use capture time; post filtering uses publication time.

### Ingestion and concurrency

The initial import is Telegram's most recent public-preview page (normally 20 posts).
Subsequent runs always refresh that page, preserving metric history for visible posts.
New post IDs are traversed backwards until the previously completed high-water mark.
Each run fetches at most four pages. A saved cursor resumes large gaps; a saved upper bound
prevents new arrivals during catch-up from skipping unseen posts. No complete historical
archive is promised. Older posts retain observations but do not receive a fresh snapshot
unless they are encountered again in a scanned page.

A conditional database UPDATE acquires a five-minute lease. The successful claimant commits
the lease before doing HTTP work. A token-checked UPDATE fences the final transaction; a
stale worker cannot publish after replacement. Post changes, snapshots, channel state and
successful run completion commit together. Database uniqueness is a final backstop.
Repeat HTTP triggers respect `next_collection_at`, so they cannot force unlimited scraping.
Transient source failures retry up to three times per page with bounded delays and timeouts;
failed collections retry after 15 minutes. After process death, the next worker recovers
expired leases and marks abandoned runs as interrupted.

Private, nonexistent, restricted and preview-disabled channels may be indistinguishable
without Telegram credentials; the UI says that public preview is unavailable.
Rounded K/M counts are converted to integers, still representing **rounded source data**.
Ordinary visible reaction counts are summed; paid Stars are excluded. No reaction widget
means N/A. Telegram's media stays on Telegram; this service does not download it.

### AI behavior

Gemini produces a Ukrainian digest from at most 50 newest text posts, at most 3,000 characters
per post and 24,000 characters of source JSON. The prompt distinguishes channel claims from
interpretation, treats post text as untrusted data, and requests message-ID references.
Output is escaped text. Attribution is helpful but is not independent fact verification.

The source/model/prompt-version fingerprint caches successful results for six hours. A
global 90-second admission cooldown and a daily attempt cap bound quota consumption.
There is a 35-second provider timeout, no automatic expensive AI retries, and explicit
handling of 429, HTTP failures, missing key, blocked/truncated or malformed output.
An older saved digest remains visible with its generation time. Provider failure never
prevents access to the rest of the dashboard. Media content is not sent to Gemini.
The free Gemini tier may use input/output to improve Google's products; only public channel
text is submitted. The configured free-tier key was verified with a live response; quotas
and temporary model availability remain provider-dependent.

## Verification

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m alembic upgrade head
python -m alembic check
```

Pytest blocks sockets and DNS with `pytest-socket`; fixtures, `httpx.MockTransport` and
`ASGITransport` replace external HTTP. On Windows the event loop's internal wake-up
socketpair is allocated before those guards; tests still cannot open network connections.
The suite covers parsing, absent metrics, invalid names/URLs, retries, idempotency,
snapshot arithmetic, concurrent leases, stale-worker fencing, catch-up, API states,
XSS escaping, quotas and AI degradation. CI separately checks PostgreSQL using a disposable
service and a mocked Telegram source (`scripts/postgres_smoke.py`); this is outside pytest.

See [verification evidence](docs/VERIFICATION.md) for what was actually run. A configured
workflow is not a passing hosted CI run; a local success is not a production smoke test.

## Deployment: Render Free + Neon Free

`render.yaml` describes a single Python web service. Migrations execute at startup because
free hosting lacks the usual paid pre-deploy facilities. Run **one Uvicorn worker** in this
deployment; database leases also protect overlapping deploy instances. The health endpoint
checks database access and migrated control state. `.dockerignore` excludes credentials.

1. Create a Neon Free project; copy its pooled PostgreSQL URL with TLS enabled into Render.
2. Create the Render service from this repository using `render.yaml` or equivalent settings.
3. Set `DATABASE_URL`, `PUBLIC_URL`, `GEMINI_API_KEY`, `SCHEDULER_TOKEN`, `ENVIRONMENT=production`.
4. Set GitHub repository variable `RADAR_URL` and secret `SCHEDULER_TOKEN`.
5. Run **Collect channels → Run workflow**. Confirm a successful run, source health and
   an additional database snapshot. The hourly cron is at minute 17 to avoid peak load.
6. Open the public URL, add a channel, inspect post details and digest, then restart and
   verify that channels/posts/history survive. Repeat after the final deployment.

The running service uses Render Free and Neon Free in Frankfurt. It was created using the
public Git repository URL. To release a new commit, use Render **Manual Deploy → Deploy
latest commit**; GitHub CI alone does not prove that production has the latest commit.
`PUBLIC_URL` must match Render's assigned domain exactly, including its generated suffix.

Free-plan facts checked on 2026-10-13:

* [Render](https://render.com/docs/free): sleeps after 15 minutes without inbound traffic;
  cold start can take roughly a minute; ephemeral local disk; monthly free compute limits.
  Render Free Postgres expires after **30 days**, so it is not the chosen durable store.
* [Neon](https://neon.com/blog/neon-free-plan-1-gb-per-project): Free projects include 1 GB
  storage and 100 CU-hours/month; idle compute scales to zero. Its cold start and quotas
  remain constraints. Monitor storage because snapshots accumulate.
* [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
  can be delayed/dropped under load and are disabled in inactive public repositories after
  60 days. They run on the default branch. This is hourly best effort, not an SLA.
* [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) lists a free tier for the
  selected model. Actual project quotas and model access must be verified with that key.

The external trigger wakes Render and waits for due collection; in-process scheduling gives
fast initial collection while the process is awake. Persistent due dates and leases support
recovery after downtime. Neither mechanism guarantees collection during provider outages.

## Scope and limitations

Public shared workspace; no user accounts or roles, as requested. Up to ten channels,
one active scrape at a time per process, global admission/AI limits. There is no public
delete/retry endpoint, so anonymous users cannot destroy observations or force repeated jobs.
This also means an operator must remove erroneous channels using the database if the demo
fills up. Multi-channel comparisons, alerts, full-history import, media analysis and exports
are deferred until mandatory deployment acceptance is complete. No Redis/Celery/Kubernetes.

An authenticated scheduler does not make a public demo immune to abuse. Channel admission
is bounded, expensive requests are throttled, hosts are fixed, redirects are not followed,
and HTML is rendered as escaped text with a restrictive CSP. Production database credentials
must never be shared publicly. Long-term snapshot retention/archival is an operator task.

[ADRs](docs/adr/) · [Agent conventions](CLAUDE.md) · [Interview preparation](docs/INTERVIEW.md)
