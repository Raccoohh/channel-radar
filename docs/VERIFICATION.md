# Acceptance evidence

Verified on 2026-10-14 (Europe/Kyiv). Live observations are time-dependent.

Application: https://channel-radar-0o24.onrender.com

Repository: https://github.com/Raccoohh/channel-radar

## Local audit remediation — pending release approval

On 2026-10-14, local changes based on `dfb2323` address audit findings B01–B07.
These results do **not** describe a new production deployment:

* Python 3.14.6: **139 offline tests passed**, including 78 new regression cases.
  Existing tests remain unchanged. Socket/DNS guards are still enabled.
* Combined statement/branch coverage: **92%** (728 statements, 188 branches), measured
  with an external coverage tool; no runtime dependency was added.
* Ruff lint and formatting passed. Working-tree credential-pattern scan found no matches
  across tracked and new non-ignored files; all 40 installed locked versions matched.
* Disposable local PostgreSQL 18.4: upgrade/check/downgrade/upgrade and expanded smoke
  passed. Checks include concurrent claims, retained snapshots, partial collection failure,
  atomic rollback, concurrent AI admission and cache reuse. No schema migration is needed.
* Local browser with synthetic providers: `t.me/durov` form submission, Collecting → Healthy,
  two fixture posts, metrics, post details and a visible AI failure with a working dashboard.
  No real Gemini quota was used for remediation tests.
* Previously captured real public HTML for `durov` and `telegram` still parses 20 posts each.
* Hosted CI, Render release/commit verification and live acceptance for these changes are
  pending explicit publication approval. The production observations below are historical.

## Automated checks

* Local Python 3.14.6: **61 offline pytest cases passed**, without warnings after asserting
  the expected socket-guard warnings. HTTP is mocked; sockets and DNS are blocked.
* Ruff lint and format passed (28 Python files); staged credential audit passed.
* [CI for 1c742be](https://github.com/Raccoohh/channel-radar/actions/runs/38469435400)
  passed on Python 3.12 and 3.14. PostgreSQL smoke and migration upgrade/check/downgrade/upgrade
  passed against a disposable CI database. The first CI run found a formatting issue;
  it was fixed and subsequent runs passed.
* Neon production: Alembic upgrade to head and schema consistency check passed. No production
  downgrade or destructive schema test was performed.

## Production checks

* Render Free, Python 3.14.6, Frankfurt; Neon Free PostgreSQL 18 in Frankfurt. `/healthz`
  returns HTTP 200 and verifies the migrated database. No paid tier was enabled.
* Added `@durov` using the public browser form: it appeared immediately as Collecting,
  then automatically became Healthy with **20 real posts** and 10,400,000 source-reported
  subscribers. Abbreviated Telegram metrics remain rounded source observations.
* A controlled authenticated `/internal/collect` returned `{"busy":false,"collected":1}`.
  Post count stayed at 20; post snapshots grew from 20 to 40; channel snapshots from 1 to 2.
  Both runs succeeded. Database query found **zero duplicate post identity groups**.
* After later scheduled wake-ups and a cold start: 20 posts, **100 post snapshots and
  5 channel snapshots**, with five successful collections. Latest views for post #552
  changed from 785,000 to 793,000; older observations remained available.
* Browser channel page, subscriber trend, post #552 details, original Telegram link and
  snapshot table were checked. Changing 30 to 365 days changed the sample from 2 to 20 posts.
  HTTP checks for 7 and 365 days also passed.
* Re-adding `@durov` returned HTTP 200 with `created: false`. A foreign URL was rejected in
  the browser with a readable error and by the API with HTTP 422. Unauthenticated collection
  returned HTTP 401. Missing/private preview parsing is covered by offline fixtures;
  no private Telegram session was used.
* Real data exposed binary-float truncation of `4.18M`. Decimal conversion and regression
  cases now yield exactly 4,180,000. Earlier snapshots remain historical observations;
  new collections use the corrected parser.
* On 2026-10-14 at 02:15 UTC, the public Generate digest button produced a real Ukrainian
  digest using `gemini-3.1-flash-lite`, two source posts, message-ID citations, separate
  interpretations and explicit limitations. A repeated request showed the same cached
  generation time. The result is stored in PostgreSQL.
* Real provider failure was observed: Gemini 3.8 overload/timeout produced a visible error
  while channels, metrics and navigation continued working. Flash-Lite was selected after
  successful live checks; no synthetic fallback content was inserted.

## Scheduling and persistence

* Repository variable `RADAR_URL` and secret `SCHEDULER_TOKEN` are configured.
* [Manual scheduler](https://github.com/Raccoohh/channel-radar/actions/runs/38468858418)
  succeeded. [Scheduled run](https://github.com/Raccoohh/channel-radar/actions/runs/38489551459)
  succeeded and woke the sleeping service. Health and authenticated collection returned 200.
  The startup worker had already collected the due channel, so the endpoint correctly
  reported `collected: 0` for that scheduled run.
* Scheduler delays were observed. Hourly cron is best effort and does not guarantee hourly
  observations. Persistent due dates permit collection on the next wake-up.
* Data survived code/configuration deployments and a cold start after several hours.
  Render confirmed a manual restart at 05:16 Europe/Kyiv. The channel, 100 post snapshots,
  five channel snapshots and successful digest remained available afterward. Repeating AI
  used digest ID 4 without creating a fifth attempt, confirming persisted cache behavior.

## Evidence limits

These checks demonstrate deployed flows, not a load test or uptime guarantee. The shared
public workspace is capped at ten channels. Render/Neon may sleep or exhaust free quotas;
Gemini may be unavailable. Initial import is the latest preview page, not a full archive.
Snapshots accumulate and need an operator retention policy for long-term use.

Browser captures are kept locally under ignored `artifacts/`: overview, post history,
provider fallback and successful digest. No secrets or database files are tracked.
