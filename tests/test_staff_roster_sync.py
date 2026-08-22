"""Staff roster sync: delta semantics, eviction, and the embedding cache.

``SyncWorker._sync_staff_roster`` is driven directly on an un-started worker —
no Qt event loop, no network, no dlib — with a stub ApiClient whose
``download_photo`` counts its calls. That count is the point of most of these
tests: signed photo URLs are re-issued on every sync, so a roster that has not
changed must still cost zero downloads.

The two recorded fixtures encode the contract the portal is building against:

* ``staff_roster_full.json``  — 3 staff with 5, 2 and 1 photos (five slots is a
  maximum, not a guarantee), one with two plates and one with none.
* ``staff_roster_delta.json`` — the same ``stf-0001`` with *every* URL rotated
  but only position 2's hash changed, its plate list cut from two to one, a new
  ``stf-0004``, and ``stf-0003`` deleted.
"""

import json
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from smart_gate.repositories.db import init_db
from smart_gate.repositories.staff_repo import StaffRepository
from smart_gate.services.face_recognition_service import decode_encoding
from smart_gate.services.sync_service import SyncWorker
from smart_gate.utils.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


FULL = _fixture("staff_roster_full")
DELTA = _fixture("staff_roster_delta")


def enrol_fully(worker, max_cycles=20):
    """One roster pull, then drain the photo queue to completion.

    Photos are fetched a few per cycle on purpose, so a single sync no longer
    finishes a backfill. Only the queue is re-driven here — re-calling the
    roster endpoint would consume another stub response and test something
    else entirely. Tests about the embedding *cache* want this settled state;
    the pacing itself is tested separately below.
    """
    worker._sync_staff_roster("tok")
    for _ in range(max_cycles):
        pending, _ = worker.staff_repo.photo_queue_progress()
        if not pending:
            return
        worker._drain_photo_queue("tok")
    raise AssertionError("photo queue never drained")

# Bytes the fake encoder refuses to embed — the profile-shot case.
UNENCODABLE = b"photo-with-no-face"


class StubApi:
    """Queued /sync/staff-roster responses; every download is recorded."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.roster_calls = []
        self.downloads = []
        self.unencodable_urls = set()

    def get_staff_roster(self, token, since_version):
        self.roster_calls.append(since_version)
        return self.responses.pop(0)

    def download_photo(self, url, token=None):
        self.downloads.append(url)
        if url in self.unencodable_urls:
            return UNENCODABLE
        # Deterministic bytes so the fake encoder can produce a stable vector.
        return f"jpeg:{url}".encode()

    @property
    def download_count(self):
        return len(self.downloads)


def _fake_encode(source):
    """Stand in for dlib: a deterministic 128-d vector, or None for a bad shot."""
    if source == UNENCODABLE:
        return None
    seed = abs(hash(bytes(source))) % (2**32)
    return np.random.default_rng(seed).random(128)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    # No dlib in the tests, and no writing biometric data into the real
    # app-data dir either.
    monkeypatch.setattr("smart_gate.services.sync_service.encode_photo", _fake_encode)
    monkeypatch.setattr(
        "smart_gate.services.sync_service.get_staff_photo_path",
        lambda uid, position: tmp_path / "photos" / str(uid) / f"{position}.jpg",
    )

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    w = SyncWorker(config=load_config(), db_path=tmp_path / "test.db", interval_seconds=10)
    w.staff_repo = StaffRepository(conn)
    w.photo_dir = tmp_path / "photos"
    yield w
    conn.close()


def _encodings(repo, staff_uid):
    return repo.count_encodings(staff_uid)


# ── Full sync ─────────────────────────────────────────────────────────


def test_full_sync_stores_every_staff_plate_and_photo(worker):
    worker.api = StubApi(FULL)

    enrol_fully(worker)

    repo = worker.staff_repo
    assert worker.api.roster_calls == [None]          # never synced → full
    assert sorted(repo.list_staff_uids()) == ["stf-0001", "stf-0002", "stf-0003"]
    assert repo.get_full_name("stf-0001") == "Abebe Bekele"
    assert repo.list_plates("stf-0001") == ["AA12345", "AA54321"]
    assert repo.list_plates("stf-0003") == []          # a staff member with no car
    # 5 + 2 + 1 photos: proof that five slots is a maximum, not a guarantee.
    assert worker.api.download_count == 8
    assert _encodings(repo, "stf-0001") == 5
    assert _encodings(repo, "stf-0002") == 2
    assert _encodings(repo, "stf-0003") == 1


def test_full_sync_records_the_version_so_the_next_pull_is_a_delta(worker):
    worker.api = StubApi(FULL, DELTA)

    worker._sync_staff_roster("tok")
    worker._sync_staff_roster("tok")

    assert worker.api.roster_calls == [None, 1024]


def test_full_sync_drops_staff_the_server_no_longer_lists(worker):
    """A full response is authoritative: anyone missing has been de-rostered and
    must stop being recognisable, embeddings and all."""
    worker.api = StubApi(FULL)
    enrol_fully(worker)

    trimmed = dict(FULL, version="2000", items=FULL["items"][:1])
    worker.api = StubApi(trimmed)
    worker.staff_repo.conn.execute("UPDATE staff_roster SET version=NULL")  # force full
    worker.staff_repo.conn.commit()

    worker._sync_staff_roster("tok")

    repo = worker.staff_repo
    assert repo.list_staff_uids() == ["stf-0001"]
    assert _encodings(repo, "stf-0002") == 0
    assert repo.list_plates("stf-0002") == []


# ── The embedding cache ───────────────────────────────────────────────


def test_an_unchanged_roster_downloads_nothing_the_second_time(worker):
    """Signed URLs rotate every sync. Only the hash may trigger a download —
    this is the whole reason the hash is stored."""
    rotated = json.loads(json.dumps(FULL))
    rotated["version"] = "1025"
    for item in rotated["items"]:
        for photo in item["photos"]:
            photo["url"] = photo["url"].replace("sig=", "sig=rotated-")
    worker.api = StubApi(FULL, rotated)

    enrol_fully(worker)
    assert worker.api.download_count == 8

    worker._sync_staff_roster("tok")

    assert worker.api.download_count == 8   # not one byte re-fetched


def test_only_the_photo_whose_hash_changed_is_re_downloaded(worker):
    """The delta fixture rotates all five of stf-0001's URLs and changes exactly
    one hash, so exactly one of its photos may be fetched."""
    worker.api = StubApi(FULL)
    enrol_fully(worker)
    assert worker.api.download_count == 8

    worker.api = StubApi(DELTA)
    enrol_fully(worker)

    # A fresh stub, so this counts only the delta's own downloads:
    # one for stf-0001 slot 2, one for the brand-new stf-0004.
    assert worker.api.download_count == 2
    assert any("stf-0001/2.jpg" in url for url in worker.api.downloads)
    assert any("stf-0004/1.jpg" in url for url in worker.api.downloads)
    assert not any("stf-0001/1.jpg" in url for url in worker.api.downloads)


def test_a_changed_hash_replaces_the_stored_encoding(worker):
    worker.api = StubApi(FULL)
    worker._sync_staff_roster("tok")
    before = worker.staff_repo.conn.execute(
        "SELECT photo_hash, encoding FROM staff_photos WHERE staff_uid='stf-0001' AND position=2"
    ).fetchone()

    worker.api = StubApi(DELTA)
    worker._sync_staff_roster("tok")
    after = worker.staff_repo.conn.execute(
        "SELECT photo_hash, encoding FROM staff_photos WHERE staff_uid='stf-0001' AND position=2"
    ).fetchone()

    assert after["photo_hash"] != before["photo_hash"]
    assert not np.array_equal(
        decode_encoding(after["encoding"]), decode_encoding(before["encoding"])
    )


# ── Delta semantics ───────────────────────────────────────────────────


def test_delta_upserts_new_staff_and_evicts_deleted_ones(worker):
    worker.api = StubApi(FULL, DELTA)
    worker._sync_staff_roster("tok")

    worker._sync_staff_roster("tok")

    repo = worker.staff_repo
    assert sorted(repo.list_staff_uids()) == ["stf-0001", "stf-0002", "stf-0004"]
    assert repo.get_full_name("stf-0004") == "Hanna Girma"
    assert repo.list_plates("stf-0004") == ["CC10101"]
    # stf-0002 was not in the delta at all and must be left untouched.
    assert _encodings(repo, "stf-0002") == 2


def test_eviction_removes_the_photos_encodings_and_plates_too(worker):
    """A de-rostered person must not stay recognisable on a gate PC."""
    worker.api = StubApi(FULL, DELTA)
    enrol_fully(worker)
    assert _encodings(worker.staff_repo, "stf-0003") == 1

    worker._sync_staff_roster("tok")

    conn = worker.staff_repo.conn
    assert conn.execute(
        "SELECT COUNT(*) FROM staff_photos WHERE staff_uid='stf-0003'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM staff_plates WHERE staff_uid='stf-0003'"
    ).fetchone()[0] == 0
    assert worker.staff_repo.get_full_name("stf-0003") is None


def test_a_dropped_plate_is_evicted(worker):
    """stf-0001 sells one of two cars: the plate must stop resolving to them."""
    worker.api = StubApi(FULL, DELTA)
    worker._sync_staff_roster("tok")
    assert worker.staff_repo.staff_for_plate("AA54321") == [("stf-0001", "Abebe Bekele")]

    worker._sync_staff_roster("tok")

    assert worker.staff_repo.list_plates("stf-0001") == ["AA12345"]
    assert worker.staff_repo.staff_for_plate("AA54321") == []


def test_plates_are_canonicalised_before_storage(worker):
    """The portal may send 'AA-123 45'; the gate only ever compares canonical
    forms, so the join in prompt 5 would silently miss otherwise."""
    payload = {
        "version": "5",
        "items": [
            {
                "staff_uid": "stf-9",
                "full_name": "Test",
                "photos": [],
                "plates": ["aa-123 45", "  bb99887  "],
                "updated_at": 1,
            }
        ],
        "deleted": [],
    }
    worker.api = StubApi(payload)

    worker._sync_staff_roster("tok")

    assert worker.staff_repo.list_plates("stf-9") == ["AA12345", "BB99887"]
    assert worker.staff_repo.staff_for_plate("AA-123-45") == [("stf-9", "Test")]


def test_a_slot_the_server_stops_listing_is_removed(worker):
    worker.api = StubApi(FULL)
    worker._sync_staff_roster("tok")

    shrunk = {
        "version": "1100",
        "items": [dict(FULL["items"][0], photos=FULL["items"][0]["photos"][:2])],
        "deleted": [],
    }
    worker.api = StubApi(shrunk)
    worker._sync_staff_roster("tok")

    assert sorted(worker.staff_repo.get_photo_hashes("stf-0001")) == [1, 2]
    assert _encodings(worker.staff_repo, "stf-0001") == 2


# ── Photos that yield no face ─────────────────────────────────────────


def test_a_photo_that_yields_no_encoding_is_skipped_without_failing_the_sync(worker):
    """In the reference set the profile shot produced no face. The sync must
    carry on and the other four photos must still be usable."""
    api = StubApi(FULL)
    api.unencodable_urls = {FULL["items"][0]["photos"][2]["url"]}
    worker.api = api

    worker._sync_staff_roster("tok")

    repo = worker.staff_repo
    assert _encodings(repo, "stf-0001") == 4          # 5 photos, 4 usable
    assert repo.get_photo_hashes("stf-0001")[3] is not None   # the row still exists


def test_an_unencodable_photo_is_not_re_downloaded_every_cycle(worker):
    """The row is stored with a NULL encoding precisely so its bytes are not
    fetched again ten seconds later."""
    api = StubApi(FULL, dict(FULL, version="1025"))
    api.unencodable_urls = {FULL["items"][0]["photos"][2]["url"]}
    worker.api = api

    worker._sync_staff_roster("tok")
    worker._sync_staff_roster("tok")

    assert api.download_count == 8


def test_a_staff_member_with_zero_usable_photos_is_logged_as_an_error(worker, caplog):
    """They cannot be recognised at all — an operational problem someone has to
    fix in the portal, so it must not pass quietly."""
    api = StubApi(FULL)
    api.unencodable_urls = {photo["url"] for photo in FULL["items"][2]["photos"]}
    worker.api = api

    with caplog.at_level(logging.ERROR):
        # The error is only correct once their slots have actually been tried —
        # mid-enrolment there is nothing to complain about yet.
        enrol_fully(worker)

    assert _encodings(worker.staff_repo, "stf-0003") == 0
    assert any(
        record.levelno >= logging.ERROR and "stf-0003" in record.getMessage()
        for record in caplog.records
    )


def test_a_failed_download_leaves_the_slot_for_the_next_cycle(worker):
    """A transient network error must not poison the cache with a hash whose
    bytes never arrived."""

    class FlakyApi(StubApi):
        def __init__(self, *responses):
            super().__init__(*responses)
            self.fail_next = True

        def download_photo(self, url, token=None):
            if self.fail_next and "stf-0002/1" in url:
                self.fail_next = False
                self.downloads.append(url)
                raise OSError("connection reset")
            return super().download_photo(url, token)

    worker.api = FlakyApi(FULL, dict(FULL, version="1025"))
    worker._sync_staff_roster("tok")     # cycle 1: the budget goes to stf-0001
    worker._drain_photo_queue("tok")     # cycle 2: stf-0002 slot 1 drops out

    assert _encodings(worker.staff_repo, "stf-0002") == 1   # slot 1 missing

    # The failed slot stayed pending, so the queue alone retries it on the next
    # cycle — no second roster pull, and nothing already stored is re-fetched.
    worker._drain_photo_queue("tok")

    assert _encodings(worker.staff_repo, "stf-0002") == 2   # retried and stored


# ── Photo files on disk ───────────────────────────────────────────────


def test_photos_are_cached_on_disk_for_future_re_embedding(worker):
    worker.api = StubApi(FULL)

    enrol_fully(worker)

    assert (worker.photo_dir / "stf-0001" / "1.jpg").exists()
    assert (worker.photo_dir / "stf-0003" / "1.jpg").exists()


def test_evicting_a_staff_member_deletes_their_cached_photos(worker):
    worker.api = StubApi(FULL, DELTA)
    enrol_fully(worker)
    assert (worker.photo_dir / "stf-0003").exists()

    worker._sync_staff_roster("tok")

    assert not (worker.photo_dir / "stf-0003").exists()


# ── Malformed input ───────────────────────────────────────────────────


def test_items_without_a_staff_uid_are_skipped_not_stored(worker):
    worker.api = StubApi(
        {"version": "3", "items": [{"full_name": "Nobody", "photos": []}], "deleted": []}
    )

    worker._sync_staff_roster("tok")

    assert worker.staff_repo.list_staff_uids() == []


def test_a_photo_slot_without_a_hash_is_skipped(worker):
    """The hash is the cache key; without one there is nothing to compare and
    the photo would be re-fetched forever."""
    worker.api = StubApi(
        {
            "version": "3",
            "items": [
                {
                    "staff_uid": "stf-9",
                    "full_name": "Test",
                    "photos": [{"position": 1, "url": "https://x/y.jpg"}],
                    "plates": [],
                    "updated_at": 1,
                }
            ],
            "deleted": [],
        }
    )

    worker._sync_staff_roster("tok")

    assert worker.api.download_count == 0
    assert worker.staff_repo.get_photo_hashes("stf-9") == {}
