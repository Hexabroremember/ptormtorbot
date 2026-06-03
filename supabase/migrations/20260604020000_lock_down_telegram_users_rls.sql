-- Remove the broad public RLS policy from existing deployments.
-- Backend writes use DATABASE_URL; PostgREST access remains revoked for anon/authenticated.

ALTER TABLE public.telegram_users ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.telegram_users FROM anon, authenticated;
GRANT ALL ON TABLE public.telegram_users TO service_role;

DROP POLICY IF EXISTS telegram_users_backend_full_access ON public.telegram_users;
