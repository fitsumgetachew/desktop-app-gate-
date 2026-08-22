"""Client-side acceptance run against the live portal (UAT).

Drives the desktop app's own service classes — the same ApiClient, AuthService
and repositories the app uses — so a PASS here means the shipping code works,
not that a test double does.

    .venv/bin/python scripts/acceptance_check.py

Steps needing the portal UI (revocation, de-provisioning) pause and prompt.
Nothing is written to the app's real database: a throwaway SQLite file is used,
so a bench run cannot clobber a gate's cached allowlist.

Never prints tokens or one-time codes.
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from smart_gate.repositories.allowlist_repo import AllowlistRepository  # noqa: E402
from smart_gate.repositories.db import init_db  # noqa: E402
from smart_gate.repositories.device_repo import DeviceRepository  # noqa: E402
from smart_gate.repositories.event_repo import EventRepository  # noqa: E402
from smart_gate.repositories.manual_reason_repo import ManualReasonRepository  # noqa: E402
from smart_gate.repositories.presence_repo import PresenceRepository  # noqa: E402
from smart_gate.services.api_client import ApiClient  # noqa: E402
from smart_gate.services.auth_service import AuthService  # noqa: E402
from smart_gate.services.decision_state import GateState, classify  # noqa: E402
from smart_gate.services.device_service import DeviceService  # noqa: E402
from smart_gate.services.sync_service import SyncWorker  # noqa: E402
from smart_gate.utils.config import load_config  # noqa: E402
from smart_gate.utils.plates import normalize_plate  # noqa: E402
from smart_gate.utils.time import now_ts  # noqa: E402

UAT_BASE = "https://sit-portal-e6750.web.app/api/gate"

results: list[tuple[str, str, str]] = []


def record(item: str, ok: bool | None, detail: str = "") -> None:
    mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    results.append((item, mark, detail))
    print(f"  [{mark}] {item}: {detail}")


def pause(prompt: str) -> None:
    print(f"\n>>> PORTAL ACTION NEEDED: {prompt}")
    input("    press Enter when done (or Ctrl-C to stop) ... ")


def build(base_url: str):
    db_path = Path(tempfile.mkdtemp()) / "acceptance.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    cfg = load_config()
    cfg.api_base_url = base_url
    device_repo = DeviceRepository(conn)
    api = ApiClient(cfg)
    auth = AuthService(api, device_repo)
    DeviceService(api, device_repo).ensure_device(cfg.gate_id, cfg.lane_id, "acceptance-run")
    return cfg, conn, db_path, api, auth, device_repo


def make_worker(cfg, conn, db_path, api, auth, device_repo) -> SyncWorker:
    w = SyncWorker(config=cfg, db_path=db_path, interval_seconds=10)
    w.device_repo = device_repo
    w.allow_repo = AllowlistRepository(conn)
    w.reason_repo = ManualReasonRepository(conn)
    w.event_repo = EventRepository(conn)
    w.presence_repo = PresenceRepository(conn)
    w.api = api
    w.auth = auth
    return w


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=UAT_BASE)
    parser.add_argument("--skip-portal-steps", action="store_true",
                        help="skip h/i, which need someone driving the portal UI")
    args = parser.parse_args()

    cfg, conn, db_path, api, auth, device_repo = build(args.base_url)
    device = device_repo.get_device()

    print(f"\nBase URL : {args.base_url}")
    print(f"Device ID: {device.device_id}")
    print("Provision this device in the portal, then open the SSO page for it.\n")

    # ── a. sign in + device check + full allowlist sync ──────────────
    print("a. Sign-in, device check, full allowlist sync")
    code = getpass.getpass("    paste the portal one-time code (hidden): ")
    try:
        session = auth.exchange_code(code)
        token = session["access_token"]
        role = session["user"].get("role")
        record("a1 exchange", True, f"signed in as role={role}")
    except Exception as exc:
        record("a1 exchange", False, f"{type(exc).__name__} — {exc}")
        return report()

    try:
        DeviceService(api, device_repo).register_device(token, device_repo.get_device())
        check = api.check_device(token, device.device_id)
        gate = (check.get("gate") or {}).get("id")
        lane = (check.get("lane") or {}).get("id")
        record("a2 device check", bool(check.get("registered")),
               f"registered={check.get('registered')} gate={gate} lane={lane}")
    except Exception as exc:
        record("a2 device check", False, f"{type(exc).__name__} — {exc}")

    worker = make_worker(cfg, conn, db_path, api, auth, device_repo)
    try:
        worker._sync_once()
        rows = conn.execute(
            "SELECT plate_number, status FROM cache_allowlist ORDER BY plate_number"
        ).fetchall()
        greens = [r["plate_number"] for r in rows if classify(
            r["plate_number"], worker.allow_repo.get_vehicle(r["plate_number"])
        ).state is GateState.GREEN]
        reds = [r["plate_number"] for r in rows if classify(
            r["plate_number"], worker.allow_repo.get_vehicle(r["plate_number"])
        ).state is GateState.RED]
        record("a3 allowlist sync", len(rows) > 0,
               f"{len(rows)} plates cached — GREEN={len(greens)} RED={len(reds)}")
        if rows:
            print(f"       sample: {[dict(r) for r in rows[:5]]}")
    except Exception as exc:
        record("a3 allowlist sync", False, f"{type(exc).__name__} — {exc}")
        rows = []

    known = rows[0]["plate_number"] if rows else None

    # ── b. lookup ────────────────────────────────────────────────────
    print("\nb. Lookup")
    if known:
        spaced = f"{known[:3]}-{known[3:]}" if len(known) > 3 else known
        try:
            found = api.lookup_vehicle(token, spaced)
            record("b1 separator-tolerant lookup", found.get("plate_number") == known,
                   f"'{spaced}' -> {found.get('plate_number')} ({found.get('status')})")
        except Exception as exc:
            record("b1 separator-tolerant lookup", False, f"{type(exc).__name__} — {exc}")
    else:
        record("b1 separator-tolerant lookup", None, "no cached plate to test with")

    unknown = f"ZZ{uuid.uuid4().hex[:6].upper()}"
    try:
        api.lookup_vehicle(token, unknown)
        record("b2 unknown plate -> 404/ORANGE", False, "server returned a record")
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        state = classify(unknown, None).state
        record("b2 unknown plate -> 404/ORANGE", status == 404 and state is GateState.ORANGE,
               f"HTTP {status}, client state {state.name}")

    # ── c. visitor registration ──────────────────────────────────────
    print("\nc. Visitor registration")
    visitor = f"VIS{uuid.uuid4().hex[:5].upper()}"
    try:
        resp = api.register_visitor(token, {
            "plate_number": visitor,
            "owner_first_name": "Acceptance",
            "owner_last_name": "Run",
            "valid_to": now_ts() + 3600,
        })
        vehicle = resp.get("vehicle") or {}
        worker.allow_repo.upsert_records([{**vehicle, "updated_at": now_ts()}])
        state = classify(visitor, worker.allow_repo.get_vehicle(visitor)).state
        record("c1 register visitor -> immediate GREEN", state is GateState.GREEN,
               f"{visitor} -> {state.name} without waiting for a sync")
    except Exception as exc:
        record("c1 register visitor -> immediate GREEN", False, f"{type(exc).__name__} — {exc}")

    blacklisted = next(
        (r["plate_number"] for r in rows if (r["status"] or "").upper() == "BLACKLISTED"), None
    )
    if blacklisted:
        before = worker.allow_repo.get_vehicle(blacklisted)
        try:
            api.register_visitor(token, {"plate_number": blacklisted})
            record("c2 blacklisted -> 409", False, "server accepted the registration")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            after = worker.allow_repo.get_vehicle(blacklisted)
            untouched = before and after and before.status == after.status
            record("c2 blacklisted -> 409 + record untouched",
                   status == 409 and bool(untouched),
                   f"HTTP {status}, cached status still {after.status if after else '?'}")
    else:
        record("c2 blacklisted -> 409", None, "no BLACKLISTED plate in the allowlist")

    # ── d. temporary permit ──────────────────────────────────────────
    print("\nd. Temporary permit")
    temp_plate = f"TMP{uuid.uuid4().hex[:5].upper()}"
    try:
        resp = api.create_temporary_permit(token, {
            "plate_number": temp_plate,
            "owner_name": "Acceptance Run",
            "expires_in_seconds": 3600,
        })
        vehicle = resp.get("vehicle") or {}
        worker.allow_repo.upsert_records([{**vehicle, "updated_at": now_ts()}])
        state = classify(temp_plate, worker.allow_repo.get_vehicle(temp_plate)).state
        record("d1 1-hour permit -> immediate GREEN", state is GateState.GREEN,
               f"{temp_plate} -> {state.name}, valid_to={vehicle.get('valid_to')}")
    except Exception as exc:
        record("d1 1-hour permit -> immediate GREEN", False, f"{type(exc).__name__} — {exc}")

    # ── e. events ────────────────────────────────────────────────────
    print("\ne. Events")
    reason_id = None
    try:
        reasons = api.get_manual_reasons(token).get("items", [])
        reason_id = reasons[0]["id"] if reasons else None
    except Exception:
        pass

    event_ids = []
    for i in range(3):
        eid = str(uuid.uuid4())
        event_ids.append(eid)
        conn.execute(
            "INSERT INTO event_queue (id, event_time, device_id, gate_id, lane_id, direction,"
            " plate_number_raw, plate_number_final, confidence, decision, decision_source,"
            " manual_by_user_id, manual_by_username, manual_reason_id, manual_reason,"
            " manual_note, is_offline_event, synced, sync_attempts, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)",
            (eid, now_ts(), device.device_id, cfg.gate_id, cfg.lane_id, "ENTRY",
             known or "TESTPLATE", normalize_plate(known or "TESTPLATE"), 0.93,
             "ALLOW" if i < 2 else "DENY", "AUTO" if i == 0 else "MANUAL",
             session["user"].get("uuid"), session["user"].get("email"),
             reason_id if i == 2 else None,
             "Manual override" if i == 2 else None,
             "acceptance run" if i == 2 else None, 0, now_ts()),
        )
    conn.commit()
    try:
        worker._sync_once()
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM event_queue WHERE synced = 0"
        ).fetchone()["c"]
        record("e1 events drain via /events/batch", remaining == 0,
               f"3 submitted, {remaining} still unsynced")
    except Exception as exc:
        record("e1 events drain via /events/batch", False, f"{type(exc).__name__} — {exc}")

    # ── f. evidence ──────────────────────────────────────────────────
    print("\nf. Evidence upload")
    try:
        jpeg = Path(tempfile.mkdtemp()) / "evidence.jpg"
        jpeg.write_bytes(bytes.fromhex("ffd8ffdb") + b"acceptance-run" + bytes.fromhex("ffd9"))
        info = api.get_evidence_upload_url(token, event_ids[0])
        method = info.get("upload_method", "multipart")
        api.upload_evidence(info["upload_url"], str(jpeg), method, token=token)
        record("f1 evidence upload", True, f"method={method}")
    except Exception as exc:
        record("f1 evidence upload", False, f"{type(exc).__name__} — {exc}")

    # ── g. offline drain, exactly once ───────────────────────────────
    print("\ng. Offline drain (10 events, no duplicates)")
    offline_ids = [str(uuid.uuid4()) for _ in range(10)]
    for eid in offline_ids:
        conn.execute(
            "INSERT INTO event_queue (id, event_time, device_id, gate_id, lane_id, direction,"
            " plate_number_raw, plate_number_final, confidence, decision, decision_source,"
            " is_offline_event, synced, sync_attempts, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,0,0,?)",
            (eid, now_ts(), device.device_id, cfg.gate_id, cfg.lane_id, "ENTRY",
             "OFFLINE1", "OFFLINE1", 0.8, "ALLOW", "MANUAL", now_ts()),
        )
    conn.commit()
    started = time.monotonic()
    try:
        worker._sync_once()
        elapsed = time.monotonic() - started
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM event_queue WHERE synced = 0"
        ).fetchone()["c"]
        # Re-submitting the same ids must dedupe rather than double-count.
        replay = api.post_events_batch(token, [{
            "id": offline_ids[0], "event_time": now_ts(), "device_id": device.device_id,
            "gate_id": cfg.gate_id, "lane_id": cfg.lane_id, "direction": "ENTRY",
            "plate_number_raw": "OFFLINE1", "plate_number_final": "OFFLINE1",
            "confidence": 0.8, "decision": "ALLOW", "decision_source": "MANUAL",
            "is_offline_event": True,
        }])
        deduped = bool(replay["results"][0].get("deduped"))
        record("g1 offline drain", remaining == 0, f"10 events in {elapsed:.1f}s, {remaining} left")
        record("g2 replay is deduped, not duplicated", deduped, f"deduped={deduped}")
    except Exception as exc:
        record("g1 offline drain", False, f"{type(exc).__name__} — {exc}")

    # ── h. revocation ────────────────────────────────────────────────
    print("\nh. Revocation (tombstone eviction)")
    if args.skip_portal_steps:
        record("h1 revocation", None, "skipped (--skip-portal-steps)")
    else:
        target = visitor
        pause(f"DELETE vehicle '{target}' in the portal (Vehicles → delete)")
        try:
            worker._sync_once()
            still = worker.allow_repo.get_vehicle(target)
            state = classify(target, still).state
            record("h1 deleted plate evicted from cache", still is None,
                   f"cache entry {'gone' if still is None else 'STILL PRESENT'}, state={state.name}")
        except Exception as exc:
            record("h1 deleted plate evicted from cache", False, f"{type(exc).__name__} — {exc}")

    # ── i. de-provisioning ───────────────────────────────────────────
    print("\ni. De-provisioning")
    if args.skip_portal_steps:
        record("i1 de-provisioning", None, "skipped (--skip-portal-steps)")
    else:
        pause(f"DE-PROVISION device '{device.device_id}' in the portal")
        seen: list[str] = []
        worker.device_deprovisioned.connect(seen.append)
        worker._config.auth_mode = "portal"
        try:
            worker._send_heartbeat(auth.tokens.get_token() or token, device.device_id)
            record("i1 heartbeat 404 -> de-provisioned signal", bool(seen),
                   seen[0] if seen else "no signal emitted")
        except Exception as exc:
            record("i1 heartbeat 404 -> de-provisioned signal", False, f"{type(exc).__name__} — {exc}")
        pause("RE-PROVISION the device so the next sign-in works")

    return report()


def report() -> int:
    print("\n" + "=" * 68)
    print("ACCEPTANCE SUMMARY")
    print("=" * 68)
    for item, mark, detail in results:
        print(f"  {mark:4}  {item:45} {detail}")
    failed = [r for r in results if r[1] == "FAIL"]
    skipped = [r for r in results if r[1] == "SKIP"]
    print(f"\n  {len(results) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
