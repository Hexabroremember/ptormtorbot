-- Fix Supabase linter: RLS disabled on public tables exposed to PostgREST.
-- These tables are used only by the FastAPI app via DATABASE_URL (postgres role),
-- which bypasses RLS. Enabling RLS blocks anon/authenticated from reading via the
-- Supabase Data API. Safe to re-run.
--
-- Run in Supabase SQL Editor or: supabase db push (if CLI linked).

DO $$
DECLARE
  t text;
  tables text[] := ARRAY[
    'events',
    'rate_counters',
    'rate_limit_overrides',
    'pdf_download_tokens',
    'crypto_orders',
    'user_saved_forms',
    'payment_codes',
    'app_kv'
  ];
BEGIN
  FOREACH t IN ARRAY tables
  LOOP
    IF EXISTS (
      SELECT 1
      FROM information_schema.tables
      WHERE table_schema = 'public'
        AND table_name = t
    ) THEN
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format(
        'REVOKE ALL ON TABLE public.%I FROM anon, authenticated',
        t
      );
    END IF;
  END LOOP;
END $$;
