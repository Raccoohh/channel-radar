# Acceptance evidence

Date: 2026-10-13. This file separates observed results from unverified deployment work.

## Observed

* Local Python 3.14.6; dependencies installed into `.venv` and versions locked.
* 57 offline pytest tests passed in 1.94 seconds (before the saved catch-up-target addition).
* Ruff check passed. Alembic initial upgrade and schema consistency check passed.
* Real public `https://t.me/s/durov` HTML fetched and parsed: 20 posts, title, subscribers,
  views and ordinary reaction counts. Source counts are rounded and time-dependent.
* Browser form `@durov`: immediately displayed Collecting, then automatically updated to
  Healthy with 20 real posts and 10,400,000 source-reported subscribers.
* Public GitHub repository created: https://github.com/Raccoohh/channel-radar (code push pending).

## Still to verify

* Final regression after remaining fixes, browser channel/post views and period filter.
* Repeated live collection, history growth, persistence across restart.
* Actual Gemini response with a free-tier account and key.
* PostgreSQL migrations and collection smoke; hosted CI.
* Render production deployment, public form scenario, external scheduler and production restart.

No public application URL is claimed until these deployment checks succeed.
