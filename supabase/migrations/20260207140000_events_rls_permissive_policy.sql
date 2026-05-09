-- PostgreSQL 15+: enabling RLS without policies can deny reads/writes for roles that are not
-- superuser — including common Supabase pooler roles — so ``events`` inserts/selects fail silently
-- for the FastAPI worker while PostgREST anon/authenticated remain revoked.
--
-- Permissive policy for all roles that still have table GRANTs (backend DATABASE_URL).
DROP POLICY IF EXISTS events_backend_full_access ON public.events;
CREATE POLICY events_backend_full_access ON public.events
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (true)
  WITH CHECK (true);
