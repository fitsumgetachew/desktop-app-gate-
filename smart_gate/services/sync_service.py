from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


import requests
from PySide6 import QtCore

from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import init_db
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.repositories.event_repo import EventRepository
from smart_gate.repositories.manual_reason_repo import ManualReasonRepository
from smart_gate.repositories.presence_repo import PresenceRepository
from smart_gate.repositories.punch_repo import PUNCH_BATCH_LIMIT, PunchRepository
from smart_gate.repositories.staff_repo import StaffRepository
from smart_gate.services.api_client import ApiClient
from smart_gate.services.auth_service import (
    AuthService,
    TransientAuthError,
    refresh_coordinator,
)
from smart_gate.services.face_recognition_service import (
    encode_photo,
    encode_to_blob,
    face_index,
)
from smart_gate.services.token_store import token_store
from smart_gate.services.vehicle_mapping import allowlist_item_to_record
from smart_gate.utils.config import AUTH_MODE_MOCK, AUTH_MODE_PORTAL, AppConfig
from smart_gate.utils.paths import get_staff_photo_path
from smart_gate.utils.plates import normalize_plate
from smart_gate.utils.time import now_ts

logger = logging.getLogger(__name__)

APP_VERSION = "2.0.0"

# Access tokens live 900 s; renew once 80% of that has burned down so the token
# is never actually allowed to expire mid-sync.
TOKEN_REFRESH_RATIO = 0.8

# Health reported to the portal in the heartbeat. The portal keeps this in its
# own `reported_status` field and shows it in the device health list, so it is
# the only channel through which a gate can say "I am alive but unhappy".
HEALTH_OK = "OK"
HEALTH_DEGRADED = "DEGRADED"

# `last_error` is operator-facing text on someone else's screen: keep it short
# and never let plate data, tokens or codes reach it.
MAX_LAST_ERROR_CHARS = 200

# Step names for the soft-fail attendance steps. These double as the prefix of
# the operator-facing `last_error` line, so they are worded as the step, not the
# endpoint.
STEP_STAFF_ROSTER = "staff roster sync"
STEP_ATTENDANCE = "attendance sync"

# Photos are fetched a few per cycle rather than all at once. A 200-staff
# enrolment is ~1000 photos, and the cost that matters is not the download but
# the ~350 ms embedding each one needs on the gate CPU — which competes directly
# with plate detection (5 fps) and face recognition (3 fps). Five per cycle is
# ~1.75 s of CPU per 10 s cycle, leaving the gate responsive while a full
# backfill still completes in well under an hour.
MAX_PHOTO_DOWNLOADS_PER_CYCLE = 5

# A slot that keeps failing must stop consuming the budget forever.
MAX_PHOTO_DOWNLOAD_ATTEMPTS = 5

DEPROVISIONED_MESSAGE = (
    "This device has been de-provisioned by an administrator. "
    "Contact IT to re-provision it."
)


class SyncWorker(QtCore.QThread):
    online_changed = QtCore.Signal(bool)
    sync_status = QtCore.Signal(str)
    last_sync_time = QtCore.Signal(int)
    next_sync_time = QtCore.Signal(int)
    sync_running = QtCore.Signal(bool)
    auth_required = QtCore.Signal()   # emitted when token refresh fails → re-login needed
    device_deprovisioned = QtCore.Signal(str)  # portal deleted this device's record

    def __init__(
        self,
        config: AppConfig,
        db_path: Path,
        interval_seconds: int,
    ) -> None:
        super().__init__()
        self._config = config
        self.db_path = db_path
        self._interval_seconds = max(interval_seconds, 5)
        self._stop_flag = False
        self._online = False
        self._backoff = 2
        self._next_wait_seconds = self._interval_seconds
        self._trigger_sync = True
        self._mutex = QtCore.QMutex()
        self._wait = QtCore.QWaitCondition()
        self._heartbeat_counter = 0   # send heartbeat every N sync cycles
        # Last health value the portal actually acknowledged. A change forces an
        # early heartbeat: a gate that just broke should not stay silent for the
        # rest of the 5-cycle window.
        self._last_reported_health: Optional[str] = None
        # Attendance steps whose endpoint has answered successfully at least
        # once this run. Until a step appears here a 404 means "not deployed
        # yet"; afterwards the same 404 is a real fault. In memory only — a
        # restart re-establishes it on the first good sync.
        self._proven_endpoints: set[str] = set()

        # Created inside run() so the requests.Session and the sqlite
        # connection both belong to this thread and nothing else.
        self.api: ApiClient | None = None
        self.auth: AuthService | None = None
        self._conn: sqlite3.Connection | None = None
        self.device_repo: DeviceRepository | None = None
        self.allow_repo: AllowlistRepository | None = None
        self.reason_repo: ManualReasonRepository | None = None
        self.event_repo: EventRepository | None = None
        self.presence_repo: PresenceRepository | None = None
        self.staff_repo: StaffRepository | None = None
        self.punch_repo: PunchRepository | None = None

    def stop(self) -> None:
        self._mutex.lock()
        self._stop_flag = True
        self._wait.wakeAll()
        self._mutex.unlock()

    def update_config(self, config: AppConfig) -> None:
        """Apply new settings without restarting the thread."""
        self._mutex.lock()
        self._config = config
        if self.api is not None:
            self.api.config = config
        self._mutex.unlock()

    def set_interval(self, interval_seconds: int) -> None:
        self._mutex.lock()
        self._interval_seconds = max(interval_seconds, 5)
        self._next_wait_seconds = self._interval_seconds
        next_time = now_ts() + self._next_wait_seconds
        self.next_sync_time.emit(next_time)
        self._wait.wakeAll()
        self._mutex.unlock()

    def trigger_sync(self) -> None:
        self._mutex.lock()
        self._trigger_sync = True
        self._wait.wakeAll()
        self._mutex.unlock()

    def run(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        init_db(self._conn)
        self.device_repo = DeviceRepository(self._conn)
        self.allow_repo = AllowlistRepository(self._conn)
        self.reason_repo = ManualReasonRepository(self._conn)
        self.event_repo = EventRepository(self._conn)
        self.presence_repo = PresenceRepository(self._conn)
        self.staff_repo = StaffRepository(self._conn)
        self.punch_repo = PunchRepository(self._conn)
        # Recognition reads an in-memory snapshot; prime it from whatever
        # the last session cached so a gate that starts offline still
        # recognises staff.
        face_index.load_from_repo(self.staff_repo)
        self.api = ApiClient(self._config)
        self.auth = AuthService(self.api, self.device_repo)

        while True:
            self._mutex.lock()
            if self._stop_flag:
                self._mutex.unlock()
                break
            if not self._trigger_sync:
                next_time = now_ts() + self._next_wait_seconds
                self.next_sync_time.emit(next_time)
                self._wait.wait(self._mutex, int(self._next_wait_seconds * 1000))
                if self._stop_flag:
                    self._mutex.unlock()
                    break
            self._trigger_sync = False
            self._mutex.unlock()

            self.sync_running.emit(True)
            self.sync_status.emit("Sync started...")
            attempted = False
            # Snapshot before the cycle: a 401 raised below may be stale news if
            # another thread refreshed mid-cycle — see auth_service's
            # _RefreshCoordinator. Refreshing again would replay a rotated token.
            refresh_marker = refresh_coordinator.marker()
            try:
                attempted = self._sync_once()
                if attempted:
                    self._set_online(True)
                    self._backoff = 2
                    self._next_wait_seconds = self._interval_seconds
                    self.sync_status.emit("Sync success")
                    self.last_sync_time.emit(now_ts())
                else:
                    self._set_online(False)
                    self.sync_status.emit("Not logged in")
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    logger.warning("Received 401 during sync, attempting token refresh")
                    try:
                        refreshed = self._try_refresh_token(refresh_marker)
                    except TransientAuthError as refresh_exc:
                        # Rate limited, 5xx or a dropped connection: the session
                        # is intact, so back off instead of signing the gate out.
                        logger.info("Refresh deferred (%s) — retrying", refresh_exc)
                        self._set_online(False)
                        self.sync_status.emit("Server busy — retrying")
                        self._backoff = min(self._backoff * 2, 60)
                        self._next_wait_seconds = self._backoff
                        continue
                    if refreshed:
                        # Token refreshed; next cycle will retry with the new token.
                        self._set_online(True)
                        self.sync_status.emit("Token refreshed — retrying next cycle")
                        self._next_wait_seconds = 2  # retry soon
                    else:
                        logger.warning("Token refresh failed — re-login required")
                        self._set_online(False)
                        self.sync_status.emit("Session expired")
                        self.auth_required.emit()
                        # Back off then stop protecting; let UI handle re-login
                        self._next_wait_seconds = self._interval_seconds
                elif exc.response is not None and exc.response.status_code == 403:
                    # The portal binds a session to the device_id it was issued
                    # for. Retrying cannot fix that — only signing in again from
                    # this machine can, so say so instead of backing off forever.
                    logger.warning("Received 403 during sync — device binding mismatch")
                    self._set_online(False)
                    self.sync_status.emit(
                        "Session belongs to a different device — sign in again"
                    )
                    self.auth_required.emit()
                    self._next_wait_seconds = self._interval_seconds
                else:
                    logger.warning("Sync HTTP error: %s", exc)
                    self._set_online(False)
                    status_code = exc.response.status_code if exc.response is not None else "?"
                    self.sync_status.emit(f"Sync failed (HTTP {status_code})")
                    self._backoff = min(self._backoff * 2, 60)
                    self._next_wait_seconds = self._backoff
                    if attempted:
                        self.last_sync_time.emit(now_ts())
            except requests.RequestException as exc:
                logger.warning("Sync request failed: %s", exc)
                self._set_online(False)
                self.sync_status.emit("Sync failed (offline)")
                self._backoff = min(self._backoff * 2, 60)
                self._next_wait_seconds = self._backoff
                if attempted:
                    self.last_sync_time.emit(now_ts())
            except Exception:
                logger.exception("Unexpected sync error")
                self._set_online(False)
                self.sync_status.emit("Sync failed")
                self._backoff = min(self._backoff * 2, 60)
                self._next_wait_seconds = self._backoff
                if attempted:
                    self.last_sync_time.emit(now_ts())
            finally:
                next_time = now_ts() + self._next_wait_seconds
                self.next_sync_time.emit(next_time)
                self.sync_running.emit(False)

        if self._conn:
            self._conn.close()

    def _set_online(self, online: bool) -> None:
        if self._online != online:
            self._online = online
            self.online_changed.emit(online)

    def _try_refresh_token(self, seen_marker: Optional[int] = None) -> bool:
        """Attempt to refresh the access token using the stored refresh token."""
        if not self.auth:
            return False
        return self.auth.refresh_access_token(seen_marker=seen_marker)

    def _get_token(self) -> Optional[str]:
        return token_store.get_token()

    def _sync_allowlist(self, token: str) -> None:
        """Pull the allowlist and apply upserts *and* revocations.

        With ``since_version`` the server sends a delta: ``items`` to upsert and
        ``deleted`` plates to drop.  Without it the response is the full set, so
        the local cache is replaced outright — merging would leave behind plates
        the server no longer knows about.
        """
        assert self.allow_repo is not None and self.api is not None
        since_version = self.allow_repo.get_last_version()
        allow_resp = self.api.get_allowlist(token, since_version)
        version = int(allow_resp.get("version", now_ts()))

        allow_items = [
            allowlist_item_to_record(item, version)
            for item in allow_resp.get("items", []) or []
        ]
        deleted = [
            normalize_plate(plate)
            for plate in allow_resp.get("deleted", []) or []
            if normalize_plate(plate)
        ]

        if since_version is None:
            # Full sync — the response is authoritative for the whole cache.
            self.allow_repo.replace_all(allow_items)
            logger.info("Allowlist full sync: %d plates cached", len(allow_items))
            return

        if allow_items:
            self.allow_repo.upsert_records(allow_items)
        if deleted:
            removed = self.allow_repo.delete_plates(deleted)
            logger.info("Allowlist delta: %d revoked plates removed", removed)

    # ------------------------------------------------------------------
    # Staff attendance roster
    # ------------------------------------------------------------------

    def _sync_staff_roster(self, token: str) -> None:
        """Pull the staff roster: names, plates and face embeddings.

        Same delta protocol as the allowlist — ``since_version`` absent means a
        full roster, and the stored ``version`` is the watermark for the next
        pull. What differs is the payload: each photo carries a content hash and
        a *freshly signed* URL, so the hash is the only thing that can tell us
        whether the bytes changed. Comparing URLs would re-download and re-embed
        the entire roster on every 10-second cycle.

        Embedding happens here, once, at sync time (~350 ms a photo). Recognition
        at the camera must never encode a roster photo.
        """
        assert self.staff_repo is not None and self.api is not None
        since_version = self.staff_repo.get_last_version()
        response = self.api.get_staff_roster(token, since_version)
        version = int(response.get("version", now_ts()))
        items = response.get("items") or []
        deleted = [str(uid) for uid in (response.get("deleted") or []) if uid]

        changed = False
        if since_version is None:
            # Full sync: the response is authoritative for the whole roster, so
            # anyone missing from it is de-rostered and must stop being
            # recognisable. Staff who *are* present still go through the
            # hash check below, so a full sync costs zero downloads when
            # nothing actually changed.
            incoming = {
                str(item.get("staff_uid"))
                for item in items
                if item.get("staff_uid")
            }
            stale = [
                uid for uid in self.staff_repo.list_staff_uids() if uid not in incoming
            ]
            if stale:
                self._evict_staff(stale)
                changed = True
                logger.info(
                    "Staff roster full sync: %d de-rostered staff removed", len(stale)
                )
        elif deleted:
            self._evict_staff(deleted)
            changed = True
            logger.info("Staff roster delta: %d staff evicted", len(deleted))

        for item in items:
            changed |= self._apply_staff_item(item, version, token)

        # Photos are fetched here, a few per cycle, after all metadata has
        # committed. Plates therefore work immediately — the
        # car-without-attendance notice needs no face at all — while faces
        # arrive progressively.
        changed |= self._drain_photo_queue(token)

        if items or deleted:
            logger.info(
                "Staff roster sync: %d updated, %d removed (version %s)",
                len(items),
                len(deleted),
                version,
            )
        if changed:
            # Recognition reads an immutable in-memory snapshot, so new staff
            # only become recognisable once it is rebuilt — do it here rather
            # than on restart.
            face_index.load_from_repo(self.staff_repo)

    def _apply_staff_item(self, item: Dict[str, Any], version: int, token: str) -> bool:
        """Upsert one roster entry. Returns True if the embedding set changed."""
        assert self.staff_repo is not None
        staff_uid = str(item.get("staff_uid") or "").strip()
        if not staff_uid:
            logger.warning("Staff roster item without a staff_uid — skipped")
            return False
        full_name = str(item.get("full_name") or "").strip() or staff_uid

        self.staff_repo.upsert_staff(
            staff_uid, full_name, item.get("updated_at"), version
        )
        # Replaced, not merged: a plate the portal dropped must stop being
        # attributed to this person.
        self.staff_repo.replace_plates(staff_uid, item.get("plates") or [])

        known_hashes = self.staff_repo.get_photo_hashes(staff_uid)
        positions: List[int] = []
        changed = False
        for photo in item.get("photos") or []:
            try:
                position = int(photo.get("position") or 0)
            except (TypeError, ValueError):
                position = 0
            photo_hash = str(photo.get("hash") or "").strip()
            url = str(photo.get("url") or "").strip()
            if position <= 0 or not photo_hash:
                logger.warning(
                    "Staff %s: photo slot with no position/hash — skipped", staff_uid
                )
                continue
            positions.append(position)
            if known_hashes.get(position) == photo_hash:
                continue  # cache hit: the signed URL rotated, the bytes did not
            if not url:
                logger.warning(
                    "Staff %s slot %d changed but carries no URL — skipped",
                    staff_uid,
                    position,
                )
                continue
            # Queued, not fetched: the roster metadata commits now and the
            # bytes follow at the pace the gate can afford.
            self.staff_repo.queue_photo(staff_uid, position, photo_hash, url)

        for position in self.staff_repo.delete_photos_except(staff_uid, positions):
            self._remove_photo_file(staff_uid, position)
            changed = True

        # Whether this person is recognisable cannot be judged here any more:
        # their photos are still queued at this point. _drain_photo_queue makes
        # that call once their slots actually settle.
        return changed

    def _drain_photo_queue(self, token: str) -> bool:
        """Fetch and embed at most a budget's worth of queued photos.

        Paced on purpose. Fetching a 1000-photo enrolment in one cycle would
        hold the sync thread for minutes and burn the CPU that plate detection
        and face recognition need — the gate would go unresponsive exactly while
        someone is standing at it.

        Every photo commits on its own (see ``upsert_photo``), so this is
        resumable for free: whatever did not commit is still pending next cycle,
        and nothing that landed is fetched twice. Killing the network mid-
        backfill costs only the photo in flight.

        Returns True if any embedding changed, so the caller can rebuild the
        recognition index.
        """
        assert self.staff_repo is not None
        pending = self.staff_repo.pending_photos(
            MAX_PHOTO_DOWNLOADS_PER_CYCLE, MAX_PHOTO_DOWNLOAD_ATTEMPTS
        )
        if not pending:
            return False

        changed = False
        for row in pending:
            staff_uid = row["staff_uid"]
            position = int(row["position"])
            url = row["source_url"] or ""
            if not url:
                self.staff_repo.mark_photo_failed(
                    staff_uid, position, "no source URL", MAX_PHOTO_DOWNLOAD_ATTEMPTS
                )
                continue
            # One bad photo must never take the rest of the queue — or the
            # cycle — down with it.
            try:
                changed |= self._fetch_and_embed(
                    staff_uid, position, row["photo_hash"], url, token
                )
            except Exception as exc:
                logger.warning(
                    "Staff %s slot %d: photo fetch failed (%s)",
                    staff_uid,
                    position,
                    type(exc).__name__,
                )
                self.staff_repo.mark_photo_failed(
                    staff_uid,
                    position,
                    type(exc).__name__,
                    MAX_PHOTO_DOWNLOAD_ATTEMPTS,
                )

        for staff_uid in {row["staff_uid"] for row in pending}:
            self._warn_if_unrecognisable(staff_uid)

        remaining, total = self.staff_repo.photo_queue_progress()
        if remaining:
            logger.info(
                "Staff photo enrolment: %d/%d embedded, %d queued",
                total - remaining,
                total,
                remaining,
            )
        return changed

    def _warn_if_unrecognisable(self, staff_uid: str) -> None:
        """Complain only about staff who genuinely cannot be recognised.

        Three situations look alike from the gate and must not be reported
        alike — only the last is anybody's fault:

        * **no photo slots at all** — a plates-only staff member. Perfectly
          legitimate: they still drive the car-without-attendance notice, and a
          gate with no face camera never needed their face.
        * **slots still queued** — mid-enrolment. Nothing is wrong yet.
        * **slots settled, none usable** — every photo they have failed to
          yield a face, so this gate can never recognise them. Someone has to
          re-enrol them in the portal.
        """
        assert self.staff_repo is not None
        if self.staff_repo.count_photos(staff_uid) == 0:
            return
        if self.staff_repo.has_pending_photos(staff_uid):
            return
        if self.staff_repo.count_encodings(staff_uid) > 0:
            return
        logger.error(
            "Staff %s (%s) has photos but none yielded a usable face — they "
            "cannot be recognised at this gate; re-enrol them in the portal",
            staff_uid,
            self.staff_repo.get_full_name(staff_uid) or staff_uid,
        )

    def _fetch_and_embed(
        self, staff_uid: str, position: int, photo_hash: str, url: str, token: str
    ) -> bool:
        """Download one photo, embed it, and store both. Returns True if stored.

        A download failure raises: the caller (`_drain_photo_queue`) owns the
        attempt accounting, and the slot stays pending so the next cycle
        retries it with a freshly issued URL.
        """
        assert self.staff_repo is not None and self.api is not None
        # The URL can be a signed capability for biometric data, so nothing
        # below ever logs it — only the slot it belongs to.
        data = self.api.download_photo(url, token=token)

        self._store_photo_file(staff_uid, position, data)
        encoding = encode_photo(data)
        if encoding is None:
            # Ordinary outcome: in the reference set the profile shot produced
            # no face. Record the hash anyway so these bytes are not fetched
            # again every cycle.
            logger.warning(
                "Staff %s slot %d yielded no face encoding — stored as unusable",
                staff_uid,
                position,
            )
        self.staff_repo.upsert_photo(
            staff_uid,
            position,
            photo_hash,
            encode_to_blob(encoding) if encoding is not None else None,
            now_ts(),
        )
        return True

    @staticmethod
    def _store_photo_file(staff_uid: str, position: int, data: bytes) -> None:
        """Keep the JPEG so a future re-embedding needs no network.

        Best-effort: the embedding is already stored, so a full or read-only
        disk must not fail the sync.
        """
        try:
            path = get_staff_photo_path(staff_uid, position)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            logger.warning(
                "Could not cache staff %s photo %d on disk: %s",
                staff_uid,
                position,
                exc.__class__.__name__,
            )

    @staticmethod
    def _remove_photo_file(staff_uid: str, position: int) -> None:
        try:
            get_staff_photo_path(staff_uid, position).unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove cached photo %s/%d", staff_uid, position)

    def _evict_staff(self, staff_uids: List[str]) -> None:
        """Forget de-rostered staff completely — rows, embeddings and JPEGs.

        Their queued punches stay: those are attendance already recorded and
        still owed to the portal.
        """
        assert self.staff_repo is not None
        self.staff_repo.delete_staff(staff_uids)
        for staff_uid in staff_uids:
            try:
                shutil.rmtree(get_staff_photo_path(staff_uid, 1).parent, ignore_errors=True)
            except OSError:
                logger.debug("Could not remove cached photos for %s", staff_uid)

    def _push_punches_batch(self, token: str, pending: List[sqlite3.Row]) -> None:
        """Drain the punch queue exactly as ``_push_events_batch`` drains events."""
        assert self.punch_repo is not None and self.api is not None
        items = [
            {
                "id": row["id"],                       # idempotency key
                "staff_uid": row["staff_uid"],
                "punch_time": row["punch_time"],
                "method": row["method"],
                "confidence": row["confidence"],
                "device_id": row["device_id"],
                "gate_id": row["gate_id"],
                "lane_id": row["lane_id"],
            }
            for row in pending
        ]
        response = self.api.post_attendance_batch(token, items)
        results = response.get("results", []) or []
        # The portal is still building this endpoint; accept whichever id key it
        # settles on rather than silently dropping every result.
        results_by_id = {
            str(result.get("id") or result.get("punch_id") or result.get("event_id")): result
            for result in results
        }

        for row in pending:
            result = results_by_id.get(row["id"])
            if result is None:
                continue  # absent from the response — stays queued for next cycle
            if result.get("ok"):
                # `deduped` means the portal already had this id: the punch
                # landed, so it is just as synced as a fresh one.
                self.punch_repo.mark_synced(row["id"])
            else:
                error = result.get("error") or "Server rejected punch"
                logger.warning("Punch %s rejected by server: %s", row["id"], error)
                self.punch_repo.increment_sync_attempt(row["id"], error)

    def _sync_once(self) -> bool:
        """Run one full sync cycle. Returns True if authenticated and attempted."""
        if not all([self.device_repo, self.allow_repo, self.reason_repo,
                    self.event_repo, self.presence_repo, self.api, self.auth]):
            return False
        device = self.device_repo.get_device()
        if not device:
            return False

        # Renew ahead of expiry rather than waiting for a 401 to bounce back.
        token = self.auth.ensure_fresh_token(TOKEN_REFRESH_RATIO)
        if not token:
            return False

        # ── Pull allowlist (delta sync + revocations) ────────────────
        # This step used to run bare, so any HTTP error unwound the whole cycle
        # and the heartbeat — the last step — never ran. The portal then could
        # not tell a gate that is failing to sync from one that is unplugged,
        # which is backwards: the failure the operator most needs to see
        # produced the least information. Record it, finish the cycle so the
        # heartbeat can carry the reason, and re-raise at the end so the local
        # offline banner and backoff behave exactly as they did before.
        deferred: Optional[BaseException] = None
        health_error: Optional[str] = None
        try:
            self._sync_allowlist(token)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (401, 403):
                raise  # credentials, not health — the run loop must handle these
            deferred = exc
            health_error = self._describe_failure("allowlist sync", exc)
            logger.warning("Allowlist sync failed (HTTP %s) — finishing cycle", status)
        except Exception as exc:
            deferred = exc
            health_error = self._describe_failure("allowlist sync", exc)
            logger.warning(
                "Allowlist sync failed (%s) — finishing cycle", type(exc).__name__
            )

        # ── Pull manual reasons (soft-fail) ──────────────────────────
        try:
            reason_resp = self.api.get_manual_reasons(token)
            reasons = reason_resp.get("items", [])
            reason_items = [
                (
                    int(r["id"]),
                    r["reason_text"],
                    1 if r.get("is_active", True) else 0,
                    now_ts(),
                )
                for r in reasons
            ]
            if reason_items:
                self.reason_repo.replace_all(reason_items)
        except requests.RequestException as exc:
            logger.info("Manual reasons sync failed; skipping")
            health_error = health_error or self._describe_failure("manual reasons sync", exc)

        # ── Pull staff roster (soft-fail) ────────────────────────────
        # Deliberately NOT the allowlist's defer-and-re-raise. Attendance is the
        # station's second job; the barrier is its first. A portal problem with
        # the staff roster must colour the heartbeat and nothing else — taking
        # the gate offline because a face photo would not download is exactly
        # backwards.
        if self.staff_repo is not None and getattr(
            self._config, "face_attendance_enabled", True
        ):
            try:
                self._sync_staff_roster(token)
                self._proven_endpoints.add(STEP_STAFF_ROSTER)
            except Exception as exc:
                if self._endpoint_not_deployed(STEP_STAFF_ROSTER, exc):
                    logger.info(
                        "Staff roster endpoint not available on this server — "
                        "attendance sync skipped"
                    )
                else:
                    logger.warning(
                        "Staff roster sync failed (%s) — continuing", type(exc).__name__
                    )
                    health_error = health_error or self._describe_failure(STEP_STAFF_ROSTER, exc)

        # ── Push unsynced events via batch ───────────────────────────
        unsynced = self.event_repo.list_unsynced()
        if unsynced:
            self._push_events_batch(token, unsynced)

        # ── Drain the punch queue (soft-fail, same reasoning) ────────
        if self.punch_repo is not None:
            try:
                pending_punches = self.punch_repo.list_unsynced(PUNCH_BATCH_LIMIT)
                if pending_punches:
                    self._push_punches_batch(token, pending_punches)
                    self._proven_endpoints.add(STEP_ATTENDANCE)
            except Exception as exc:
                if self._endpoint_not_deployed(STEP_ATTENDANCE, exc):
                    logger.info(
                        "Attendance endpoint not available on this server — "
                        "punches stay queued"
                    )
                else:
                    logger.warning(
                        "Attendance sync failed (%s) — continuing", type(exc).__name__
                    )
                    health_error = health_error or self._describe_failure(STEP_ATTENDANCE, exc)

        # ── Upload pending evidence files (soft-fail) ────────────────
        try:
            self._upload_pending_evidence(token)
        except Exception as exc:
            logger.exception("Evidence upload phase failed unexpectedly")
            health_error = health_error or self._describe_failure("evidence upload", exc)

        # ── Heartbeat (every 5 cycles, or at once when health changes) ──
        health = HEALTH_DEGRADED if health_error else HEALTH_OK
        self._heartbeat_counter += 1
        if self._heartbeat_counter >= 5 or health != self._last_reported_health:
            self._heartbeat_counter = 0
            self._send_heartbeat(token, device.device_id, health, health_error)

        if deferred is not None:
            # The portal has been told; now let the app react locally as before.
            raise deferred

        return True

    def _endpoint_not_deployed(self, step: str, exc: BaseException) -> bool:
        """True when this endpoint has never worked and is 404ing — not yet shipped.

        The attendance endpoints are being built in parallel with this app, and
        the reference server has neither of them. Treating that as a fault would
        paint every gate DEGRADED for the whole build window, and a health
        signal that is always red is one nobody reads.

        The exemption lasts only **until the endpoint proves it exists**. Once a
        step has answered successfully, ``_proven_endpoints`` remembers it and a
        later 404 — a path typo, a stale endpoint override, a server rewrite
        that regresses — degrades like any other status. Without that, the day
        after the portal ships, attendance could stop syncing silently and
        forever: a 404 deliberately does not count a sync attempt, so
        ``MAX_SYNC_ATTEMPTS`` would never trip and punches would pile up
        unbounded with nothing anywhere going red.

        A 404 before any success is genuinely ambiguous — not-yet-deployed and
        typo'd-from-the-start look identical from here — and is resolved in
        favour of staying quiet. Anything other than 404 is always a real
        failure. The allowlist is untouched by all of this: it is the gate's own
        data, and a 404 there still degrades *and* re-raises.

        Punches are never lost meanwhile; they stay queued until the endpoint
        appears.
        """
        if step in self._proven_endpoints:
            return False
        return (
            isinstance(exc, requests.HTTPError)
            and exc.response is not None
            and exc.response.status_code == 404
        )

    @staticmethod
    def _describe_failure(step: str, exc: BaseException) -> str:
        """One short, non-sensitive line for the portal's device health list.

        Only the step name and an HTTP status / exception class ever leave the
        gate: response bodies and request URLs can carry plate data or tokens.
        """
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            detail = f"HTTP {exc.response.status_code}"
        else:
            detail = type(exc).__name__
        return f"{step} failed: {detail}"[:MAX_LAST_ERROR_CHARS]

    def _build_event_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Build the API payload for a single event row."""
        return {
            "id": row["id"],                              # idempotency key
            "event_time": row["event_time"],
            "device_id": row["device_id"],
            "gate_id": row["gate_id"],
            "lane_id": row["lane_id"],
            "direction": row["direction"],
            "plate_number_raw": row["plate_number_raw"],
            # Canonical form — the server stores plates the same way.
            "plate_number_final": normalize_plate(row["plate_number_final"]),
            "confidence": row["confidence"],
            "decision": row["decision"],
            "decision_source": row["decision_source"],
            "manual_by_user_id": row["manual_by_user_id"] if "manual_by_user_id" in row.keys() else None,
            "manual_by_username": row["manual_by_username"],
            "manual_reason_id": row["manual_reason_id"] if "manual_reason_id" in row.keys() else None,
            "manual_reason_text": row["manual_reason"],
            "manual_note": row["manual_note"],
            "is_offline_event": bool(row["is_offline_event"]),
            "evidence_uploaded_url": None,  # local evidence path is never sent to server
        }

    def _push_events_batch(self, token: str, unsynced: List[sqlite3.Row]) -> None:
        """
        Submit the batch. Per-item failures do NOT abort the batch — each event is
        processed independently and marked accordingly.
        """
        items = [self._build_event_payload(row) for row in unsynced]
        batch_resp = self.api.post_events_batch(token, items)
        results: List[Dict[str, Any]] = batch_resp.get("results", [])

        # Index results by event_id for O(1) lookup
        results_by_id = {r["event_id"]: r for r in results}

        for row in unsynced:
            result = results_by_id.get(row["id"])
            if result is None:
                # Server didn't include this event in response — leave for next cycle
                continue

            if result.get("ok"):
                self.event_repo.mark_synced(row["id"])
                # Update local presence hint from server-authoritative state
                pu = result.get("presence_update")
                if pu and pu.get("plate_number"):
                    state = pu.get("inside_status", "UNKNOWN")
                    self.presence_repo.upsert_presence(
                        normalize_plate(pu["plate_number"]), state, now_ts()
                    )
            else:
                error_msg = result.get("error") or "Server rejected event"
                logger.warning("Event %s rejected by server: %s", row["id"], error_msg)
                self.event_repo.increment_sync_attempt(row["id"], error_msg)

    def _upload_pending_evidence(self, token: str) -> None:
        pending = self.event_repo.list_pending_evidence_upload()
        for row in pending:
            event_id = row["id"]
            evidence_path = row["evidence_path"]
            if not evidence_path or not Path(evidence_path).exists():
                self.event_repo.increment_evidence_upload_attempt(event_id, "File not found on disk")
                continue
            try:
                url_resp = self.api.get_evidence_upload_url(token, event_id)
                upload_url = url_resp["upload_url"]
                upload_method = url_resp.get("upload_method", "multipart")
                upload_resp = self.api.upload_evidence(upload_url, evidence_path, upload_method, token=token)
                file_url = upload_resp.get("file_url", "") if upload_resp else ""
                self.event_repo.mark_evidence_uploaded(event_id, file_url)
                logger.info("Evidence uploaded for event %s → %s", event_id, file_url)
            except Exception as exc:
                logger.warning("Evidence upload failed for event %s: %s", event_id, exc)
                self.event_repo.increment_evidence_upload_attempt(event_id, str(exc))

    def _send_heartbeat(
        self,
        token: str,
        device_id: str,
        health: str = HEALTH_OK,
        last_error: Optional[str] = None,
    ) -> None:
        try:
            payload = {
                "device_id": device_id,
                "app_version": APP_VERSION,
                "status": health,
                "last_error": last_error,
            }
            self.api.heartbeat(token, payload)
            # Only a heartbeat the server accepted counts as "the portal knows";
            # a failed one leaves the flag alone so the next cycle retries.
            self._last_reported_health = health
            logger.debug("Heartbeat sent (%s)", health)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 404 and self._is_portal_mode():
                # The portal deletes the device record and revokes its sessions
                # in one operation. 404 here is the earliest signal — waiting for
                # the refresh to 401 would leave a retired or stolen machine
                # opening the barrier off its cache for another ~15 minutes.
                logger.warning("Heartbeat 404 in portal mode — device de-provisioned")
                self.device_deprovisioned.emit(DEPROVISIONED_MESSAGE)
                return
            logger.debug("Heartbeat failed (non-critical): HTTP %s", status)
        except Exception as exc:
            logger.debug("Heartbeat failed (non-critical): %s", exc)

    def _is_portal_mode(self) -> bool:
        """Only the portal deletes device records. The reference server 404s
        heartbeats for any unregistered device during ordinary dev flows, so
        acting on it there would sign developers out constantly."""
        return getattr(self._config, "auth_mode", AUTH_MODE_MOCK) == AUTH_MODE_PORTAL
