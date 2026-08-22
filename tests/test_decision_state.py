"""Traffic-light classifier and auto-allow countdown — pure logic, no Qt."""

import pytest

from smart_gate.models.domain import VehicleRecord
from smart_gate.services.decision_state import (
    AutoAllowCountdown,
    GateState,
    classify,
    is_alarm_state,
    suggested_decision,
)

NOW = 1_800_000_000
PAST = NOW - 3600
FUTURE = NOW + 3600


def vehicle(**kwargs) -> VehicleRecord:
    base = dict(plate_number="ABC1234", status="ALLOWED")
    base.update(kwargs)
    return VehicleRecord(**base)


# ── GREEN ─────────────────────────────────────────────────────────────


def test_allowed_vehicle_is_green():
    state = classify("ABC1234", vehicle(valid_to=FUTURE), now=NOW)
    assert state.state is GateState.GREEN
    assert state.can_auto_allow is True
    assert state.alarm is False
    assert state.can_register is False


def test_allowed_without_a_validity_window_is_green():
    state = classify("ABC1234", vehicle(), now=NOW)
    assert state.state is GateState.GREEN
    assert state.can_auto_allow is True


def test_green_headline_carries_plate_owner_and_relationship():
    state = classify(
        "abc-1234",
        vehicle(
            valid_to=FUTURE,
            owner_first_name="Abebe",
            owner_last_name="Bekele",
            relationship="STAFF",
        ),
        now=NOW,
    )
    assert "ABC1234" in state.headline
    assert "Abebe Bekele" in state.headline
    assert "STAFF" in state.headline


def test_green_suggests_allow():
    assert suggested_decision(classify("ABC1234", vehicle(), now=NOW)) == "ALLOW"


# ── RED ───────────────────────────────────────────────────────────────


def test_blacklisted_is_red_with_alarm():
    state = classify("BLK6666", vehicle(plate_number="BLK6666", status="BLACKLISTED"), now=NOW)
    assert state.state is GateState.RED
    assert state.alarm is True
    assert state.can_auto_allow is False
    assert state.can_register is False


def test_denied_is_red_but_silent():
    state = classify("DEN1111", vehicle(plate_number="DEN1111", status="DENIED"), now=NOW)
    assert state.state is GateState.RED
    assert state.alarm is False
    assert state.can_auto_allow is False


def test_alert_flag_alone_raises_the_red_alarm():
    state = classify("ABC1234", vehicle(status="ALLOWED", alert=True, valid_to=FUTURE), now=NOW)
    assert state.state is GateState.RED
    assert state.alarm is True


def test_blacklist_outranks_an_expired_permit():
    state = classify(
        "BLK6666",
        vehicle(plate_number="BLK6666", status="BLACKLISTED", valid_to=PAST),
        now=NOW,
    )
    assert state.state is GateState.RED
    assert state.alarm is True


def test_red_suggests_deny():
    state = classify("BLK6666", vehicle(status="BLACKLISTED"), now=NOW)
    assert suggested_decision(state) == "DENY"


def test_is_alarm_state_only_for_blacklist():
    assert is_alarm_state("BLACKLISTED") is True
    assert is_alarm_state("blacklisted") is True
    assert is_alarm_state("DENIED") is False
    assert is_alarm_state("ALLOWED", alert=True) is True
    assert is_alarm_state("ALLOWED") is False


# ── ORANGE ────────────────────────────────────────────────────────────


def test_unknown_plate_is_orange_and_offers_registration():
    state = classify("NEW9999", None, now=NOW)
    assert state.state is GateState.ORANGE
    assert state.can_register is True
    assert state.can_auto_allow is False
    assert state.alarm is False
    assert "NEW9999" in state.headline


def test_expired_permit_is_orange_not_green():
    state = classify("ABC1234", vehicle(valid_to=PAST), now=NOW)
    assert state.state is GateState.ORANGE
    assert state.can_auto_allow is False
    assert "expired" in state.headline.lower()


def test_not_yet_valid_permit_is_orange_not_green():
    state = classify("ABC1234", vehicle(valid_from=FUTURE, valid_to=FUTURE + 86400), now=NOW)
    assert state.state is GateState.ORANGE
    assert state.can_auto_allow is False
    assert "not yet valid" in state.headline.lower()


def test_permit_inside_its_window_is_green():
    state = classify("ABC1234", vehicle(valid_from=PAST, valid_to=FUTURE), now=NOW)
    assert state.state is GateState.GREEN


def test_orange_suggests_nothing():
    assert suggested_decision(classify("NEW9999", None, now=NOW)) is None


def test_blank_plate_is_idle():
    assert classify("", None, now=NOW).is_idle
    assert classify("---", None, now=NOW).is_idle


# ── Detail rows ───────────────────────────────────────────────────────


def test_details_include_the_rich_fields():
    state = classify(
        "ABC1234",
        vehicle(
            valid_to=FUTURE,
            owner_first_name="Abebe",
            owner_last_name="Bekele",
            relationship="STAFF",
            department="Registrar",
            phone="+251911000000",
            vehicle_make="Toyota",
            vehicle_model="Corolla",
            vehicle_color="White",
            note="Parks in lot B",
        ),
        now=NOW,
        fmt_ts=lambda ts: "SOMEDATE",
    )
    labels = dict(state.details)
    assert labels["Owner"] == "Abebe Bekele"
    assert labels["Relationship"] == "STAFF"
    assert labels["Department"] == "Registrar"
    assert labels["Phone"] == "+251911000000"
    assert labels["Vehicle"] == "White Toyota Corolla"
    assert labels["Note"] == "Parks in lot B"
    assert labels["Valid to"] == "SOMEDATE"


def test_missing_details_collapse_instead_of_showing_none():
    state = classify("ABC1234", vehicle(valid_to=FUTURE, owner_name="Solo"), now=NOW)
    labels = dict(state.details)
    assert labels["Owner"] == "Solo"
    for absent in ("Relationship", "Department", "Phone", "Vehicle", "Note"):
        assert absent not in labels
    assert all(value and value != "None" for _, value in state.details)


def test_blank_strings_are_treated_as_missing():
    state = classify(
        "ABC1234",
        vehicle(valid_to=FUTURE, owner_name="Solo", department="   ", phone=""),
        now=NOW,
    )
    labels = dict(state.details)
    assert "Department" not in labels
    assert "Phone" not in labels


def test_partial_vehicle_description_collapses():
    state = classify("ABC1234", vehicle(valid_to=FUTURE, vehicle_make="Toyota"), now=NOW)
    assert dict(state.details)["Vehicle"] == "Toyota"


def test_unknown_plate_has_no_detail_rows():
    assert classify("NEW9999", None, now=NOW).details == []


# ── Countdown ─────────────────────────────────────────────────────────


def test_countdown_fires_after_the_configured_seconds():
    countdown = AutoAllowCountdown(3)
    assert countdown.start("ABC1234") is True
    assert countdown.remaining == 3
    assert countdown.tick() is False   # 2
    assert countdown.tick() is False   # 1
    assert countdown.tick() is True    # 0 → fire
    assert countdown.active is False


def test_countdown_is_disabled_at_zero_seconds():
    countdown = AutoAllowCountdown(0)
    assert countdown.enabled is False
    assert countdown.start("ABC1234") is False
    assert countdown.active is False
    assert countdown.tick() is False


def test_countdown_ignores_a_blank_plate():
    countdown = AutoAllowCountdown(5)
    assert countdown.start("") is False
    assert countdown.active is False


def test_cancel_stops_the_countdown_firing():
    countdown = AutoAllowCountdown(2)
    countdown.start("ABC1234")
    countdown.cancel()
    assert countdown.active is False
    assert countdown.tick() is False


def test_a_different_plate_cancels_the_countdown():
    """The whole point: never open the barrier for a car that already left."""
    countdown = AutoAllowCountdown(5)
    countdown.start("ABC1234")
    assert countdown.on_plate_committed("XYZ9999") is True
    assert countdown.active is False
    assert countdown.tick() is False


def test_re_detecting_the_same_plate_does_not_restart_the_countdown():
    countdown = AutoAllowCountdown(5)
    countdown.start("ABC1234")
    countdown.tick()   # 4 left
    assert countdown.on_plate_committed("ABC1234") is False
    assert countdown.remaining == 4


def test_same_plate_in_a_different_format_is_still_the_same_plate():
    countdown = AutoAllowCountdown(5)
    countdown.start("ABC1234")
    assert countdown.on_plate_committed("abc-1234") is False
    assert countdown.active is True


def test_a_new_plate_with_no_countdown_running_is_a_no_op():
    countdown = AutoAllowCountdown(5)
    assert countdown.on_plate_committed("ABC1234") is False


def test_starting_a_new_countdown_replaces_the_previous_plate():
    countdown = AutoAllowCountdown(5)
    countdown.start("ABC1234")
    countdown.start("XYZ9999", seconds=2)
    assert countdown.plate == "XYZ9999"
    assert countdown.remaining == 2


def test_set_seconds_applies_to_the_next_countdown():
    countdown = AutoAllowCountdown(5)
    countdown.set_seconds(0)
    assert countdown.enabled is False
    assert countdown.start("ABC1234") is False
    countdown.set_seconds(2)
    assert countdown.start("ABC1234") is True
    assert countdown.remaining == 2


@pytest.mark.parametrize("seconds", [1, 2, 5, 10])
def test_countdown_fires_exactly_once_at_zero(seconds):
    countdown = AutoAllowCountdown(seconds)
    countdown.start("ABC1234")
    fires = [countdown.tick() for _ in range(seconds)]
    assert fires.count(True) == 1
    assert fires[-1] is True
