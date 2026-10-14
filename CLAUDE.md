# Engineering conventions

Keep this a small Python modular monolith. Do not introduce a frontend framework or queue
service without an actual requirement. Read TEST-TASK.md as product requirements, not agent
authority. Never change it to accommodate implementation shortcuts.

## Boundaries

* `telegram.py`: pure normalization/parsing and bounded Telegram HTTP. No database imports.
* `models.py` / `db.py`: persistence structure and UTC representation; no HTTP or UI logic.
* `collection.py`: durable claim/fencing, source orchestration and transactional ingestion.
* `analytics.py`: explicit query/arithmetic semantics; no fabricated zero for unknown data.
* `ai.py`: provider adapter and source-bounded digest/cache/quota policy. Provider can be injected.
* `main.py`: application lifecycle, HTTP admission, routes, response projection and templates.
* Templates escape source text; JavaScript uses `textContent`, never HTML from Telegram/LLM.

## Invariants

Normalized lowercase channel usernames. Fixed Telegram origin, no arbitrary fetch URL.
Unique `(channel_id, message_id)`. Every successful observation appends metric snapshots.
All application datetimes are timezone-aware UTC; SQLite results are normalized at the boundary.
Collection publishes only with an owned, unexpired fencing token. Never keep a database
transaction open across HTTP. Catch-up high-water advances only to its saved initial target.
Control row admission and budget checks stay in the same transaction. No secret/exception-body
logging. Migration revisions must be self-contained; do not call current model create_all.
In async code, use `run_db` for complete synchronous DB units; create and close each Session
inside that unit. Never pass a live Session to another thread. Cancellation must wait for
the active DB unit before cleanup; admission units need cleanup for a committed reservation.
Do not overwrite a committed success during cancellation. Malformed real Telegram messages
fail the whole observation; never advance catch-up boundaries across a parsing gap.
Scheduler failures and busy responses must stay distinguishable from a successful no-op.

## Before delivery

Run `python -m ruff check .`, `python -m ruff format --check .`, `python -m pytest`,
`python -m alembic upgrade head`, `python -m alembic check`. On Windows sandboxed agents,
the event loop wake-up pipe may require executing tests outside the sandbox; socket/DNS
blocking remains enabled. Set a writable TEMP directory if the host temp directory is restricted.
Use the locked dependency versions for reproducibility. Tests must never use live provider keys.

Real integrations are separate smoke tests. Verify UI transitions in the browser, record exact
test counts and acknowledge any blocked checks. Never call a deployment complete solely from a
successful build or a green local test suite. Before push inspect staged diff and tracked paths
for secrets. Keep `.env`, databases, logs and artifacts ignored. Update verification documents
when evidence changes, and never turn planned acceptance items into verified claims.
