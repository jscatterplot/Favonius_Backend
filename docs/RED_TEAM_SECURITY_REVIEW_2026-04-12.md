# Red-Team-Style Security Review (Static Code Assessment)

**Date:** 2026-04-12  
**Scope:** `src/api`, `src/websocket_handler`, authentication, token handling, and service-to-service trust boundaries.  
**Method:** Static code review + architecture consistency checks (no active exploitation performed).

---

## Executive summary

You are right to be worried: for critical infrastructure use, this codebase still has several **high-impact attack paths** that a motivated adversary would likely probe first.

Most urgent risks are:

1. **Authentication gaps in the aiohttp API server wiring** (routes registered without enforced auth wrappers).
2. **Bearer token leakage via URL query parameter** (`/realtime/subscribe` response constructs `?token=` URL).
3. **Key-separation violation** (Supabase service-role key reused as JWT signing key in websocket auth manager).
4. **Long-lived secret material stored/validated in plaintext-equivalent form** for station API keys/tokens in DB paths.

These are “zero-day style” in the sense that they are exploitable from code behavior, not from known CVE signatures.

---

## Findings

## F1 — Missing explicit auth enforcement in aiohttp route registration (**Critical**)

### Evidence
- The websocket handler API server registers sensitive routes directly (`/organizations`, `/vehicles`, `/stations`, `/sessions`, `/admin/*`) without wrapping handlers with `require_auth` / permission decorators during route setup.  
- Decorators exist in `auth_manager.py`, but they are not applied in `api_server.py` route binding.

### Why this matters
This creates a high-risk trust bug: either:
- endpoints become unintentionally public if handler signatures are later adjusted, or
- currently mis-bound handlers can fail unpredictably (500s), enabling denial-of-service and hiding access-control defects until production changes activate them.

In either case, the access-control boundary is fragile and easy to break under normal refactors.

### Patch direction
- Enforce auth at the **router/middleware layer**, not per-handler convention.
- Add a global middleware that verifies bearer tokens and attaches `request["user"]` for all protected prefixes.
- Keep explicit allowlist for public endpoints (`/health`, `/auth/login`, `/auth/refresh`).

### Why this approach
Middleware-level enforcement removes “forgot to add decorator” class bugs and gives one choke-point for logging, rate-limit identity, and incident response.

---

## F2 — Access token exposed in query string (**High**)

### Evidence
- `subscribe_realtime` returns:
  - `"/realtime/ws?token=<bearer token>"`

### Why this matters
Query-string tokens leak through:
- reverse proxy logs,
- browser history,
- referrer headers,
- APM tracing and analytics pipelines.

For critical infra operations, this is an unacceptable token exfiltration channel.

### Patch direction
- Never place bearer tokens in URLs.
- Return a neutral websocket path and require `Authorization: Bearer` during WebSocket handshake, or use a short-lived one-time signed ticket (≤60s TTL) bound to client + audience.

### Why this approach
Header-based auth or one-time tickets minimizes passive leakage and enables replay-resistant controls.

---

## F3 — JWT signing uses Supabase service-role secret (**High**)

### Evidence
- `AuthManager` sets `self.jwt_secret = config.service_key`.
- `SupabaseConfig.service_key` is documented as Supabase **service role key**.

### Why this matters
This violates key separation:
- one compromised key can both unlock Supabase privileged operations and mint/verify internal session tokens,
- rotation blast radius is large,
- auditing provenance of tokens becomes harder.

### Patch direction
- Introduce dedicated auth secrets:
  - `WS_AUTH_JWT_SIGNING_KEY_CURRENT`
  - `WS_AUTH_JWT_SIGNING_KEY_PREVIOUS`
- Keep service-role key only for Supabase admin operations.
- Add token `iss`, `aud`, `kid`, and enforce all on verification.

### Why this approach
Compartmentalized secrets drastically reduce lateral movement after credential compromise and support controlled rotation windows.

---

## F4 — API key/token persistence pattern suggests plaintext-equivalent secret exposure (**Medium–High**)

### Evidence
- API key validation compares DB `api_key` directly against provided key.
- Auth token storage writes token value directly to `auth_tokens.token`.

### Why this matters
If DB data is leaked (backup, replica, insider, SQLi in another service), attackers can directly reuse active credentials.

### Patch direction
- Store **hashed** key material (Argon2id or HMAC-SHA256 with server-side pepper).
- For lookup, hash presented key and compare hash.
- Show full key only at issuance time; never retrievable thereafter.
- Add key ID prefix (e.g., `fk_live_<id>_<secret>`) so lookup can use indexed ID + hash verify.

### Why this approach
This is standard secret-at-rest hardening and materially lowers impact of database compromise.

---

## 30/60/90-day patch plan

## First 30 days (containment)
1. Enforce router-level auth middleware for websocket handler API.
2. Remove query-token pattern from realtime subscribe output.
3. Add WAF/proxy rule to reject URLs containing `token=` on realtime paths.
4. Rotate potentially exposed tokens and service-role keys.

## Days 31–60 (hardening)
1. Split JWT signing keys from Supabase service-role key.
2. Add `iss`/`aud`/`kid` claim validation + key rotation support.
3. Migrate API key and auth token storage to hashed form.
4. Add SIEM alerts for auth failures, unusual token reuse, and cross-region anomalies.

## Days 61–90 (resilience)
1. Add adversarial integration tests (auth bypass, replay, privilege escalation cases).
2. Add threat-model regression checklist to CI gates.
3. Run external penetration test focused on websocket + API trust boundaries.
4. Build incident response runbook for credential compromise scenarios.

---

## Recommended architecture decisions (and rationale)

- **Central auth middleware over per-route decorators:** lowers human error rate and standardizes telemetry.
- **Short-lived, audience-bound tokens:** reduces replay and token forwarding risk.
- **Key separation by function (service-role vs auth-signing):** limits blast radius and simplifies rotation.
- **Hash-only secret storage:** prevents direct credential reuse after DB compromise.
- **Fail-closed defaults in production:** safer than permissive behavior when env vars are missing/misconfigured.

---

## Validation checklist after patches

- [ ] Protected routes return `401` without auth and `403` on insufficient role.
- [ ] No endpoint returns tokens in URL form.
- [ ] JWT verification rejects wrong `iss`, wrong `aud`, unknown `kid`, expired tokens.
- [ ] Database no longer stores plaintext API keys/tokens.
- [ ] Key rotation drill succeeds with no downtime.
- [ ] Security monitoring detects and alerts on replay/credential stuffing patterns.

