# Staff roster + photos — desktop integration report

**For the SIT portal team.** Written from the Smart Gate desktop app, verified
live against `https://sit-portal-e6750.web.app/api/gate` on **2026-08-22 07:01**.

**Summary:** `/sync/staff-roster` is deployed, authenticates correctly, and
returns the right envelope — but every staff item comes back with
`"photos": []`. The desktop therefore downloads **0 images**, computes **0 face
embeddings**, and can never recognise anyone. Nothing else is blocking; this one
field is.

---

## 1. What we observed (live, unmodified)

```
GET /sync/staff-roster
Authorization: Bearer <access token>
→ HTTP 200, application/json
```

```json
{
  "version": "1787369497",
  "items": [
    {
      "staff_uid": "xBcEVZ9AKwhqrBaF3rFn5Hx7hTV2",
      "full_name": "Fitsum Tola Tola",
      "photos": [],
      "plates": [],
      "updated_at": 1787369497
    }
  ],
  "deleted": []
}
```

`?since_version=1` returns the same body, so the delta form is wired up too.

**Controls proving the problem is scoped to this one field:**

| Check | Result |
|---|---|
| `GET /sync/allowlist` (same token, same deployment) | 200, items with all 17 fields populated |
| `POST /attendance/batch` (empty batch probe) | 200 `{"ok":true,"received_at":…,"results":[]}` |
| Token refresh / device binding | works |
| Envelope keys `version` / `items` / `deleted` | correct |
| Item keys `staff_uid` / `full_name` / `updated_at` | correct |
| **`photos` array** | **always empty** |
| `plates` array | also empty (secondary — see §6) |

So: auth, routing, envelope and the delta protocol are all fine. Only the photo
payload is missing.

---

## 2. What the desktop needs instead

Each item must carry up to five enrolled photos:

```json
{ "version": "<int-as-string>",
  "items": [
    { "staff_uid": "xBcEVZ9AKwhqrBaF3rFn5Hx7hTV2",
      "full_name": "Fitsum Tola Tola",
      "photos": [
        { "position": 1, "hash": "<stable content hash>", "url": "<signed GET url>" },
        { "position": 2, "hash": "…", "url": "…" }
      ],
      "plates": ["ABC1234"],
      "updated_at": 1787369497 }
  ],
  "deleted": ["<staff_uid>"] }
```

### Field requirements

| Field | Type | Rule |
|---|---|---|
| `position` | int | 1–5, unique per staff member. It is half the storage key — see §4. |
| `hash` | string | **Content** hash (sha256 of the bytes is ideal). Must be identical for identical bytes and must change when the image changes. Any stable string works; we only compare for equality. |
| `url` | string | Absolute `https://` URL that returns the image bytes to an **unauthenticated** GET. Signed/expiring is expected and fine. |

Five is a **maximum, not a quota**. Two photos is fine; one is fine. One usable
photo is enough to be recognised.

---

## 3. How the desktop fetches (exact client behaviour)

Two calls, both already implemented and running:

**1. The roster** — `smart_gate/services/api_client.py :: get_staff_roster`

```python
GET {api_base_url}/sync/staff-roster
    ?since_version=<int>            # omitted entirely on a full sync
Authorization: Bearer <access_token>
timeout: 15s
```

**2. Each photo** — `smart_gate/services/api_client.py :: download_photo`

```python
GET <the url string, verbatim>
# NO Authorization header when the URL is absolute
timeout: 30s
→ raw image bytes
```

> ⚠️ **We deliberately send no bearer token to an absolute photo URL.** The
> signature in the query string is the credential. This mirrors how we already
> handle presigned evidence uploads, and an extra `Authorization` header is at
> best ignored and at worst rejected by S3/GCS/Firebase Storage. **So the URL
> must work on its own.** If you hand us a URL that requires our portal session,
> every download will 401 and no photo will ever embed.
>
> Quick check: `curl -sSI '<the url>'` from a clean shell, with no auth, must
> return `200`.

---

## 4. The caching rule — please read, it is the easiest thing to get wrong

The desktop syncs every ~6–10 seconds. It stores `(staff_uid, position) → hash`
and **downloads a photo only when the incoming `hash` differs from the stored
one.**

- **`hash` is the only cache key.** A changed URL with an unchanged hash causes
  **no** download. That is intended — signed URLs rotate constantly.
- **Never derive the hash from the URL, the timestamp, or a random value.** If
  the hash changes on every response, the gate re-downloads and re-embeds the
  entire roster every few seconds. Encoding costs ~350 ms per photo on the gate
  PC, so a 50-person roster would saturate the CPU permanently and starve the
  plate-recognition pipeline.
- Positions not present in the response are **deleted** locally. To remove one
  photo, omit that position; to remove a person, put their `staff_uid` in
  `deleted`.

## 5. What happens to a photo after download

We run `face_recognition` (dlib) and store a 128-d embedding. The image itself is
never sent anywhere — recognition is fully local and offline.

Practical requirements, measured on the real gate hardware:

- **Format:** JPEG or PNG bytes. Serve a sane `Content-Type`.
- **A detectable face must be present.** In the department's existing enrolment
  set, **4 of 5 photos encoded — the profile shot produced no face at all.** That
  is normal and we handle it, but it means near-frontal shots are worth
  prioritising.
- **Size:** the face itself should be **≥ 80 px** on its longest side. Anything
  around 300–800 px wide for the whole image is plenty; multi-megapixel originals
  just cost download time.
- One face per photo. If several are present we take the largest.

If a photo yields no face we keep the row with a null embedding, so it is **not**
re-downloaded every cycle — but that person may end up with zero usable photos,
which the desktop now surfaces as an operational error (§7).

---

## 6. Secondary: `plates` is also empty

`plates` should carry that staff member's vehicle plates:

```json
"plates": ["ABC1234", "XYZ9876"]
```

We canonicalise on arrival (uppercase, strip every non-alphanumeric — `"abc-123 4"`
→ `"ABC1234"`), so send them in whatever form you store. Send the **complete**
current list each time: it replaces the stored set, which is how a sold or
reassigned car stops resolving to the previous owner.

This drives the "staff drove in without recording attendance" voice notice. It is
independent of the photos — plates work even on a gate with no face camera — so
it can ship separately.

---

## 7. How to verify you have fixed it

**From your side:**

1. `GET /sync/staff-roster` returns at least one item with a non-empty `photos`.
2. Take a `url` from that response and `curl -sSI '<url>'` with **no auth
   headers** → `200` and an image content-type.
3. Call the endpoint twice without changing any photo. The `hash` for each
   position must be **byte-identical** across both responses.

**From the gate side** (we will confirm): the desktop's Staff Attendance panel
has an enrolment strip and a **Staff…** dialog. Today it reads:

> ⚠️ *1 staff synced, but the portal sent no photos — face recognition cannot
> work until photos are enrolled there*

| Staff | Photos | Embedded | Plates | Status |
|---|---|---|---|---|
| Fitsum Tola Tola | 0 | 0 | 0 | No photos from portal |

Once photos arrive it turns green and reads `N staff ready · M photos embedded`,
with the per-person breakdown showing how many of each person's photos actually
produced a usable face. That distinguishes three failures that otherwise look
identical from the guard booth:

- nothing synced yet,
- **staff synced but the portal sent no photos ← where we are now**,
- photos downloaded but none contained a readable face.

---

## 8. Not blocking, for completeness

`POST /attendance/batch` is deployed and already returns the correct envelope
(`{ok, received_at, results}`) — verified live. Punches are queued locally with a
client-generated uuid4 `id` as the idempotency key and drained in batches of ≤200.
Per-item results are read as `{ok, id, deduped, error}`; we accept `id`,
`punch_id` or `event_id` as the key. Nothing needed here right now.

---

## 9. The one-line ask

> `GET /sync/staff-roster` currently returns `"photos": []` for every staff
> member. Please populate it with `{position, hash, url}` per enrolled photo,
> where `hash` is a **stable content hash** (not URL- or time-derived) and `url`
> is fetchable **without an Authorization header**.

Either the staff records genuinely have no photos enrolled in the portal, or the
endpoint is not joining the photo records / not generating signed URLs — from the
gate we cannot tell which, but the fix is on the portal side either way.
