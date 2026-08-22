"""Geometry for the attendance camera overlay.

A staff member standing at the window has no idea what the camera can see, so
they drift out of frame, stand too far back, or look away — and every one of
those is an invisible failure that just reads as "Not recognised". The overlay
turns that into something they can act on: a guide frame showing where to stand,
a box tracking their face, and one short hint when they are in the wrong place.

Modelled on the department's existing attendance station
(``attendance-system/web_app/templates/live.html`` → ``drawSingleFaceBox``):
a thin rectangle with thicker corner brackets, green once the match is good and
orange while it is not.

Everything here is pure integer geometry — no Qt, no camera — because the
letterboxing and scaling maths is exactly the part that is easy to get subtly
wrong and impossible to eyeball at 3 fps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Drawing weights, matching the reference implementation.
BOX_LINE_WIDTH = 2
CORNER_LINE_WIDTH = 4
CORNER_LENGTH = 26

# The guide frame, as a fraction of the video frame. Calibrated against the
# real enrolment photos rather than guessed: across six people a well-framed
# face measured 0.26-0.38 of frame height (mean 0.31), and dlib's box is very
# nearly square. Sized so a correctly-placed face fills roughly two thirds to
# nine tenths of the guide — it has to read as "put your face in here", so a
# guide the face cannot hope to fill would just look like failure.
GUIDE_HEIGHT_FRACTION = 0.42
GUIDE_ASPECT = 0.85                     # width / height

# How much of the guide the face should fill before it counts as well placed.
# Against the measured photos the good range is 0.45-0.94, so these sit well
# clear of it on both sides: an earlier, tighter pair would have told two of
# those six perfectly well-framed people to move closer.
TOO_FAR_AREA_RATIO = 0.30               # ~0.21 of frame height — genuinely far
TOO_CLOSE_AREA_RATIO = 1.80             # ~0.52 of frame height — genuinely close

# How far the face centre may sit from the frame centre before we ask them to
# centre themselves — as a fraction of the FRAME, not the guide, because what
# matters is the risk of drifting out of shot, not how the guide happens to be
# sized. Measured across eleven enrolment photos, well-framed faces sat up to
# 0.20 of the width and 0.23 of the height off centre (that station's camera
# has a consistent bias), so these leave real headroom above it. A hint that
# fires on people who are standing perfectly well is a hint everyone learns to
# ignore.
CENTRE_TOLERANCE_X = 0.28
CENTRE_TOLERANCE_Y = 0.30

# A detected box smaller than this many pixels on its longest side is not worth
# encoding: dlib's descriptor is unreliable at that size, and encoding costs
# 40-60 ms we would rather not spend. This is the desktop equivalent of the
# reference station's "send only the detected face" — the cheap detector gates
# the expensive step.
MIN_FACE_PIXELS = 80

# Overlay states, which pick the colour.
STATE_SEARCHING = "searching"       # no face in frame
STATE_TRACKING = "tracking"         # a face, not (yet) matched
STATE_MATCHED = "matched"           # recognised

HINT_STEP_INTO_FRAME = "Step into the frame"
HINT_MOVE_CLOSER = "Move closer"
HINT_MOVE_BACK = "Move back"
HINT_CENTRE = "Centre your face"
HINT_HOLD_STILL = "Hold still"


@dataclass(frozen=True)
class FaceBox:
    """A face rectangle in frame pixel coordinates.

    Field order matches ``face_recognition``'s ``(top, right, bottom, left)``
    tuples, but named, because that ordering has caused enough bugs elsewhere.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def centre(self) -> Tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    @property
    def longest_side(self) -> int:
        return max(self.width, self.height)

    def scaled(self, factor: float) -> "FaceBox":
        return FaceBox(
            int(round(self.left * factor)),
            int(round(self.top * factor)),
            int(round(self.right * factor)),
            int(round(self.bottom * factor)),
        )

    @classmethod
    def from_css(cls, box: Sequence[int]) -> "FaceBox":
        """Build from ``face_recognition``'s (top, right, bottom, left)."""
        top, right, bottom, left = box
        return cls(int(left), int(top), int(right), int(bottom))


@dataclass(frozen=True)
class DetectionFrame:
    """What the recognition pass saw, for the overlay to draw.

    Carries the frame size it was measured against so the view can scale it to
    whatever the preview happens to be, without assuming the two match.
    """

    boxes: Tuple[FaceBox, ...] = ()
    frame_width: int = 0
    frame_height: int = 0
    state: str = STATE_SEARCHING
    label: Optional[str] = None          # e.g. the recognised name
    hint: Optional[str] = None

    @property
    def has_face(self) -> bool:
        return bool(self.boxes)

    @property
    def primary(self) -> Optional[FaceBox]:
        """The largest face — the one standing closest to the camera."""
        if not self.boxes:
            return None
        return max(self.boxes, key=lambda b: b.area)


def guide_rect(frame_width: int, frame_height: int) -> FaceBox:
    """The 'stand here' frame, centred in the video."""
    if frame_width <= 0 or frame_height <= 0:
        return FaceBox(0, 0, 0, 0)
    height = int(frame_height * GUIDE_HEIGHT_FRACTION)
    width = int(height * GUIDE_ASPECT)
    # A very wide-but-short frame can make the guide wider than the video.
    width = min(width, int(frame_width * 0.9))
    left = (frame_width - width) // 2
    top = (frame_height - height) // 2
    return FaceBox(left, top, left + width, top + height)


def preview_scale(
    frame_width: int, frame_height: int, target_width: int, target_height: int
) -> float:
    """Uniform scale used by ``KeepAspectRatio`` to fit the frame in the target.

    The view draws onto the *already scaled* pixmap, so this single factor is
    all the mapping that is needed — there is no letterbox offset to add,
    which is precisely the offset that usually gets forgotten.
    """
    if frame_width <= 0 or frame_height <= 0:
        return 1.0
    return min(target_width / frame_width, target_height / frame_height)


def positioning_hint(
    box: Optional[FaceBox],
    guide: FaceBox,
    frame_width: int = 0,
    frame_height: int = 0,
) -> Optional[str]:
    """One short instruction, or ``None`` when the person is well placed.

    Deliberately one line at a time: a list of everything wrong is noise to
    someone who is just trying to get to work. Distance is judged against the
    guide, position against the frame.
    """
    if box is None or box.area == 0:
        return HINT_STEP_INTO_FRAME
    if guide.area == 0:
        return None

    ratio = box.area / guide.area
    if ratio < TOO_FAR_AREA_RATIO:
        return HINT_MOVE_CLOSER
    if ratio > TOO_CLOSE_AREA_RATIO:
        return HINT_MOVE_BACK

    if frame_width > 0 and frame_height > 0:
        face_x, face_y = box.centre
        if (
            abs(face_x - frame_width / 2) > frame_width * CENTRE_TOLERANCE_X
            or abs(face_y - frame_height / 2) > frame_height * CENTRE_TOLERANCE_Y
        ):
            return HINT_CENTRE
    return None


def worth_encoding(box: FaceBox, min_pixels: int = MIN_FACE_PIXELS) -> bool:
    """Whether this face is big enough to be worth the encoding cost.

    The cheap detector gates the expensive descriptor — the same idea as the
    reference station sending only a cropped face to its backend, except the
    saving here is CPU rather than bandwidth.
    """
    return box.longest_side >= min_pixels


def build_detection(
    boxes: Sequence[FaceBox],
    frame_width: int,
    frame_height: int,
    state: str = STATE_TRACKING,
    label: Optional[str] = None,
) -> DetectionFrame:
    """Assemble what the overlay needs, hint included."""
    ordered: List[FaceBox] = sorted(boxes, key=lambda b: b.area, reverse=True)
    if not ordered:
        return DetectionFrame(
            (), frame_width, frame_height, STATE_SEARCHING, None, None
        )
    guide = guide_rect(frame_width, frame_height)
    hint = positioning_hint(ordered[0], guide, frame_width, frame_height)
    if state == STATE_MATCHED:
        # They have been recognised; telling them to move now is pointless
        # noise, and they are about to walk away anyway.
        hint = None
    return DetectionFrame(tuple(ordered), frame_width, frame_height, state, label, hint)


EMPTY_DETECTION = DetectionFrame()
