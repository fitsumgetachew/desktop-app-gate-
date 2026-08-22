"""Overlay geometry and the positioning hints.

Pure integer maths — no Qt, no camera. The calibration tests matter most: the
thresholds were first set by guess, and against the real enrolment photos that
guess told two of six perfectly well-framed people to move closer. The measured
ranges below are the regression guard against doing that again.

Measured on this machine across the reference enrolment set (frames normalised
to 640x480): a well-framed face is 0.26-0.38 of frame height, fills 0.45-0.95 of
the guide, and sits up to 0.20 of the width / 0.23 of the height off centre.
"""

import pytest

from smart_gate.services.face_overlay import (
    CENTRE_TOLERANCE_X,
    CENTRE_TOLERANCE_Y,
    HINT_CENTRE,
    HINT_MOVE_BACK,
    HINT_MOVE_CLOSER,
    HINT_STEP_INTO_FRAME,
    MIN_FACE_PIXELS,
    STATE_MATCHED,
    STATE_SEARCHING,
    STATE_TRACKING,
    TOO_CLOSE_AREA_RATIO,
    TOO_FAR_AREA_RATIO,
    DetectionFrame,
    FaceBox,
    build_detection,
    guide_rect,
    positioning_hint,
    preview_scale,
    worth_encoding,
)

FRAME_W, FRAME_H = 640, 480
GUIDE = guide_rect(FRAME_W, FRAME_H)


def _centred(size: int) -> FaceBox:
    """A square face of ``size`` px, centred in a 640x480 frame."""
    half = size // 2
    return FaceBox(320 - half, 240 - half, 320 + half, 240 + half)


# ── FaceBox ───────────────────────────────────────────────────────────


def test_from_css_reorders_face_recognitions_tuple():
    """face_recognition hands back (top, right, bottom, left) — an ordering that
    has caused enough bugs to be worth naming the fields."""
    box = FaceBox.from_css((100, 400, 350, 150))

    assert (box.left, box.top, box.right, box.bottom) == (150, 100, 400, 350)


def test_box_geometry():
    box = FaceBox(10, 20, 110, 140)

    assert box.width == 100
    assert box.height == 120
    assert box.area == 12000
    assert box.centre == (60, 80)
    assert box.longest_side == 120


def test_scaling_a_box_is_uniform():
    box = FaceBox(10, 20, 110, 140).scaled(2.0)

    assert (box.left, box.top, box.right, box.bottom) == (20, 40, 220, 280)


def test_a_degenerate_box_has_no_area():
    assert FaceBox(50, 50, 10, 10).area == 0


# ── Guide frame ───────────────────────────────────────────────────────


def test_the_guide_is_centred_in_the_frame():
    assert GUIDE.centre == pytest.approx((FRAME_W // 2, FRAME_H // 2), abs=1)


def test_the_guide_is_portrait_and_face_sized():
    """It has to read as 'put your face in here'. A guide a face cannot fill
    just looks like failure."""
    assert GUIDE.height < FRAME_H * 0.5
    assert GUIDE.width < GUIDE.height


def test_the_guide_never_exceeds_a_very_wide_frame():
    guide = guide_rect(200, 900)

    assert guide.width <= 200


def test_an_empty_frame_has_an_empty_guide():
    assert guide_rect(0, 0).area == 0


# ── preview_scale ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target,expected",
    [((1280, 960), 2.0), ((640, 480), 1.0), ((320, 240), 0.5), ((1280, 480), 1.0)],
)
def test_preview_scale_matches_keep_aspect_ratio(target, expected):
    assert preview_scale(FRAME_W, FRAME_H, *target) == pytest.approx(expected)


def test_preview_scale_of_an_empty_frame_is_one():
    assert preview_scale(0, 0, 100, 100) == 1.0


# ── Hints ─────────────────────────────────────────────────────────────


def test_no_face_asks_them_to_step_in():
    assert positioning_hint(None, GUIDE, FRAME_W, FRAME_H) == HINT_STEP_INTO_FRAME


def test_a_small_face_is_told_to_move_closer():
    assert positioning_hint(_centred(70), GUIDE, FRAME_W, FRAME_H) == HINT_MOVE_CLOSER


def test_a_huge_face_is_told_to_move_back():
    assert positioning_hint(_centred(300), GUIDE, FRAME_W, FRAME_H) == HINT_MOVE_BACK


def test_a_well_sized_face_off_to_one_side_is_told_to_centre():
    box = FaceBox(20, 180, 170, 330)          # good size, hard against the left

    assert positioning_hint(box, GUIDE, FRAME_W, FRAME_H) == HINT_CENTRE


def test_a_well_placed_face_is_left_alone():
    assert positioning_hint(_centred(150), GUIDE, FRAME_W, FRAME_H) is None


def test_distance_is_reported_before_position():
    """One instruction at a time — a list of everything wrong is noise to
    someone trying to get to work."""
    tiny_and_off = FaceBox(10, 10, 70, 70)

    assert positioning_hint(tiny_and_off, GUIDE, FRAME_W, FRAME_H) == HINT_MOVE_CLOSER


def test_without_frame_dimensions_position_is_not_judged():
    """Only the size check can run; guessing at a centre would be worse than
    saying nothing."""
    box = FaceBox(20, 180, 170, 330)

    assert positioning_hint(box, GUIDE) is None


# ── Calibration against the real enrolment photos ─────────────────────


@pytest.mark.parametrize("face_px", [124, 148, 150, 180])
def test_every_measured_well_framed_face_size_passes_silently(face_px):
    """The sizes actually measured off the reference photos. An earlier guessed
    threshold nagged two of these."""
    assert positioning_hint(_centred(face_px), GUIDE, FRAME_W, FRAME_H) is None


@pytest.mark.parametrize("dx,dy", [(0.123, 0.115), (0.188, 0.121), (0.202, 0.115),
                                   (0.094, 0.233), (0.072, 0.185)])
def test_every_measured_off_centre_offset_passes_silently(dx, dy):
    """Real faces sit measurably off centre — that station's camera has a
    consistent bias, and a hint firing on all of them would be ignored."""
    cx = int(320 + dx * FRAME_W)
    cy = int(240 + dy * FRAME_H)
    box = FaceBox(cx - 75, cy - 75, cx + 75, cy + 75)

    assert positioning_hint(box, GUIDE, FRAME_W, FRAME_H) is None


def test_the_tolerances_keep_headroom_over_what_was_measured():
    assert CENTRE_TOLERANCE_X > 0.202
    assert CENTRE_TOLERANCE_Y > 0.233
    assert TOO_FAR_AREA_RATIO < 0.45        # smallest measured fill
    assert TOO_CLOSE_AREA_RATIO > 0.95      # largest measured fill


# ── worth_encoding ────────────────────────────────────────────────────


def test_a_tiny_face_is_not_worth_embedding():
    """The cheap detector gates the expensive descriptor — the desktop version
    of the reference station sending only a cropped face to its backend."""
    assert worth_encoding(FaceBox(0, 0, 40, 40)) is False


def test_a_normal_face_is_worth_embedding():
    assert worth_encoding(_centred(150)) is True


def test_the_smallest_measured_real_face_is_still_encoded():
    """124 px was the smallest well-framed face in the reference set; the floor
    must sit below it or real people stop being recognised."""
    assert MIN_FACE_PIXELS < 124
    assert worth_encoding(_centred(124)) is True


# ── build_detection ───────────────────────────────────────────────────


def test_no_faces_is_the_searching_state():
    detection = build_detection([], FRAME_W, FRAME_H)

    assert detection.state == STATE_SEARCHING
    assert detection.has_face is False
    assert detection.primary is None
    assert detection.hint is None


def test_the_largest_face_comes_first():
    """Whoever is closest to the camera is the one being served; bystanders in
    the background must not steal the box."""
    small, large = _centred(80), _centred(200)

    detection = build_detection([small, large], FRAME_W, FRAME_H)

    assert detection.boxes[0] == large
    assert detection.primary == large


def test_a_tracked_face_carries_its_hint():
    detection = build_detection([_centred(70)], FRAME_W, FRAME_H, STATE_TRACKING)

    assert detection.state == STATE_TRACKING
    assert detection.hint == HINT_MOVE_CLOSER


def test_a_matched_face_is_never_nagged():
    """They have been recognised and are about to walk away — telling them to
    move now is pure noise."""
    detection = build_detection(
        [_centred(70)], FRAME_W, FRAME_H, STATE_MATCHED, "Abebe Bekele"
    )

    assert detection.hint is None
    assert detection.label == "Abebe Bekele"


def test_an_empty_detection_frame_is_safe_to_draw():
    detection = DetectionFrame()

    assert detection.has_face is False
    assert detection.primary is None
