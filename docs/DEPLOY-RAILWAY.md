# Deploy on Railway (PDF Mini App + API)

## Health checks

Configure Railway **Healthcheck Path** to `/health` (liveness). Use `/health/ready` for readiness that verifies **database** connectivity (returns **503** if the DB is unreachable).

Optional JSON request logs: set **`LOG_JSON=1`** so each HTTP request emits one JSON line (method, path, status, `duration_ms`, `X-Request-ID`). Hot paths (`/redeem-payment-code`, `/generate-pdf`, `/api/my-purchase-history`) include `"hot_path": true`.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token (@BotFather). Required for Telegram + optional subprocess bot. |
| `WEB_APP_URL` | Public HTTPS origin of this service (no trailing slash). If unset, [`effective_public_base_url`](../app/public_url.py) uses `RAILWAY_PUBLIC_DOMAIN` or Fly env. |
| `DATABASE_URL` | PostgreSQL URI (recommended: Supabase **pooler** host, not raw `db.*.supabase.co` if your host lacks IPv6). See `.env.example`. |
| `PORT` | Injected by Railway; the app reads it in Docker/`__main__.py`. |
| `NOWPAYMENTS_API_KEY` / `NOWPAYMENTS_IPN_SECRET` | Crypto invoices + IPN webhook (set on the **API** service). |
| `START_TELEGRAM_BOT_SUBPROCESS` | `1` (default) runs polling in-process; set `0` if another worker polls the same bot token. |
| `CORS_ORIGINS` | Optional comma-separated origins; empty = `*`. |
| `ADMIN_TELEGRAM_IDS` / `ADMIN_API_SECRET` | Admin Mini App access. |
| Rate limits | `RATE_PREVIEW_PER_HOUR` (default 20), `RATE_FINAL_PER_DAY` (8), `RATE_INVOICE_PER_HOUR` (6), `RATE_REDEEM_PER_HOUR` (10). Set to `0` to disable a gate. |

## SQLite / `DATA_DIR`

If you run **without** `DATABASE_URL`, the app uses SQLite under `DATA_DIR` (default `./data`). On Railway the filesystem is **ephemeral** unless you attach a **volume**. Production should use **Postgres** (`DATABASE_URL`).

## Supabase migrations order

Apply SQL migrations in order:

1. [`supabase/migrations/20260207120000_enable_rls_backend_tables.sql`](../supabase/migrations/20260207120000_enable_rls_backend_tables.sql) — enables RLS and revokes PostgREST roles from backend tables.
2. [`supabase/migrations/20260207140000_events_rls_permissive_policy.sql`](../supabase/migrations/20260207140000_events_rls_permissive_policy.sql) — adds a permissive policy on `public.events` so the FastAPI database role can insert/select analytics rows.

Without (2), PostgreSQL 15+ can deny access to `events` for non-superuser pooler roles, which breaks purchase history and redemption logging.

## Telegram Mini App URLs and `tg_user_sess`

- Per-user links (reply keyboard, admin panel for a known user) use [`mini_app_entry_url(user_id)`](../app/telegram_bot.py), which appends **`tg_user_sess`** when possible so the Mini App can authenticate without `initData`.
- The **chat menu button** (`set_chat_menu_button`) uses `mini_app_entry_url()` **without** a user id — Telegram does not provide a user context there, so that URL may have **no** session query param. Users should open the app from the bot **keyboard** or **`/start`** for the signed session when needed.

## Legacy Render files

[`render.yaml`](../render.yaml) and related scripts are optional references; this project is deployed on **Railway**.

## Backfill redemption events

If older redeems lack `payment_code_redeemed` rows in `events`, run (after backup):

```bash
python scripts/backfill_payment_redeemed_events.py --dry-run
python scripts/backfill_payment_redeemed_events.py --apply
```
