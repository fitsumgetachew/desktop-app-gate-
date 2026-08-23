"""Face embedding and matching for staff attendance.

Split so that the only part that runs per frame — the matching maths — is a
pure function over numpy arrays, testable with synthetic vectors and no camera,
no dlib and no database.

The matching rules are copied from the reference implementation the department
already runs:
``~/Software-Projects/SIT/attendance-system/web_app/app.py``
→ ``verify_face_with_confidence``. Distances are grouped per person, each person
contributes only their *best* (lowest) distance, and a match needs both a
distance within tolerance and a confidence above the floor. Measured on the real
reference photos, a leave-one-out same-person match scores distance 0.245 /
confidence 75.5 %, comfortably inside 0.45 / 55.0.

``face_recognition`` (and therefore dlib) is imported lazily inside the encoding
functions only. A station where the face stack failed to build must still sync
the roster and still run the gate — it simply stores no embeddings.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np

from smart_gate.services.face_overlay import FaceBox, worth_encoding

logger = logging.getLogger(__name__)

# Reference thresholds — see module docstring. Kept as module constants so a
# test can move them without touching config, and so the numbers are greppable.
# Thresholds proven in the department's running student system
# (attendance-system/face_system: FACE_MATCH_TOLERANCE = 0.5, distance-only).
# 0.45+55% double-gated the same distance twice and rejected borderline-but-real
# matches; 45% confidence == distance 0.55, so the distance check is the only
# binding gate — matching the reference behaviour while keeping the knob.
FACE_TOLERANCE = 0.50
FACE_MIN_CONFIDENCE = 45.0

# Below this, matching is mathematically dead: same-person distances run
# 0.2-0.45, so a tolerance under 0.30 rejects every real face while looking
# like a working camera. A configured value below it is clamped, loudly.
MIN_SANE_TOLERANCE = 0.30
MAX_SANE_TOLERANCE = 0.60

# dlib's face encoder emits a 128-d float64 vector.
ENCODING_DIM = 128
ENCODING_DTYPE = np.float64

# Detection runs on a half-scale frame: 220 ms at 640x480 versus 67 ms at
# 320x240 on the reference machine, and the face is still found. The ALPR
# thread is already spending a 640x640 ONNX pass plus PaddleOCR at 5 fps on the
# same CPU; at full scale the two pipelines fight and both stutter.
DETECTION_SCALE = 0.5

_import_warned = False


def _face_recognition():
    """Import ``face_recognition`` lazily, or return ``None`` once and warn.

    Mirrors how ``camera_service`` loads the ALPR pipeline: a missing model is a
    degraded feature, never a crash.
    """
    global _import_warned
    try:
        import face_recognition  # noqa: PLC0415 — deliberately lazy

        return face_recognition
    except Exception:
        if not _import_warned:
            _import_warned = True
            logger.warning(
                "face_recognition unavailable — staff attendance will sync the "
                "roster but recognise nobody",
                exc_info=True,
            )
        return None


@dataclass(frozen=True)
class FaceMatch:
    """A recognised staff member. ``distance`` is dlib's, ``confidence`` a %."""

    staff_uid: str
    full_name: str
    confidence: float
    distance: float


@dataclass(frozen=True)
class KnownFace:
    """One enrolled embedding. A staff member normally contributes several."""

    staff_uid: str
    full_name: str
    encoding: np.ndarray


# ----------------------------------------------------------------------
# Encoding (sync time only — never per frame)
# ----------------------------------------------------------------------


def encode_photo(source: Union[bytes, str, Path]) -> Optional[np.ndarray]:
    """Embed the single face in a roster photo, or ``None``.

    ``None`` is an ordinary outcome, not an error: in the reference photo set 4
    of 5 shots encoded and the profile shot produced no face at all. Callers
    record the miss and carry on.
    """
    fr = _face_recognition()
    if fr is None:
        return None
    try:
        image = _load_rgb(source)
        if image is None:
            return None
        encodings = fr.face_encodings(image)
    except Exception:
        logger.warning("Face encoding failed for a roster photo", exc_info=True)
        return None
    if not encodings:
        return None
    return np.asarray(encodings[0], dtype=ENCODING_DTYPE)


def _load_rgb(source: Union[bytes, str, Path]):
    """Decode JPEG/PNG bytes or a path into the RGB array dlib expects."""
    import cv2  # noqa: PLC0415 — heavy, and only needed on the sync path

    if isinstance(source, (str, Path)):
        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
    else:
        buffer = np.frombuffer(source, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        logger.warning("Could not decode a roster photo (unsupported or truncated)")
        return None
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


@dataclass(frozen=True)
class DetectedFace:
    """One face found in a live frame, with its embedding if one was worth taking."""

    box: FaceBox
    encoding: Optional[np.ndarray] = None


def detect_faces(frame_bgr) -> List[FaceBox]:
    """Locate faces in a live BGR frame, in full-frame coordinates.

    Detection runs on a half-scale copy and the boxes are scaled back up: 220 ms
    at 640x480 versus 67 ms at half scale on this machine, and the face is still
    found. HOG only — the CNN model needs a GPU this station does not have.
    """
    fr = _face_recognition()
    if fr is None or frame_bgr is None:
        return []
    import cv2  # noqa: PLC0415

    try:
        small = cv2.resize(frame_bgr, (0, 0), fx=DETECTION_SCALE, fy=DETECTION_SCALE)
        small_rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        locations = fr.face_locations(small_rgb, model="hog")
    except Exception:
        logger.debug("Face detection error on a frame", exc_info=True)
        return []
    factor = 1.0 / DETECTION_SCALE
    return [FaceBox.from_css(box).scaled(factor) for box in locations]


def encode_faces(frame_bgr, boxes: Sequence[FaceBox]) -> List[np.ndarray]:
    """Embed already-located faces, reading full-resolution pixels."""
    fr = _face_recognition()
    if fr is None or frame_bgr is None or not boxes:
        return []
    import cv2  # noqa: PLC0415

    try:
        full_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        locations = [(b.top, b.right, b.bottom, b.left) for b in boxes]
        encodings = fr.face_encodings(full_rgb, known_face_locations=locations)
    except Exception:
        logger.debug("Face encoding error on a frame", exc_info=True)
        return []
    return [np.asarray(enc, dtype=ENCODING_DTYPE) for enc in encodings]


def detect_and_encode(frame_bgr) -> List[DetectedFace]:
    """Locate every face, and embed only the ones big enough to be worth it.

    The cheap detector gates the expensive descriptor: a face smaller than
    ``MIN_FACE_PIXELS`` cannot match reliably, so spending 40-60 ms embedding it
    buys nothing. It is still returned — the overlay draws it, so someone
    standing too far back sees a box and the hint telling them to move closer,
    rather than nothing at all.
    """
    boxes = detect_faces(frame_bgr)
    if not boxes:
        return []
    encodable = [box for box in boxes if worth_encoding(box)]
    encodings = encode_faces(frame_bgr, encodable)
    by_box = dict(zip(encodable, encodings))
    return [DetectedFace(box, by_box.get(box)) for box in boxes]


def encode_frame_faces(frame_bgr) -> List[np.ndarray]:
    """Embeddings only — kept for callers that do not draw an overlay."""
    return [f.encoding for f in detect_and_encode(frame_bgr) if f.encoding is not None]


# ----------------------------------------------------------------------
# Matching — pure, and the only part that runs per frame
# ----------------------------------------------------------------------


def face_distances(known: Sequence[np.ndarray], probe: np.ndarray) -> np.ndarray:
    """Euclidean distance from ``probe`` to each known encoding.

    Identical to ``face_recognition.face_distance``, reimplemented here so the
    matching path — and its tests — need neither dlib nor a GPU.
    """
    if len(known) == 0:
        return np.empty(0, dtype=ENCODING_DTYPE)
    matrix = np.asarray(known, dtype=ENCODING_DTYPE)
    return np.linalg.norm(matrix - np.asarray(probe, dtype=ENCODING_DTYPE), axis=1)


def identify(
    probe_encoding: Optional[np.ndarray],
    known: Sequence[KnownFace],
    tolerance: float = FACE_TOLERANCE,
    min_confidence: float = FACE_MIN_CONFIDENCE,
) -> Optional[FaceMatch]:
    """Best staff match for one probe encoding, or ``None``.

    Pure function; mirrors ``verify_face_with_confidence`` step for step:
    group distances per ``staff_uid``, take each person's best (lowest), convert
    the winner to ``confidence = max(0, (1 - distance) * 100)``, and require
    *both* ``distance <= tolerance`` and ``confidence >= min_confidence``.

    Taking each person's best is what makes several enrolment photos help rather
    than hurt: a bad angle in slot 3 cannot drag down the person's score.
    """
    if probe_encoding is None or not known:
        return None

    distances = face_distances([face.encoding for face in known], probe_encoding)
    if distances.size == 0:
        return None

    best_per_person: dict = {}
    for face, distance in zip(known, distances):
        current = best_per_person.get(face.staff_uid)
        if current is None or distance < current[0]:
            best_per_person[face.staff_uid] = (float(distance), face.full_name)

    best_uid, (best_distance, best_name) = min(
        best_per_person.items(), key=lambda item: item[1][0]
    )
    confidence = max(0.0, (1.0 - best_distance) * 100.0)

    if best_distance <= tolerance and confidence >= min_confidence:
        return FaceMatch(
            staff_uid=best_uid,
            full_name=best_name,
            confidence=confidence,
            distance=best_distance,
        )
    logger.debug(
        "Face rejected: best distance %.3f, confidence %.1f%%",
        best_distance,
        confidence,
    )
    return None


# ----------------------------------------------------------------------
# In-memory index
# ----------------------------------------------------------------------


def decode_encoding(blob: Optional[bytes]) -> Optional[np.ndarray]:
    """Turn a stored BLOB back into a 128-d vector, or ``None`` if it is not one."""
    if not blob or len(blob) != ENCODING_DIM * ENCODING_DTYPE().itemsize:
        return None
    return np.frombuffer(blob, dtype=ENCODING_DTYPE)


def encode_to_blob(encoding: np.ndarray) -> bytes:
    return np.asarray(encoding, dtype=ENCODING_DTYPE).tobytes()


class MatchVoter:
    """Smooths per-frame verdicts over a short window.

    dlib returns a distance, not a decision, and a face near the tolerance
    crosses it frame to frame as the head moves a degree or the light shifts.
    Judging each frame alone turns that into a name flickering against "Not
    recognised" — which reads as broken recognition even when the person is
    matching more often than not.

    The plate pipeline has voted across frames since the beginning; faces did
    not, and this is the same idea. A window of recent verdicts, and a person is
    committed once they win ``min_votes`` of it. Raising the vote count makes
    recognition steadier and slower; it never makes it looser, because every
    vote still had to clear the distance threshold on its own.
    """

    def __init__(self, window: int = 5, min_votes: int = 2) -> None:
        self._window = max(1, int(window))
        self._min_votes = max(1, int(min_votes))
        self._recent: deque = deque(maxlen=self._window)

    def reset(self) -> None:
        self._recent.clear()

    def vote(self, match: Optional[FaceMatch]) -> Optional[FaceMatch]:
        """Record this frame's verdict and return the committed match, if any.

        A frame with no match still counts — it is evidence, and it is what lets
        a window empty out once somebody walks away.
        """
        self._recent.append(match)
        tally: dict = {}
        for seen in self._recent:
            if seen is None:
                continue
            best = tally.get(seen.staff_uid)
            # Keep each person's closest sighting in the window, mirroring the
            # best-of-N photos rule inside identify().
            if best is None or seen.distance < best.distance:
                tally[seen.staff_uid] = seen
        if not tally:
            return None
        counts = Counter(
            seen.staff_uid for seen in self._recent if seen is not None
        )
        uid, votes = counts.most_common(1)[0]
        if votes < self._min_votes:
            return None
        return tally[uid]


class FaceIndex:
    """Every usable staff embedding, held in memory.

    Loaded once at startup and rebuilt after each roster sync. Recognition runs
    on the camera thread at ~3 fps and must never touch SQLite, so reads take a
    reference to an immutable snapshot and writers swap a whole new one in under
    a lock.
    """

    def __init__(self) -> None:
        self._faces: Tuple[KnownFace, ...] = ()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._faces)

    @property
    def staff_count(self) -> int:
        return len({face.staff_uid for face in self._faces})

    def replace(self, faces: Sequence[KnownFace]) -> None:
        with self._lock:
            self._faces = tuple(faces)

    def load_from_repo(self, staff_repo) -> int:
        """Rebuild from ``staff_photos``. Returns the number of embeddings loaded."""
        faces: List[KnownFace] = []
        for staff_uid, full_name, blob in staff_repo.list_encodings():
            encoding = decode_encoding(blob)
            if encoding is None:
                logger.warning(
                    "Skipping a malformed embedding for staff %s", staff_uid
                )
                continue
            faces.append(KnownFace(staff_uid, full_name, encoding))
        self.replace(faces)
        logger.info(
            "Face index loaded: %d embeddings across %d staff",
            len(faces),
            len({face.staff_uid for face in faces}),
        )
        return len(faces)

    def identify(
        self,
        probe_encoding: Optional[np.ndarray],
        tolerance: float = FACE_TOLERANCE,
        min_confidence: float = FACE_MIN_CONFIDENCE,
    ) -> Optional[FaceMatch]:
        return identify(probe_encoding, self._faces, tolerance, min_confidence)


# One index per process, shared between the sync thread (which rebuilds it) and
# the face camera thread (which reads it) — same singleton pattern as
# services/token_store.py.
face_index = FaceIndex()
