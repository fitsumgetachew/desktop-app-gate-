from smart_gate.services.permit_rules import (
    STATUS_ALLOWED,
    STATUS_BLACKLISTED,
    STATUS_DENIED,
    STATUS_EXPIRED,
    STATUS_NOT_FOUND,
    STATUS_UNKNOWN,
    assess_plate,
    blacklist_override_error,
    effective_status,
    format_valid_to,
    is_blacklisted,
    is_expired,
)

NOW = 1_700_000_000
PAST = NOW - 60
FUTURE = NOW + 3600


# ── Expiry ────────────────────────────────────────────────────────────


def test_is_expired_past_and_future():
    assert is_expired(PAST, now=NOW) is True
    assert is_expired(FUTURE, now=NOW) is False


def test_is_expired_exactly_now_is_still_valid():
    assert is_expired(NOW, now=NOW) is False


def test_no_valid_to_never_expires():
    assert is_expired(None, now=NOW) is False
    assert is_expired(0, now=NOW) is False


def test_expired_permit_is_not_allowed_offline():
    """The offline bug: a cached ALLOWED row whose valid_to has passed."""
    assert effective_status(STATUS_ALLOWED, PAST, now=NOW) == STATUS_EXPIRED


def test_unexpired_permit_stays_allowed():
    assert effective_status(STATUS_ALLOWED, FUTURE, now=NOW) == STATUS_ALLOWED


def test_status_without_valid_to_passes_through():
    assert effective_status(STATUS_DENIED, None, now=NOW) == STATUS_DENIED
    assert effective_status(None, None, now=NOW) == STATUS_UNKNOWN


def test_status_is_case_and_space_insensitive():
    assert effective_status(" allowed ", FUTURE, now=NOW) == STATUS_ALLOWED


def test_assessment_marks_expired_and_suggests_deny():
    result = assess_plate("abc-1234", STATUS_ALLOWED, PAST, now=NOW)
    assert result.plate == "ABC1234"
    assert result.status == STATUS_EXPIRED
    assert result.expired is True
    assert result.allowed is False
    assert result.suggested_decision == "DENY"


def test_assessment_allows_valid_permit():
    result = assess_plate("ABC1234", STATUS_ALLOWED, FUTURE, now=NOW)
    assert result.allowed is True
    assert result.suggested_decision == "ALLOW"
    assert result.blacklisted is False


def test_assessment_not_found():
    result = assess_plate("ABC1234", None, found=False)
    assert result.found is False
    assert result.status == STATUS_NOT_FOUND
    assert result.suggested_decision is None


def test_format_valid_to_switches_label_on_expiry():
    fmt = str
    assert format_valid_to(None, fmt) == ""
    assert "valid to" in format_valid_to(2_000_000_000, fmt)
    assert "expired" in format_valid_to(1, fmt)


# ── Blacklist ─────────────────────────────────────────────────────────


def test_is_blacklisted_from_status_string():
    assert is_blacklisted(STATUS_BLACKLISTED) is True
    assert is_blacklisted("blacklisted") is True
    assert is_blacklisted(STATUS_ALLOWED) is False


def test_is_blacklisted_honours_server_alert_flag():
    assert is_blacklisted(STATUS_ALLOWED, alert=True) is True


def test_is_blacklisted_falls_back_to_status_when_alert_absent():
    """The alert flag is used when present but never depended upon."""
    assert is_blacklisted(STATUS_BLACKLISTED, alert=None) is True
    assert is_blacklisted(STATUS_BLACKLISTED, alert=False) is True


def test_blacklist_outranks_expiry():
    assert effective_status(STATUS_BLACKLISTED, PAST, now=NOW) == STATUS_BLACKLISTED


def test_blacklisted_assessment_preselects_deny():
    result = assess_plate("BLK-6666", STATUS_BLACKLISTED, None, now=NOW)
    assert result.blacklisted is True
    assert result.suggested_decision == "DENY"
    assert result.allowed is False


def test_blacklisted_via_alert_only():
    result = assess_plate("BLK6666", STATUS_ALLOWED, FUTURE, alert=True, now=NOW)
    assert result.blacklisted is True
    assert result.status == STATUS_BLACKLISTED
    assert result.suggested_decision == "DENY"


# ── Blacklist override ────────────────────────────────────────────────


def _blacklisted():
    return assess_plate("BLK6666", STATUS_BLACKLISTED, None, now=NOW)


def test_deny_on_blacklisted_needs_no_justification():
    assert blacklist_override_error(_blacklisted(), "DENY", "", "") is None


def test_allow_on_blacklisted_requires_a_reason():
    error = blacklist_override_error(_blacklisted(), "ALLOW", "", "some note")
    assert error is not None and "reason" in error.lower()


def test_allow_on_blacklisted_requires_a_note():
    error = blacklist_override_error(_blacklisted(), "ALLOW", "Manual override", "  ")
    assert error is not None and "note" in error.lower()


def test_allow_on_blacklisted_passes_with_reason_and_note():
    assert blacklist_override_error(
        _blacklisted(), "ALLOW", "Manual override", "Escorted by security"
    ) is None


def test_normal_plate_allow_is_never_gated():
    ok = assess_plate("ABC1234", STATUS_ALLOWED, FUTURE, now=NOW)
    assert blacklist_override_error(ok, "ALLOW", "", "") is None
    assert blacklist_override_error(None, "ALLOW", "", "") is None
