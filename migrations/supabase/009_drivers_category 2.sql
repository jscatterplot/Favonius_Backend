-- Add category column to drivers for delivery-vs-employee accounting splits.
-- Nullable on purpose: forces explicit categorization rather than silently
-- mis-attributing every existing driver as 'employee' via a default.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS so re-running this migration on a
-- partially-applied environment is safe.
ALTER TABLE public.drivers
    ADD COLUMN IF NOT EXISTS category TEXT NULL
    CHECK (category IS NULL OR category IN ('delivery', 'employee'));

COMMENT ON COLUMN public.drivers.category IS
    'Driver role for accounting splits. NULL = uncategorized; the agent '
    'excludes NULLs from category-filtered queries and surfaces the '
    'excluded count in the answer.';
