# Supabase Auth Hook Setup

The Favonius backend reads depot access and user role directly from JWT claims
(`user_metadata.depot_ids` and `user_metadata.favonius_role`). These claims must
be populated by a Supabase Auth Hook that fires whenever a user's token is issued
or refreshed.

---

## 1. Prerequisites

- A `user_depot_access` table in your Supabase PostgreSQL database:

```sql
CREATE TABLE IF NOT EXISTS user_depot_access (
    user_id  UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    depot_id UUID        NOT NULL REFERENCES depots(id)     ON DELETE CASCADE,
    role     TEXT        NOT NULL DEFAULT 'viewer'
                         CHECK (role IN ('admin', 'operator', 'viewer', 'auditor')),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, depot_id)
);
```

- Each user's platform-wide role stored in `auth.users.raw_user_meta_data` under
  the key `favonius_role` (set via `supabase.auth.admin.updateUserById()`).

---

## 2. Hook SQL function

Create this function in your Supabase SQL Editor:

```sql
CREATE OR REPLACE FUNCTION public.add_favonius_claims(event JSONB)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, auth
AS $$
DECLARE
    user_id   UUID;
    user_role TEXT;
    depot_ids UUID[];
BEGIN
    user_id := (event->>'user_id')::UUID;

    -- Read the platform role stored on the user record
    SELECT COALESCE(raw_user_meta_data->>'favonius_role', 'viewer')
    INTO   user_role
    FROM   auth.users
    WHERE  id = user_id;

    -- Collect depot IDs the user can access
    SELECT ARRAY_AGG(depot_id ORDER BY depot_id)
    INTO   depot_ids
    FROM   public.user_depot_access
    WHERE  user_id = user_id;

    -- Merge claims into user_metadata
    RETURN jsonb_set(
        jsonb_set(
            event,
            '{claims, user_metadata, favonius_role}',
            to_jsonb(user_role)
        ),
        '{claims, user_metadata, depot_ids}',
        COALESCE(to_jsonb(depot_ids), '[]'::jsonb)
    );
END;
$$;
```

---

## 3. Register the hook

In the Supabase Dashboard:

1. Navigate to **Authentication → Hooks**
2. Click **Add hook**
3. Choose **Customize access token (JWT) claims**
4. Set the function to `public.add_favonius_claims`
5. Save

The hook fires on every token mint and refresh — no backend code change is needed
when a user's depot access is modified, because the new list is embedded in the next
token the user receives.

---

## 4. How the backend reads these claims

`src/security/auth.py:verify_depot_access()` follows this fast path:

```python
metadata = user.get("user_metadata", {})
depot_ids = metadata.get("depot_ids")
if isinstance(depot_ids, list):
    if depot_id in depot_ids:
        return   # access granted
    raise HTTPException(403, ...)
```

If `depot_ids` is absent (token issued before the hook was configured), the backend
falls back to a direct DB query against `user_depot_access`. Once the hook is live,
all tokens will carry the claim and the DB fallback will not be exercised.

---

## 5. Granting access to a depot

```sql
-- Grant operator access to a depot
INSERT INTO public.user_depot_access (user_id, depot_id, role)
VALUES ('<user-uuid>', '<depot-uuid>', 'operator')
ON CONFLICT (user_id, depot_id) DO UPDATE SET role = EXCLUDED.role;
```

The change takes effect on the user's next token refresh (up to 1 hour delay for
access tokens; immediate if the user signs out and back in).

To set a user's platform role:

```javascript
// Supabase Admin SDK
await supabase.auth.admin.updateUserById(userId, {
  user_metadata: { favonius_role: 'operator' }
})
```
