"""Per-character consensus over the ALPR frame buffer.

The pipeline used to require N *identical* reads before committing a plate.
Real OCR rarely fails a whole plate — it wobbles one glyph — so that rule threw
away five good frames because of one bad character and the vehicle read as
unknown. These tests pin the merge that replaced it, and the limits that stop it
inventing a plate no frame actually saw.

Pure logic: `_consensus` is exercised directly, so no camera, no ONNX, no OCR.
"""

import pytest

from smart_gate.services.alpr_pipeline import PlateRecognizer, PlateRecognizerConfig


@pytest.fixture
def recognizer():
    # __init__ only stores config; the detector and OCR load lazily on first use.
    return PlateRecognizer(PlateRecognizerConfig())


def consensus(recognizer, reads):
    return recognizer._consensus(list(reads))


# ── The behaviour that already worked, unchanged ──────────────────────


def test_identical_reads_commit_with_full_agreement(recognizer):
    """The clean case must behave exactly as it always did."""
    assert consensus(recognizer, ["AA12345"] * 3) == ("AA12345", 1.0)


def test_too_few_reads_do_not_commit(recognizer):
    assert consensus(recognizer, ["AA12345", "AA12345"]) is None


def test_an_empty_buffer_commits_nothing(recognizer):
    assert consensus(recognizer, []) is None


# ── The false negative this fixes ─────────────────────────────────────


def test_one_wobbling_glyph_no_longer_loses_the_plate(recognizer):
    """Five frames read AA12345, one read the 5 as an S. The old exact-match
    rule committed nothing at all; the plate must survive."""
    plate, agreement = consensus(
        recognizer,
        ["AA12345", "AA12345", "AA1234S", "AA12345", "AA12345", "AA12345"],
    )
    assert plate == "AA12345"
    assert 0.9 < agreement < 1.0      # high, but honestly not perfect


def test_wobbles_in_different_positions_still_resolve(recognizer):
    """No single read is correct here, but each character has a majority."""
    plate, _ = consensus(recognizer, ["AA12345", "4A12345", "AA1Z345", "AA12345"])
    assert plate == "AA12345"


def test_the_majority_wins_a_position_not_the_first_frame(recognizer):
    plate, _ = consensus(recognizer, ["BB11111", "AA11111", "AA11111"])
    assert plate == "AA11111"


# ── The limits that stop it inventing a plate ─────────────────────────


def test_a_chaotic_buffer_commits_nothing(recognizer):
    """Every read disagrees with every other: there is no plate here, and
    stitching one together from per-position winners would be a fabrication."""
    assert consensus(recognizer, ["AB12345", "CD67890", "EF13579"]) is None


def test_reads_of_different_lengths_do_not_vote_against_each_other(recognizer):
    """"AA1234" and "AA12345" are two different readings, not a disagreement
    about one glyph. The better-supported length wins outright."""
    plate, agreement = consensus(
        recognizer, ["AA12345", "AA12345", "AA12345", "AA1234", "AA123"]
    )
    assert plate == "AA12345"
    assert agreement == 1.0          # the short reads never diluted it


def test_the_winning_length_still_needs_enough_support(recognizer):
    """Most-common length wins, but two reads is not a commit even so."""
    assert consensus(recognizer, ["AA12345", "AA12345", "AA1234", "AA123"]) is None


def test_agreement_below_the_floor_is_refused(recognizer):
    """Tightening the floor rejects a merge the default would have allowed —
    the knob is real, not decorative."""
    reads = ["AA12345", "AA12345", "BB99999"]
    assert consensus(recognizer, reads) is not None
    recognizer._config.min_char_agreement = 0.95
    assert consensus(recognizer, reads) is None


def test_agreement_is_reported_honestly(recognizer):
    """Confidence is the mean per-position agreement, so a downstream reader
    can tell a clean plate from a merged one."""
    _, clean = consensus(recognizer, ["AA12345"] * 4)
    _, merged = consensus(recognizer, ["AA12345", "AA12345", "AA12345", "AA1234S"])
    assert clean == 1.0
    assert merged < clean
