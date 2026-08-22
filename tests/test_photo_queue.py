"""The paced, resumable photo queue.

A 200-staff enrolment is ~1000 photos at ~350 ms of CPU each. Fetching that in
one cycle would hold the sync thread for minutes and starve plate detection and
face recognition — the gate would freeze exactly while someone is standing at
it. So photos are queued during the roster walk and drained a few per cycle,
each committing on its own so a dropped network costs only the photo in flight.

Driven through the real ``SyncWorker`` and a real SQLite database, with a stub
API — the pacing lives in the interaction between them, so stubbing the repo
would test nothing.
"""

import sqlite3

import numpy as np
import pytest

from smart_gate.repositories.db import init_db
from smart_gate.repositories.staff_repo import StaffRepository
from smart_gate.services.sync_service import (
    MAX_PHOTO_DOWNLOAD_ATTEMPTS,
    MAX_PHOTO_DOWNLOADS_PER_CYCLE,
    SyncWorker,
)
from smart_gate.utils.config import load_config


def _roster(staff_count, photos_each, version="1000"):
    return {
        "version": version,
        "items": [
            {
                "staff_uid": f"stf-{n:04d}",
                "full_name": f"Staff {n}",
                "plates": [f"AA{n:05d}"],
                "photos": [
                    {
                        "position": p,
                        "hash": f"hash-{n}-{p}",
                        "url": f"/sync/staff-photo/stf-{n:04d}/{p}",
                    }
                    for p in range(1, photos_each + 1)
                ],
                "updated_at": 1766000000,
            }
            for n in range(1, staff_count + 1)
        ],
        "deleted": [],
    }


class StubApi:
    def __init__(self, response):
        self.response = response
        self.downloads = []
        self.fail_urls = set()

    def get_staff_roster(self, token, since_version):
        return self.response

    def download_photo(self, url, token=None):
        self.downloads.append(url)
        if url in self.fail_urls:
            raise OSError("connection reset")
        return f"jpeg:{url}".encode()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    monkeypatch.setattr(
        "smart_gate.services.sync_service.encode_photo",
        lambda source: np.random.default_rng(abs(hash(bytes(source))) % 2**32).random(128),
    )
    monkeypatch.setattr(
        "smart_gate.services.sync_service.get_staff_photo_path",
        lambda uid, position: tmp_path / "photos" / str(uid) / f"{position}.jpg",
    )
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    w = SyncWorker(config=load_config(), db_path=tmp_path / "t.db", interval_seconds=10)
    w.staff_repo = StaffRepository(conn)
    yield w
    conn.close()


# ── Pacing ────────────────────────────────────────────────────────────


def test_a_big_enrolment_is_not_fetched_in_one_go(worker):
    """20 staff x 5 photos = 100 photos. One cycle must take a bounded bite."""
    worker.api = StubApi(_roster(20, 5))

    worker._sync_staff_roster("tok")

    assert len(worker.api.downloads) == MAX_PHOTO_DOWNLOADS_PER_CYCLE
    pending, total = worker.staff_repo.photo_queue_progress()
    assert total == 100
    assert pending == 100 - MAX_PHOTO_DOWNLOADS_PER_CYCLE


def test_the_metadata_lands_immediately_even_though_photos_trail(worker):
    """Plates must not wait for faces: the car-without-attendance notice needs
    no photo at all, and a gate with no face camera needs none ever."""
    worker.api = StubApi(_roster(20, 5))

    worker._sync_staff_roster("tok")

    assert len(worker.staff_repo.list_staff_uids()) == 20
    assert worker.staff_repo.staff_for_plate("AA00020") == [("stf-0020", "Staff 20")]


def test_successive_cycles_drain_the_queue_to_completion(worker):
    worker.api = StubApi(_roster(4, 5))     # 20 photos

    worker._sync_staff_roster("tok")
    for _ in range(20):
        pending, _ = worker.staff_repo.photo_queue_progress()
        if not pending:
            break
        worker._drain_photo_queue("tok")

    assert worker.staff_repo.photo_queue_progress()[0] == 0
    assert len(worker.api.downloads) == 20
    assert len(set(worker.api.downloads)) == 20      # nothing fetched twice


# ── Resume ────────────────────────────────────────────────────────────


def test_an_interrupted_backfill_resumes_where_it_stopped(worker):
    """The whole point of committing per photo: whatever landed stays landed."""
    worker.api = StubApi(_roster(4, 5))
    worker._sync_staff_roster("tok")
    done_first = list(worker.api.downloads)

    # "Network death": every remaining fetch fails for a whole cycle.
    pending_rows = worker.staff_repo.pending_photos(100, MAX_PHOTO_DOWNLOAD_ATTEMPTS)
    worker.api.fail_urls = {row["source_url"] for row in pending_rows}
    worker._drain_photo_queue("tok")

    # Network back.
    worker.api.fail_urls = set()
    for _ in range(20):
        if not worker.staff_repo.photo_queue_progress()[0]:
            break
        worker._drain_photo_queue("tok")

    assert worker.staff_repo.photo_queue_progress()[0] == 0
    # The photos fetched before the outage were never fetched again.
    assert len(set(done_first)) == len(done_first)
    assert worker.api.downloads.count(done_first[0]) == 1


def test_one_bad_photo_does_not_block_the_others(worker):
    worker.api = StubApi(_roster(1, 5))
    worker.api.fail_urls = {"/sync/staff-photo/stf-0001/3"}

    worker._sync_staff_roster("tok")

    # Four of the five committed despite the failure in the middle.
    assert worker.staff_repo.count_encodings("stf-0001") == 4


# ── The attempt ceiling ───────────────────────────────────────────────


def test_a_permanently_broken_photo_is_retired(worker):
    """Otherwise it eats a slot of the budget every cycle, forever, starving
    photos that would have worked."""
    worker.api = StubApi(_roster(1, 1))
    worker.api.fail_urls = {"/sync/staff-photo/stf-0001/1"}

    worker._sync_staff_roster("tok")
    for _ in range(MAX_PHOTO_DOWNLOAD_ATTEMPTS + 3):
        worker._drain_photo_queue("tok")

    attempts = len(worker.api.downloads)
    assert attempts == MAX_PHOTO_DOWNLOAD_ATTEMPTS
    assert worker.staff_repo.pending_photos(10, MAX_PHOTO_DOWNLOAD_ATTEMPTS) == []


def test_a_new_hash_gives_a_retired_slot_a_fresh_start(worker):
    """A different photo is not the old photo's failures."""
    worker.api = StubApi(_roster(1, 1))
    worker.api.fail_urls = {"/sync/staff-photo/stf-0001/1"}
    worker._sync_staff_roster("tok")
    for _ in range(MAX_PHOTO_DOWNLOAD_ATTEMPTS + 1):
        worker._drain_photo_queue("tok")
    assert worker.staff_repo.pending_photos(10, MAX_PHOTO_DOWNLOAD_ATTEMPTS) == []

    replacement = _roster(1, 1, version="2000")
    replacement["items"][0]["photos"][0]["hash"] = "a-different-photo"
    worker.api = StubApi(replacement)
    worker._sync_staff_roster("tok")

    assert worker.staff_repo.count_encodings("stf-0001") == 1


# ── URL freshness ─────────────────────────────────────────────────────


def test_a_queued_slot_has_its_url_refreshed_by_the_next_roster_pull(worker):
    """A signed URL can expire while the slot waits its turn. Re-queuing is what
    replaces it — so a pending slot must not look like a cache hit."""
    worker.api = StubApi(_roster(3, 5))     # 15 photos, only 5 fetched
    worker._sync_staff_roster("tok")

    rotated = _roster(3, 5, version="1001")
    for item in rotated["items"]:
        for photo in item["photos"]:
            photo["url"] = photo["url"] + "?sig=fresh"
    worker.api = StubApi(rotated)
    worker._sync_staff_roster("tok")

    still_pending = worker.staff_repo.pending_photos(100, MAX_PHOTO_DOWNLOAD_ATTEMPTS)
    assert still_pending, "expected photos still waiting"
    assert all(row["source_url"].endswith("?sig=fresh") for row in still_pending)


def test_a_settled_slot_is_still_a_cache_hit(worker):
    """Refreshing pending URLs must not turn into re-downloading everything."""
    worker.api = StubApi(_roster(1, 3))
    worker._sync_staff_roster("tok")
    assert worker.staff_repo.photo_queue_progress()[0] == 0
    before = len(worker.api.downloads)

    rotated = _roster(1, 3, version="1001")
    for photo in rotated["items"][0]["photos"]:
        photo["url"] = photo["url"] + "?sig=rotated"
    worker.api = StubApi(rotated)
    worker._sync_staff_roster("tok")

    assert len(worker.api.downloads) == 0        # fresh stub: nothing re-fetched
    assert before == 3
