# ADR 002 — Current post state plus immutable metric observations

Status: implemented.

A post's identity is stable while its views and reactions change. Store unique posts by
channel/message ID and separate metric observations indexed by post/time. Store subscriber
observations separately. Latest values on posts support cheap lists; snapshots support history.

Use a database lease with a fencing token to serialize ingestion. Append snapshots and update
current values in one transaction; natural-key uniqueness is enforced by the database.
Unavailable values remain null. Rounded Telegram counts remain rounded observations, not
precision that we can reconstruct. Snapshot capture time differs from post publication time.

Consequences: extra rows and storage growth, but honest history without guessing deltas.
Only posts encountered in bounded preview scans get new observations. Initial import is the
latest page; cursor/high-water state catches up new gaps without an unbounded request.
