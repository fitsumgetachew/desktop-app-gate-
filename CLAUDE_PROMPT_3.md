# Task 3: Portal SSO login mode (authentication only)

## Phase 1 — Understand the current state first

Read before changing anything:

- `INTEGRATION_GUIDE.md` §2 (authentication), §3 (devices), §9 (errors) — the finalized contract.
- `smart_gate/services/auth_service.py` — `login()`, `refresh_access_token()`,
  `ensure_fresh_token()`, `call_authed()`, `logout()`.
- `smart_gate/services/token_store.py` — the process-wide in-memory access-token store.
- `smart_gate/services/worker_context.py` — **every worker thread builds its own
  `ApiClient` + `AuthService`**; this matters for §2.4 below.
- `smart_gate/main.py` — `LoginWorker` (incl. `_check_device`), `_handle_login`,
  `_on_login_success`, `_handle_logout`, `_handle_auth_required`.
- `smart_gate/ui/login_view.py`, `smart_gate/utils/config.py` (env pattern +
  `save_config`), `tests/` (119 tests passing — keep them green).

Context: the app today implements the mock flow — the sign-in screen collects
email + password and calls `POST /auth/desktop/start` itself, then
`/auth/desktop/exchange`. The production SIT portal is now live: **the operator
signs in on the portal web page in a browser, the portal mints the one-time
code**, and the desktop only performs step 2 (exchange) onward. In portal mode
the desktop must never send email/password.

Portal specifics (production extensions beyond the mock):
- `POST /auth/desktop/exchange` and `POST /auth/refresh` per contract §2.
- **`/auth/refresh` rotates: it returns a NEW `refresh_token` on every call**, and
  the old one dies immediately. Reuse of a dead refresh token can trip server-side
  theft detection and revoke the whole token family.
- **`POST /auth/logout` `{refresh_token}`** revokes the session server-side.

Scope: **authentication and the login UI only.** Do not touch ALPR, camera, sync,
event, or decision code.

## Phase 2 — Implement

### 1. `auth_mode` config setting
Add `AUTH_MODE` following the existing config/env pattern in `utils/config.py`:
`"mock"` (default — current behavior, completely unchanged) | `"portal"`.
Add `PORTAL_SSO_URL`, default `https://sit-portal-e6750.web.app/sso`.
**Both keys must be written by `save_config()`** (it has previously dropped keys it
didn't know about — verify by round-tripping a save in a test) and both must be
editable in the Settings view alongside the existing fields.

### 2. Portal-mode sign-in screen
When `auth_mode == "portal"`, `login_view` replaces email/password with:

- A **"Sign in via SIT Portal"** button that opens the system browser
  (`QtGui.QDesktopServices.openUrl`) at:
  `{PORTAL_SSO_URL}?client=smart-gate&device_id={device_id}`
  using the **same `device_id` the app already generates and persists** — the code
  is bound to it server-side and exchange fails on mismatch. URL-encode the params.
  Also render the full URL as selectable text ("can't open a browser? open this on
  your phone") so a kiosk without a default browser is not a dead end.
- A **code-entry field** + **Continue** button. The portal displays a base64url
  code in groups of 4; accept it with or without spaces — strip **all** whitespace
  (including pasted newlines) before sending. Continue calls
  `POST /auth/desktop/exchange {code, device_id}` on a worker thread (never block
  the UI thread), with the button disabled while in flight.
- Errors: **401 → "Invalid or expired code — generate a new one on the portal
  page"**, keep the field editable and let them retry. Codes expire in 120 s.
  Network error → a distinct "Cannot reach the portal" message.
- **Never log the code, tokens, or the full SSO URL with the code in it.**

On success, take **exactly the same post-login path as today**: persist tokens,
`POST /devices/register`, `POST /devices/check`, then `login_success`. Factor the
shared post-exchange logic so both modes run identical code — do not fork it.

### 3. Logout revokes server-side
In `AuthService.logout()`, when `auth_mode == "portal"`, call
`POST /auth/logout {refresh_token}` **before** the local cleanup (it needs the
token that `clear_session()` is about to delete). Best-effort: short timeout
(≤ 5 s), failures logged at warning and ignored — logout must never hang or fail
because the network is down. Add the endpoint to `DEFAULT_ENDPOINTS`
(`AUTH_LOGOUT` → `/auth/logout`) and to `api_client.py`.

### 4. Single-flight refresh (CRITICAL — this is a real bug under rotation)
`refresh_access_token()` already persists a rotated `refresh_token`
(`auth_service.py` — it compares against the stored one and updates). That part is
correct. **The gap is concurrency.**

`token_store` is a thread-safe singleton for the *access* token, but the refresh
call itself is not serialized, and every worker thread has its own `AuthService`
and its own SQLite connection reading `local_device_config.refresh_token`. So two
threads can read the *same* refresh token and both POST `/auth/refresh` — e.g. the
`SyncWorker`'s proactive `ensure_fresh_token(0.8)` firing in the same moment a
`LookupWorker`/`TempPermitWorker`/`RegisterVisitorWorker` hits a 401 and calls
`call_authed`. Against the mock (no rotation) this is harmless. **Against the
portal, the second request presents an already-consumed token → 401 →
`SessionExpiredError` → the kiosk is thrown back to the login screen, and
refresh-reuse detection may revoke the whole family, killing even the thread that
refreshed legitimately.** At a gate running 24/7 with a ~12-minute refresh cadence
this is a matter of days, and it will look like a random unexplained logout.

Implement standard single-flight refresh:
- A **process-wide** refresh lock (module-level, like `token_store`) — not a
  per-`AuthService` lock, since each thread has its own instance.
- Inside the lock, **re-check first**: if the in-memory access token was replaced
  while this thread waited (compare the token string, or use a short grace window /
  refresh counter), return that token instead of issuing a second refresh.
- Only one HTTP refresh may be in flight per process; the others reuse its result.
- Keep the persisted refresh token write inside the same critical section so the
  read-modify-write of `local_device_config.refresh_token` cannot interleave.

### 5. Offline branch on a missing `/devices/check`
The portal does not implement `/devices/check` yet, so it will 404.
`requests.HTTPError` subclasses `RequestException`, so `LoginWorker._check_device`
already catches it and returns `registered=None, offline=True` — login proceeds in
offline mode, which is the desired behavior. **Verify this with a test**, and
improve only the operator-facing wording: a 404/501 should read like "device check
not available — continuing in offline mode", not "Server unreachable
(HTTPError)". A `registered: false` body must still fail closed (block login).

## Phase 3 — Test

Unit tests (no network, no Qt event loop — mock the client):
- Config round-trip: `AUTH_MODE`/`PORTAL_SSO_URL` survive `save_config()`.
- Code normalization: `"abcd efgh\nijkl"` → `"abcdefghijkl"`.
- Rotation: a refresh response with a new `refresh_token` persists it; one without
  leaves the stored token untouched.
- **Single-flight: N threads calling `ensure_fresh_token`/`refresh_access_token`
  concurrently must produce exactly ONE `POST /auth/refresh`** (assert the mock
  client's call count == 1) and all threads must end up with the same access token.
- `_check_device`: 404 → offline branch; `registered: false` → login blocked;
  connection error → offline branch.
- Portal-mode logout posts `/auth/logout` before clearing, and a failing logout
  still clears local state.
- Mock mode is untouched: all 119 existing tests still pass.

Live UAT test (base URL `https://sit-portal-e6750.web.app/api/gate`; fallback
`https://us-central1-sit-portal-e6750.cloudfunctions.net/gateAuth`), with
`AUTH_MODE=portal`:
1. Browser sign-in on the portal → paste code → exchange 200 → app signed in →
   `/devices/check` 404s → **app enters offline mode and does not crash** (that
   branch behaving correctly is part of what we are testing).
2. Expired code (wait > 120 s) → clean 401 message, retry works.
3. Stay signed in 12+ minutes → app still signed in **and** the persisted
   `refresh_token` value has CHANGED (query the SQLite row before/after).
4. Logout, then manually replay `/auth/refresh` with the pre-logout refresh token
   → 401.

Finish by running `python -m pytest tests/` (all green) and launching
`python -m smart_gate` once in each mode to confirm both sign-in screens render.
