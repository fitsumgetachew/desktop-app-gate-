"""The matching maths, mirrored from the reference implementation.

``verify_face_with_confidence`` in
``attendance-system/web_app/app.py`` is the original; ``identify`` is a pure
port of it. These tests use synthetic 128-d vectors on purpose — shipping real
face images in a test suite would put biometric data in git, and the maths does
not care where the numbers came from.

Distances here are constructed, not measured: a vector that differs from
another in one coordinate by ``d`` is exactly ``d`` away in L2, which makes the
threshold boundaries exact rather than approximate.
"""

import numpy as np
import pytest

from smart_gate.services.face_recognition_service import (
    FACE_MIN_CONFIDENCE,
    FACE_TOLERANCE,
    FaceIndex,
    KnownFace,
    decode_encoding,
    encode_to_blob,
    face_distances,
    identify,
)


def _vec(offset: float = 0.0) -> np.ndarray:
    """A 128-d vector at exactly ``offset`` L2 distance from ``_vec(0)``."""
    v = np.zeros(128, dtype=np.float64)
    v[0] = offset
    return v


PROBE = _vec(0.0)


def test_reference_thresholds_are_the_ones_the_department_uses():
    assert FACE_TOLERANCE == 0.45
    assert FACE_MIN_CONFIDENCE == 55.0


# ── Accept ────────────────────────────────────────────────────────────


def test_same_person_matches_at_the_reference_thresholds():
    """0.245 / 75.5% is what a leave-one-out match measured on the real photos."""
    known = [KnownFace("stf-1", "Abebe Bekele", _vec(0.245))]

    match = identify(PROBE, known)

    assert match is not None
    assert match.staff_uid == "stf-1"
    assert match.full_name == "Abebe Bekele"
    assert match.distance == pytest.approx(0.245)
    assert match.confidence == pytest.approx(75.5)


def test_each_person_contributes_only_their_best_photo():
    """Several enrolment photos must help, never hurt: a bad angle in one slot
    cannot drag down the person's score."""
    known = [
        KnownFace("stf-1", "Abebe", _vec(0.9)),    # a poor shot
        KnownFace("stf-1", "Abebe", _vec(0.20)),   # a good one
        KnownFace("stf-2", "Sara", _vec(0.30)),
    ]

    match = identify(PROBE, known)

    assert match.staff_uid == "stf-1"
    assert match.distance == pytest.approx(0.20)


def test_the_closest_person_wins():
    known = [
        KnownFace("stf-1", "Abebe", _vec(0.30)),
        KnownFace("stf-2", "Sara", _vec(0.24)),
    ]

    assert identify(PROBE, known).staff_uid == "stf-2"


def test_a_distance_exactly_on_the_tolerance_is_accepted():
    """<= tolerance, not < : 0.45 gives 55.0% confidence, also exactly on its
    floor, so the boundary case has to pass both comparisons."""
    known = [KnownFace("stf-1", "Abebe", _vec(0.45))]

    match = identify(PROBE, known)

    assert match is not None
    assert match.confidence == pytest.approx(55.0)


# ── Reject ────────────────────────────────────────────────────────────


def test_a_distance_just_over_the_tolerance_is_rejected():
    known = [KnownFace("stf-1", "Abebe", _vec(0.4501))]

    assert identify(PROBE, known) is None


def test_a_confidence_just_under_the_floor_is_rejected():
    """Raising min_confidence alone rejects a distance the tolerance allows —
    the two thresholds are an AND, not a restatement of each other."""
    known = [KnownFace("stf-1", "Abebe", _vec(0.44))]   # 56.0% confidence

    assert identify(PROBE, known, min_confidence=56.5) is None
    assert identify(PROBE, known, min_confidence=55.0) is not None


def test_a_stranger_is_rejected():
    known = [KnownFace("stf-1", "Abebe", _vec(0.8))]

    assert identify(PROBE, known) is None


def test_an_empty_index_returns_none():
    assert identify(PROBE, []) is None


def test_no_probe_returns_none():
    """A frame with no face at all must not be treated as a rejection of
    someone in particular."""
    assert identify(None, [KnownFace("stf-1", "Abebe", _vec(0.1))]) is None


# ── face_distances ────────────────────────────────────────────────────


def test_face_distances_matches_the_l2_definition():
    known = [_vec(0.0), _vec(0.25), _vec(1.0)]

    assert face_distances(known, PROBE) == pytest.approx([0.0, 0.25, 1.0])


def test_face_distances_on_an_empty_set_is_empty():
    assert face_distances([], PROBE).size == 0


# ── Blob round-trip ───────────────────────────────────────────────────


def test_encoding_survives_the_blob_round_trip():
    original = np.random.default_rng(7).random(128)

    restored = decode_encoding(encode_to_blob(original))

    assert np.array_equal(restored, original)


@pytest.mark.parametrize("blob", [None, b"", b"too-short", b"\x00" * 512])
def test_a_malformed_blob_decodes_to_none(blob):
    """A truncated row must be skipped, not fed to the matcher as a short
    vector that would raise on the first subtraction."""
    assert decode_encoding(blob) is None


# ── FaceIndex ─────────────────────────────────────────────────────────


class _Repo:
    def __init__(self, rows):
        self.rows = rows

    def list_encodings(self):
        return self.rows


def test_index_loads_and_identifies():
    index = FaceIndex()

    loaded = index.load_from_repo(
        _Repo(
            [
                ("stf-1", "Abebe", encode_to_blob(_vec(0.9))),
                ("stf-1", "Abebe", encode_to_blob(_vec(0.2))),
                ("stf-2", "Sara", encode_to_blob(_vec(0.7))),
            ]
        )
    )

    assert loaded == 3
    assert len(index) == 3
    assert index.staff_count == 2
    assert index.identify(PROBE).staff_uid == "stf-1"


def test_index_skips_malformed_rows_instead_of_failing_to_load():
    index = FaceIndex()

    loaded = index.load_from_repo(
        _Repo([("stf-1", "Abebe", b"junk"), ("stf-2", "Sara", encode_to_blob(_vec(0.1)))])
    )

    assert loaded == 1
    assert index.identify(PROBE).staff_uid == "stf-2"


def test_an_empty_index_identifies_nobody():
    index = FaceIndex()

    assert index.identify(PROBE) is None
    assert len(index) == 0
