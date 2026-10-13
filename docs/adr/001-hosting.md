# ADR 001 — Render Free with an independent Neon database

Status: selected; deployment verification pending.

The product needs a public Python process and persistent cloud data without paid services.
Render Free supports a normal ASGI app but sleeps and has ephemeral disks. Its free database
expires after 30 days. Choose Neon Free for PostgreSQL and Render Free for one web process.
Use hourly GitHub Actions to wake and trigger collection, plus the local worker for fast
first collection. Persist due dates, leases and cursors in PostgreSQL.

Consequences: cold starts and cron delays are visible limitations, not hidden guarantees.
Provider outages and quota exhaustion remain possible. Startup runs Alembic migrations;
schema changes must preserve rolling-deploy compatibility. Avoid provider-specific storage
so relocation to another Python host is inexpensive. Sources and plan date are in README.
