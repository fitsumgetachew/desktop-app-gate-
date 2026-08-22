"""Scanning for attached cameras.

The probe is injectable, so none of this needs a camera. What is worth pinning
is that a device which opens but never delivers a frame is not reported — on
Linux those are the metadata nodes sitting next to the real capture device, and
offering one as "USB device 1" would hand the operator a camera that shows
nothing.
"""

from smart_gate.services.camera_discovery import (
    MAX_PROBE_INDEX,
    DiscoveredCamera,
    candidate_indices,
    discover_cameras,
)


def test_only_indices_that_produced_a_frame_are_reported():
    present = {0: DiscoveredCamera(0, 640, 480), 3: DiscoveredCamera(3, 1280, 720)}

    found = discover_cameras(probe=present.get, indices=[0, 1, 2, 3])

    assert [c.index for c in found] == [0, 3]


def test_nothing_attached_reports_nothing():
    assert discover_cameras(probe=lambda i: None, indices=[0, 1, 2]) == []


def test_results_keep_probe_order():
    present = {i: DiscoveredCamera(i) for i in (4, 1)}

    found = discover_cameras(probe=present.get, indices=[1, 4])

    assert [c.index for c in found] == [1, 4]


def test_the_label_includes_the_resolution_when_known():
    assert DiscoveredCamera(2, 1280, 720).label == "USB device 2 (1280x720)"


def test_the_label_degrades_without_a_resolution():
    assert DiscoveredCamera(2).label == "USB device 2"


def test_candidate_indices_stay_within_the_probe_limit():
    """Each miss costs a real open-timeout, so the search has to be bounded."""
    indices = candidate_indices()

    assert indices
    assert all(0 <= i <= MAX_PROBE_INDEX for i in indices)
    assert len(indices) == len(set(indices))


def test_candidate_indices_fall_back_to_a_plain_range():
    """On a platform with no /dev/video* hint, probe the low indices."""
    indices = candidate_indices(max_index=3)

    assert all(0 <= i <= 3 for i in indices)
