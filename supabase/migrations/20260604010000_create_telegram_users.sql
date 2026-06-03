-- Persistent Telegram user directory for later bot broadcasts.
-- Backend writes through DATABASE_URL; anon/authenticated are revoked because this is user data.

CREATE TABLE IF NOT EXISTS public.telegram_users (
  telegram_user_id bigint PRIMARY KEY,
  chat_id bigint NOT NULL,
  username text,
  first_name text,
  last_name text,
  language_code text,
  is_bot boolean NOT NULL DEFAULT false,
  source text NOT NULL DEFAULT 'unknown',
  first_seen_at text NOT NULL,
  last_seen_at text NOT NULL,
  last_interaction_event text,
  can_broadcast boolean NOT NULL DEFAULT true,
  blocked_at text,
  last_broadcast_at text,
  broadcast_success_count integer NOT NULL DEFAULT 0,
  broadcast_failure_count integer NOT NULL DEFAULT 0,
  last_broadcast_error text
);

CREATE INDEX IF NOT EXISTS idx_telegram_users_last_seen
  ON public.telegram_users(last_seen_at);

CREATE INDEX IF NOT EXISTS idx_telegram_users_can_broadcast
  ON public.telegram_users(can_broadcast);

ALTER TABLE public.telegram_users ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.telegram_users FROM anon, authenticated;

DROP POLICY IF EXISTS telegram_users_backend_full_access ON public.telegram_users;
CREATE POLICY telegram_users_backend_full_access ON public.telegram_users
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (true)
  WITH CHECK (true);
