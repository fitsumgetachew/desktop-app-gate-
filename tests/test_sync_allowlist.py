"""Allowlist merge semantics: upsert, revocation, and full-sync replacement.

``SyncWorker._sync_allowlist`` is exercised directly on an un-started worker —
no Qt event loop, no network — with a stub ApiClient.
"""

import sqlite3
from pathlib import Path

import pytest

from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import init_db
from smart_gate.services.sync_service import SyncWorker
from smart_gate.utils.config import load_config


def _make_conn(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


class StubApi:
    """Returns queued /sync/allowlist responses and records the calls made."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get_allowlist(self, token, since_version):
        self.calls.append(since_version)
        return self.responses.pop(0)


def _item(plate, status="ALLOWED", valid_to=None, updated_at=1000, alert=None):
    item = {
        "plate_number": plate,
        "status": status,
        "valid_to": valid_to,
        "owner_name": "Owner",
        "updated_at": updated_at,
    }
    if alert is not None:
        item["alert"] = alert
    return item


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    conn = _make_conn(tmp_path)
    w = SyncWorker(config=load_config(), db_path=tmp_path / "test.db", interval_seconds=10)
    w.allow_repo = AllowlistRepository(conn)
    yield w
    conn.close()


# ── Full sync ─────────────────────────────────────────────────────────


def _seed_unversioned(repo, plate):
    """Insert a row with no version, as a pre-versioning build would leave it.

    get_last_version() then returns None, which is what makes the next pull a
    full sync.
    """
    repo.conn.execute(
        "INSERT INTO cache_allowlist (plate_number, status, updated_at, version)"
        " VALUES (?, 'ALLOWED', 900, NULL)",
        (plate,),
    )
    repo.conn.commit()


def test_full_sync_replaces_the_whole_cache(worker):
    """No since_version → the response is authoritative; stale rows must go."""
    _seed_unversioned(worker.allow_repo, "STALE1")
    worker.api = StubApi({"version": "1000", "items": [_item("ABC-1234")]})

    worker._sync_allowlist("token")

    assert worker.api.calls == [None]  # full sync
    assert worker.allow_repo.list_plates() == ["ABC1234"]
    assert worker.allow_repo.get_plate_status("STALE1") is None


def test_full_sync_with_empty_server_clears_the_cache(worker):
    _seed_unversioned(worker.allow_repo, "ABC1234")
    worker.api = StubApi({"version": "1000", "items": []})

    worker._sync_allowlist("token")

    assert worker.allow_repo.list_plates() == []


def test_replace_all_is_atomic_on_failure(worker):
    """A bad row must not leave the cache half-wiped."""
    worker.allow_repo.upsert_items([("ABC1234", "ALLOWED", None, None, 900, 900)])
    with pytest.raises(Exception):
        worker.allow_repo.replace_all([("DUP1", "ALLOWED", None, None, 1, 1),
                                       ("DUP1", "ALLOWED", None, None, 1, 1)])
    assert worker.allow_repo.get_plate_status("ABC1234") == "ALLOWED"


def test_full_sync_normalizes_plates(worker):
    worker.api = StubApi({"version": "1000", "items": [_item("abc-1234")]})
    worker._sync_allowlist("token")
    assert worker.allow_repo.get_plate_status("ABC1234") == "ALLOWED"


# ── Delta sync ────────────────────────────────────────────────────────


def test_delta_sync_upserts_without_touching_untouched_plates(worker):
    worker.allow_repo.upsert_items([
        ("ABC1234", "ALLOWED", None, None, 900, 900),
        ("KEEPME", "ALLOWED", None, None, 900, 900),
    ])
    worker.api = StubApi({"version": "1100", "items": [_item("ABC1234", "DENIED")]})

    worker._sync_allowlist("token")

    assert worker.api.calls == [900]  # sent as a delta
    assert worker.allow_repo.get_plate_status("ABC1234") == "DENIED"
    assert worker.allow_repo.get_plate_status("KEEPME") == "ALLOWED"


def test_delta_sync_removes_revoked_plates(worker):
    """The bug: delta sync only upserted, so a revoked plate stayed ALLOWED."""
    worker.allow_repo.upsert_items([
        ("ABC1234", "ALLOWED", None, None, 900, 900),
        ("REVOKED1", "ALLOWED", None, None, 900, 900),
    ])
    worker.api = StubApi({"version": "1100", "items": [], "deleted": ["REVOKED1"]})

    worker._sync_allowlist("token")

    assert worker.allow_repo.get_plate_status("REVOKED1") is None
    assert worker.allow_repo.get_plate_status("ABC1234") == "ALLOWED"


def test_deleted_plates_are_normalized_before_removal(worker):
    worker.allow_repo.upsert_items([("ABC1234", "ALLOWED", None, None, 900, 900)])
    worker.api = StubApi({"version": "1100", "items": [], "deleted": ["abc-1234"]})

    worker._sync_allowlist("token")

    assert worker.allow_repo.get_plate_status("ABC1234") is None


def test_delta_sync_applies_upserts_and_deletions_together(worker):
    worker.allow_repo.upsert_items([
        ("OLD1", "ALLOWED", None, None, 900, 900),
        ("GONE1", "ALLOWED", None, None, 900, 900),
    ])
    worker.api = StubApi({
        "version": "1100",
        "items": [_item("NEW-1", "ALLOWED", updated_at=1100)],
        "deleted": ["GONE1"],
    })

    worker._sync_allowlist("token")

    assert sorted(worker.allow_repo.list_plates()) == ["NEW1", "OLD1"]


def test_missing_deleted_key_is_tolerated(worker):
    """Older servers do not send `deleted` — that must not break the sync."""
    worker.allow_repo.upsert_items([("ABC1234", "ALLOWED", None, None, 900, 900)])
    worker.api = StubApi({"version": "1100", "items": []})

    worker._sync_allowlist("token")

    assert worker.allow_repo.get_plate_status("ABC1234") == "ALLOWED"


def test_alert_flag_is_cached(worker):
    worker.api = StubApi({
        "version": "1000",
        "items": [_item("BLK-6666", "BLACKLISTED", alert=True)],
    })

    worker._sync_allowlist("token")

    status, valid_to, alert = worker.allow_repo.get_plate_record("BLK6666")
    assert status == "BLACKLISTED"
    assert alert is True


def test_revocation_then_resync_is_a_full_sync(worker):
    """Emptying the cache resets the version, so the next pull is a full sync."""
    worker.allow_repo.upsert_items([("ONLY1", "ALLOWED", None, None, 900, 900)])
    worker.api = StubApi(
        {"version": "1100", "items": [], "deleted": ["ONLY1"]},
        {"version": "1200", "items": [_item("ABC1234", updated_at=1200)]},
    )

    worker._sync_allowlist("token")
    worker._sync_allowlist("token")

    assert worker.api.calls == [900, None]
    assert worker.allow_repo.list_plates() == ["ABC1234"]
