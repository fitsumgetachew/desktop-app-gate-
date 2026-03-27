from pathlib import Path

from smart_gate.models.domain import EventRecord
from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import init_db
from smart_gate.repositories.event_repo import EventRepository
from smart_gate.utils.time import now_ts


def _make_conn(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_allowlist_upsert(tmp_path: Path):
    conn = _make_conn(tmp_path)
    repo = AllowlistRepository(conn)

    repo.upsert_items([
        ("ABC-123", "ALLOWED", None, None, 1700000000, 1700000001),
        ("BLK-1", "BLACKLISTED", None, None, 1700000000, 1700000001),
    ])

    assert repo.get_plate_status("ABC-123") == "ALLOWED"
    assert repo.get_plate_status("BLK-1") == "BLACKLISTED"


def test_event_queue_add_and_sync(tmp_path: Path):
    conn = _make_conn(tmp_path)
    repo = EventRepository(conn)

    ts = now_ts()
    event = EventRecord(
        id="evt-1",
        event_time=ts,
        gate_id="G1",
        lane_id="L1",
        device_id="D1",
        direction="ENTRY",
        plate_number_raw="abc",
        plate_number_final="ABC",
        confidence=None,
        decision="ALLOW",
        decision_source="MANUAL",
        manual_by_user_id=None,
        manual_by_username="guard",
        manual_reason_id=None,
        manual_reason="Manual override",
        manual_note="",
        is_offline_event=True,
        evidence_path=None,
        synced=False,
        sync_attempts=0,
        last_sync_error=None,
        created_at=ts,
    )

    repo.add_event(event)
    unsynced = repo.list_unsynced()
    assert len(unsynced) == 1
    assert unsynced[0]["id"] == "evt-1"

    repo.mark_synced("evt-1")
    unsynced = repo.list_unsynced()
    assert len(unsynced) == 0
