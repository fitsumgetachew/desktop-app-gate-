"""The plate read zone: parsing, cropping, and the fallback that matters.

A malformed zone must mean "read the whole frame" — a typo in Settings must
degrade to today's behaviour, never to a sliver of sky nobody chose.
"""

import numpy as np
import pytest

from smart_gate.utils.roi import ReadZone, parse_read_zone


# ── Parsing ───────────────────────────────────────────────────────────


def test_empty_and_none_mean_full_frame():
    assert parse_read_zone("") is None
    assert parse_read_zone(None) is None
    assert parse_read_zone("   ") is None


def test_a_valid_zone_parses():
    zone = parse_read_zone("0.5,0.25,0.5,0.6")
    assert zone == ReadZone(x=0.5, y=0.25, w=0.5, h=0.6)


def test_spaces_are_tolerated():
    assert parse_read_zone(" 0.5 , 0.25 , 0.5 , 0.6 ") is not None


@pytest.mark.parametrize(
    "raw",
    [
        "0.5,0.25,0.5",          # three numbers
        "a,b,c,d",               # not numeric
        "1.2,0,0.5,0.5",         # origin outside the frame
        "0,0,0.01,0.5",          # too narrow to be usable
        "0.9,0.9,0.5,0.5",       # leaves the frame entirely... clamps? no: x+w>1
    ],
)
def test_invalid_zones_fall_back_to_full_frame(raw):
    assert parse_read_zone(raw) is None


def test_a_zone_touching_the_edges_is_fine():
    assert parse_read_zone("0,0,1,1") == ReadZone(0.0, 0.0, 1.0, 1.0)


# ── Cropping ──────────────────────────────────────────────────────────


def test_apply_crops_the_right_pixels():
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    frame[25:85, 100:200] = 255          # light up the intended zone

    zone = ReadZone(x=0.5, y=0.25, w=0.5, h=0.6)
    crop = zone.apply(frame)

    assert crop.shape == (60, 100, 3)
    assert crop.min() == 255             # nothing outside the zone leaked in


def test_apply_is_a_view_not_a_copy():
    """5 fps x full-frame copies would be pure waste; slicing is free."""
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    crop = ReadZone(0.5, 0.25, 0.5, 0.6).apply(frame)
    assert crop.base is frame


def test_pixel_rect_never_produces_an_empty_crop():
    """Rounding at the extreme edge must still yield at least one pixel."""
    zone = ReadZone(x=0.99, y=0.99, w=0.05, h=0.05)
    x1, y1, x2, y2 = zone.pixel_rect(640, 480)
    assert x2 > x1 and y2 > y1
    assert x2 <= 640 and y2 <= 480


def test_the_zoom_actually_zooms():
    """The whole point: after the 640x640 letterbox, a plate inside the zone
    is several times larger than it would have been from the full frame."""
    frame_w, plate_w = 1920, 120         # a plate 120 px wide in a 1080p frame
    zone = ReadZone(x=0.5, y=0.25, w=0.5, h=0.6)   # zone is 960 px wide

    full_frame_scale = 640 / frame_w      # letterboxing the full frame
    zone_scale = 640 / (frame_w * zone.w)  # letterboxing just the zone

    plate_px_before = plate_w * full_frame_scale
    plate_px_after = plate_w * zone_scale

    assert plate_px_before == 40          # borderline-invisible to the detector
    assert plate_px_after == 80           # double the pixels, same camera
    assert plate_px_after / plate_px_before == 1 / zone.w
