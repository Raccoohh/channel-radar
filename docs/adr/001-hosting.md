# ADR 001 — Render Free with an independent Neon database

Status: implemented and verified on Render Free + Neon Free, 2026-10-14.

The product needs a public Python process and persistent cloud data without paid services.
Render Free supports a normal ASGI app but sleeps and has ephemeral disks. Its free database
expires after 30 days. Choose Neon Free for PostgreSQL and Render Free for one web process.
Use hourly GitHub Actions to wake and trigger collection, plus the local worker for fast
first collection. Persist due dates, leases and cursors in PostgreSQL.

Consequences: cold starts and cron delays are visible limitations, not hidden guarantees.
Provider outages and quota exhaustion remain possible. Startup runs Alembic migrations;
schema changes must preserve rolling-deploy compatibility. Avoid provider-specific storage
so relocation to another Python host is inexpensive. Sources and plan date are in README.

Reliability follow-up, 2026-10-15 (local evidence; live acceptance required): a scheduler waits
for readiness, submits one POST and observes a busy cycle by process-scoped UUID. Keep receipts
bounded in the existing single worker instead of adding a workflow table or queue. On restart
or cross-instance routing the observer fails with unknown result; it never infers success from
a newer no-op. Existing PostgreSQL leases, fencing and per-channel run history stay durable.
This trades successful observation across restarts for no schema migration and fail-closed
semantics. A future multi-worker deployment would need durable cycle receipts. Provider cold
start is not declared solved until a natural scheduled run after inactivity is observed.
