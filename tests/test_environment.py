"""Local data is partitioned per server environment.

The bug this pins: one gate.db for every server meant a station moved from UAT
to production sent production its UAT watermark, got back "nothing newer", and
showed UAT's roster forever — and would have drained UAT's queued punches into
production's permanent records. Isolation is structural (one database per
environment), not row-by-row, so a queue physically cannot reach the wrong
server.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from smart_gate.models.domain import PunchRecord
from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import Database, init_db
from smart_gate.repositories.punch_repo import PunchRepository
from smart_gate.repositories.staff_repo import StaffRepository
from smart_gate.services.sync_service import SyncWorker
from smart_gate.utils import paths
from smart_gate.utils.config import load_config
from smart_gate.utils.environment import (
    environment_key,
    environment_label,
    normalize_base_url,
)

UAT = "https://sit-portal-e6750.web.app/api/gate"
PROD = "https://portal.sitedu.info/api/gate"
MOCK = "http://localhost:8000"


@pytest.fixture
def app_data(tmp_path, monkeypatch):
    """Point every path helper at a throwaway app-data dir."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("APPDATA", raising=False)
    paths.set_active_environment(None)
    yield tmp_path / "xdg" / paths.APP_NAME
    paths.set_active_environment(None)


def _config_for(monkeypatch, tmp_path, base_url):
    env = tmp_path / f"{environment_key(base_url)}.env"
    env.write_text(f"API_BASE_URL={base_url}\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env))
    return load_config()


# ── Key derivation ────────────────────────────────────────────────────


def test_same_url_gives_the_same_key_across_restarts():
    assert environment_key(PROD) == environment_key(PROD)


def test_different_servers_give_different_keys():
    assert environment_key(UAT) != environment_key(PROD)
    assert environment_key(MOCK) != environment_key(PROD)


@pytest.mark.parametrize(
    "variant",
    [
        "https://portal.sitedu.info/api/gate/",     # trailing slash
        "https://PORTAL.SITEDU.INFO/api/gate",      # host case
        "http://portal.sitedu.info/api/gate",       # scheme
        "  https://portal.sitedu.info/api/gate  ",  # whitespace
    ],
)
def test_cosmetic_url_differences_do_not_fork_the_data(variant):
    assert environment_key(variant) == environment_key(PROD)


def test_a_different_path_is_a_different_server():
    assert environment_key(PROD) != environment_key("https://portal.sitedu.info/api/gate-v2")


def test_normalisation_and_label():
    assert normalize_base_url("https://Portal.Example/api/gate/") == "portal.example/api/gate"
    assert environment_label(PROD) == "portal.sitedu.info"
    assert environment_label(MOCK) == "localhost:8000"
    assert environment_label("") == "not configured"


# ── Paths ─────────────────────────────────────────────────────────────


def test_db_path_is_per_environment(app_data, monkeypatch, tmp_path):
    _config_for(monkeypatch, tmp_path, UAT)
    uat_db = paths.get_default_db_path()
    _config_for(monkeypatch, tmp_path, PROD)
    prod_db = paths.get_default_db_path()

    assert uat_db != prod_db
    assert f"env-{environment_key(UAT)}" in str(uat_db)
    assert f"env-{environment_key(PROD)}" in str(prod_db)
    assert uat_db.name == prod_db.name == "gate.db"


def test_evidence_and_staff_photos_follow_the_environment(app_data, monkeypatch, tmp_path):
    cfg = _config_for(monkeypatch, tmp_path, PROD)
    key = environment_key(PROD)
    assert f"env-{key}" in cfg.evidence_dir
    assert f"env-{key}" in str(paths.get_staff_photo_path("stf-1", 1))


def test_an_explicit_evidence_dir_is_honoured_as_is(app_data, monkeypatch, tmp_path):
    env = tmp_path / "x.env"
    env.write_text(f"API_BASE_URL={PROD}\nEVIDENCE_DIR={tmp_path / 'my-evidence'}\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env))
    cfg = load_config()
    assert cfg.evidence_dir == str(tmp_path / "my-evidence")


# ── Switching environments ────────────────────────────────────────────


class _RosterApi:
    """Records the since_version each pull asks for."""

    def __init__(self):
        self.since = []

    def get_staff_roster(self, token, since_version):
        self.since.append(since_version)
        return {"version": "5000", "items": [], "deleted": []}

    def get_allowlist(self, token, since_version):
        self.since.append(since_version)
        return {"version": "5000", "items": [], "deleted": []}


def _open(db_path: Path) -> sqlite3.Connection:
    conn = Database(db_path).connect()
    init_db(conn)
    return conn


def test_switching_servers_starts_clean_and_returns_lossless(app_data, monkeypatch, tmp_path):
    # Environment A learns a watermark.
    _config_for(monkeypatch, tmp_path, UAT)
    a_path = paths.get_default_db_path()
    a = _open(a_path)
    AllowlistRepository(a).upsert_records([
        {"plate_number": "AA12345", "status": "ALLOWED", "updated_at": 4000, "version": 4000}
    ])
    assert AllowlistRepository(a).get_last_version() == 4000
    a.close()

    # Switch to B: no rows, and the FIRST pull must be a full sync.
    _config_for(monkeypatch, tmp_path, PROD)
    b_path = paths.get_default_db_path()
    assert b_path != a_path
    b = _open(b_path)
    assert AllowlistRepository(b).get_last_version() is None      # the bug
    assert b.execute("SELECT COUNT(*) FROM cache_allowlist").fetchone()[0] == 0

    worker = SyncWorker(config=load_config(), db_path=b_path, interval_seconds=10)
    worker.allow_repo = AllowlistRepository(b)
    worker.api = _RosterApi()
    worker._sync_allowlist("tok")
    assert worker.api.since == [None]      # absent → full sync, not UAT's watermark
    b.close()

    # Back to A: everything is exactly as it was left.
    _config_for(monkeypatch, tmp_path, UAT)
    assert paths.get_default_db_path() == a_path
    a = _open(a_path)
    assert AllowlistRepository(a).get_last_version() == 4000
    assert AllowlistRepository(a).get_vehicle("AA12345") is not None
    a.close()


def test_a_punch_queued_in_one_environment_cannot_drain_to_another(app_data, monkeypatch, tmp_path):
    _config_for(monkeypatch, tmp_path, UAT)
    a = _open(paths.get_default_db_path())
    PunchRepository(a).add_punch(PunchRecord(
        id="p-1", staff_uid="stf-uat", punch_time=1000, method="face",
        confidence=70.0, device_id="dev", gate_id="g", lane_id="l",
    ))
    assert len(PunchRepository(a).list_unsynced()) == 1
    a.close()

    _config_for(monkeypatch, tmp_path, PROD)
    b = _open(paths.get_default_db_path())
    assert PunchRepository(b).list_unsynced() == []
    b.close()


def test_mock_mode_is_just_another_environment(app_data, monkeypatch, tmp_path):
    _config_for(monkeypatch, tmp_path, MOCK)
    mock_db = paths.get_default_db_path()
    _config_for(monkeypatch, tmp_path, PROD)
    assert mock_db != paths.get_default_db_path()
    assert f"env-{environment_key(MOCK)}" in str(mock_db)


# ── Legacy migration ──────────────────────────────────────────────────


def _make_legacy_db(app_data) -> Path:
    legacy = app_data / "data" / "gate.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(legacy)
    StaffRepository(conn).upsert_staff("stf-legacy", "Legacy Person", 1000, 1000)
    conn.close()
    return legacy


def test_a_pre_partitioning_database_is_adopted_once_with_its_rows(app_data, monkeypatch, tmp_path):
    legacy = _make_legacy_db(app_data)
    paths.set_active_environment(None)
    key = environment_key(PROD)

    adopted = paths.adopt_legacy_database(key)

    assert adopted == paths.get_env_db_path(key)
    assert not legacy.exists()                       # moved, not copied
    conn = _open(adopted)
    assert StaffRepository(conn).get_full_name("stf-legacy") == "Legacy Person"
    conn.close()

    # Second start: nothing to adopt, nothing overwritten.
    assert paths.adopt_legacy_database(key) is None


def test_adoption_never_overwrites_an_environment_that_already_has_data(app_data):
    key = environment_key(PROD)
    existing = paths.get_env_db_path(key)
    conn = _open(existing)
    StaffRepository(conn).upsert_staff("stf-prod", "Prod Person", 1000, 1000)
    conn.close()
    legacy = _make_legacy_db(app_data)

    assert paths.adopt_legacy_database(key) is None
    assert legacy.exists()                            # left alone
    conn = _open(existing)
    assert StaffRepository(conn).get_full_name("stf-prod") == "Prod Person"
    assert StaffRepository(conn).get_full_name("stf-legacy") is None
    conn.close()


def test_wal_and_shm_side_files_move_with_the_database(app_data):
    legacy = _make_legacy_db(app_data)
    for suffix in ("-wal", "-shm"):
        legacy.with_name(legacy.name + suffix).write_bytes(b"")
    key = environment_key(PROD)

    target = paths.adopt_legacy_database(key)

    for suffix in ("-wal", "-shm"):
        assert not legacy.with_name(legacy.name + suffix).exists()
        assert target.with_name(target.name + suffix).exists()


# ── Shared device identity ────────────────────────────────────────────


def test_the_device_id_is_reused_across_environments(app_data, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from smart_gate.repositories.device_repo import DeviceRepository
    from smart_gate.services.device_service import DeviceService

    _config_for(monkeypatch, tmp_path, UAT)
    a = _open(paths.get_default_db_path())
    dev_a = DeviceService(SimpleNamespace(), DeviceRepository(a)).ensure_device("G", "L", "n")
    a.close()

    _config_for(monkeypatch, tmp_path, PROD)
    b = _open(paths.get_default_db_path())
    dev_b = DeviceService(SimpleNamespace(), DeviceRepository(b)).ensure_device("G", "L", "n")
    b.close()

    assert dev_a.device_id == dev_b.device_id
    identity = json.loads(paths.get_device_identity_path().read_text())
    assert identity["device_id"] == dev_a.device_id
