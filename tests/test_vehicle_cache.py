"""Caching the richer owner/vehicle fields, and tolerating their absence."""

import sqlite3
from pathlib import Path

import pytest

from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import init_db
from smart_gate.services.vehicle_mapping import allowlist_item_to_record, record_to_vehicle


@pytest.fixture
def repo(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield AllowlistRepository(conn)
    conn.close()


RICH_ITEM = {
    "plate_number": "abc-1234",
    "status": "ALLOWED",
    "valid_to": 2_000_000_000,
    "valid_from": 1_000_000_000,
    "owner_name": "Abebe Bekele",
    "owner_first_name": "Abebe",
    "owner_last_name": "Bekele",
    "relationship": "STAFF",
    "department": "Registrar",
    "phone": "+251911000000",
    "vehicle_make": "Toyota",
    "vehicle_model": "Corolla",
    "vehicle_color": "White",
    "note": "Parks in lot B",
    "alert": False,
    "updated_at": 1_700_000_000,
}


# ── Mapping ───────────────────────────────────────────────────────────


def test_mapper_normalizes_the_plate():
    assert allowlist_item_to_record(RICH_ITEM)["plate_number"] == "ABC1234"


def test_mapper_carries_every_detail_field():
    record = allowlist_item_to_record(RICH_ITEM, version=42)
    assert record["relationship"] == "STAFF"
    assert record["department"] == "Registrar"
    assert record["vehicle_make"] == "Toyota"
    assert record["valid_from"] == 1_000_000_000
    assert record["version"] == 42


def test_mapper_tolerates_an_older_server_without_the_rich_fields():
    """The app must still work against a server that predates the new fields."""
    minimal = {
        "plate_number": "OLD1234",
        "status": "ALLOWED",
        "valid_to": None,
        "updated_at": 1_700_000_000,
    }
    record = allowlist_item_to_record(minimal, version=1)
    assert record["plate_number"] == "OLD1234"
    assert record["relationship"] is None
    assert record["vehicle_make"] is None
    assert record["valid_from"] is None


def test_mapper_turns_blank_strings_into_none():
    record = allowlist_item_to_record({**RICH_ITEM, "department": "  ", "note": ""})
    assert record["department"] is None
    assert record["note"] is None


# ── Round-trip through the cache ──────────────────────────────────────


def test_upsert_records_round_trips_every_field(repo):
    repo.upsert_records([allowlist_item_to_record(RICH_ITEM, version=1)])

    vehicle = repo.get_vehicle("ABC1234")
    assert vehicle is not None
    assert vehicle.status == "ALLOWED"
    assert vehicle.owner_first_name == "Abebe"
    assert vehicle.owner_last_name == "Bekele"
    assert vehicle.relationship == "STAFF"
    assert vehicle.department == "Registrar"
    assert vehicle.phone == "+251911000000"
    assert vehicle.vehicle_make == "Toyota"
    assert vehicle.vehicle_model == "Corolla"
    assert vehicle.vehicle_color == "White"
    assert vehicle.valid_from == 1_000_000_000
    assert vehicle.valid_to == 2_000_000_000
    assert vehicle.note == "Parks in lot B"
    assert vehicle.alert is False


def test_lookup_is_plate_format_insensitive(repo):
    repo.upsert_records([allowlist_item_to_record(RICH_ITEM, version=1)])
    assert repo.get_vehicle("abc 1234") is not None
    assert repo.get_vehicle("ABC-1234") is not None


def test_get_vehicle_returns_none_for_an_unknown_plate(repo):
    assert repo.get_vehicle("NOPE0000") is None


def test_display_helpers_collapse_missing_pieces(repo):
    repo.upsert_records([
        allowlist_item_to_record(
            {"plate_number": "P1", "status": "ALLOWED", "updated_at": 1,
             "owner_name": "Solo Name", "vehicle_make": "Toyota"},
            version=1,
        )
    ])
    vehicle = repo.get_vehicle("P1")
    assert vehicle.display_owner == "Solo Name"      # falls back to owner_name
    assert vehicle.display_vehicle == "Toyota"       # colour/model absent


def test_first_last_name_wins_over_flat_owner_name():
    vehicle = record_to_vehicle(allowlist_item_to_record(RICH_ITEM))
    assert vehicle.display_owner == "Abebe Bekele"


def test_upsert_records_overwrites_an_existing_row(repo):
    repo.upsert_records([allowlist_item_to_record(RICH_ITEM, version=1)])
    updated = {**RICH_ITEM, "department": "Finance", "status": "DENIED"}
    repo.upsert_records([allowlist_item_to_record(updated, version=2)])

    vehicle = repo.get_vehicle("ABC1234")
    assert vehicle.department == "Finance"
    assert vehicle.status == "DENIED"


def test_base_only_upsert_preserves_the_detail_fields(repo):
    """A lookup that only knows the base fields must not erase owner details."""
    repo.upsert_records([allowlist_item_to_record(RICH_ITEM, version=1)])
    repo.upsert_items([("ABC1234", "DENIED", None, "Abebe Bekele", 2, 2, 0)])

    vehicle = repo.get_vehicle("ABC1234")
    assert vehicle.status == "DENIED"          # base field updated
    assert vehicle.relationship == "STAFF"     # detail field survived
    assert vehicle.vehicle_make == "Toyota"


def test_replace_all_accepts_records_and_wipes_stale_rows(repo):
    repo.upsert_records([allowlist_item_to_record(RICH_ITEM, version=1)])
    repo.replace_all([
        allowlist_item_to_record(
            {"plate_number": "NEW1", "status": "ALLOWED", "updated_at": 5,
             "relationship": "STUDENT"},
            version=5,
        )
    ])
    assert repo.get_vehicle("ABC1234") is None
    assert repo.get_vehicle("NEW1").relationship == "STUDENT"


def test_blacklisted_alert_survives_the_round_trip(repo):
    repo.upsert_records([
        allowlist_item_to_record(
            {"plate_number": "BLK6666", "status": "BLACKLISTED", "alert": True,
             "updated_at": 1},
            version=1,
        )
    ])
    vehicle = repo.get_vehicle("BLK6666")
    assert vehicle.status == "BLACKLISTED"
    assert vehicle.alert is True
