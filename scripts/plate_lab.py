"""Measure what the plate pipeline actually reads, so tuning stops being guesswork.

Two modes:

  # Every frame from the live gate camera, committed plates logged
  .venv/bin/python scripts/plate_lab.py live --seconds 60

  # A folder of stills (or saved evidence crops), with optional ground truth
  .venv/bin/python scripts/plate_lab.py images path/to/plates/

Ground truth for the images mode: name each file with the correct plate, e.g.
``AA12345.jpg`` or ``AA12345_dusk.jpg`` (text before the first ``_``). When the
truth is known the run prints exact-match accuracy, per-character accuracy, and
— most usefully — the confusion pairs, which is what tells you whether the fix
is a better crop, a different angle, or a character whitelist.

Every crop is written to the output dir so a wrong read can be looked at rather
than argued about.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from smart_gate.services.alpr_pipeline import (  # noqa: E402
    PlateRecognizer,
    PlateRecognizerConfig,
)
from smart_gate.utils.config import load_config  # noqa: E402
from smart_gate.utils.paths import get_detector_model_path  # noqa: E402
from smart_gate.utils.plates import normalize_plate  # noqa: E402

OUT_DIR = Path("/tmp/plate_lab")


def build_recognizer() -> PlateRecognizer:
    config = PlateRecognizerConfig(detector_path=str(get_detector_model_path()))
    recognizer = PlateRecognizer(config)
    recognizer.warmup()
    return recognizer


def truth_from_name(path: Path) -> str:
    """``AA12345_dusk.jpg`` -> ``AA12345``; unnamed files yield ""."""
    stem = path.stem.split("_")[0]
    return normalize_plate(stem)


def char_accuracy(expected: str, got: str) -> float:
    if not expected:
        return 0.0
    matches = sum(1 for e, g in zip(expected, got) if e == g)
    return matches / max(len(expected), len(got))


def run_images(folder: Path) -> int:
    images = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
    )
    if not images:
        print(f"no images in {folder}")
        return 1

    recognizer = build_recognizer()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    exact = scored = 0
    char_total = 0.0
    confusions: Counter = Counter()
    misses: list[tuple[str, str, str]] = []

    print(f"\n{'file':28} {'expected':12} {'read':12} conf   result")
    print("-" * 74)
    for image_path in images:
        frame = cv2.imread(str(image_path))
        if frame is None:
            print(f"{image_path.name:28} unreadable file")
            continue

        # One still is one frame, so drop the temporal buffer between images —
        # otherwise image N votes on image N+1's plate.
        recognizer.reset()
        result = None
        # The buffer needs min_votes_for_commit frames; a still is deterministic,
        # so feeding it that many times is equivalent to a stationary vehicle.
        for _ in range(recognizer._config.min_votes_for_commit):
            result = recognizer.process_frame(frame) or result

        got = result.plate_number if result else ""
        conf = f"{result.confidence:.2f}" if result else "  — "
        expected = truth_from_name(image_path)

        if expected:
            scored += 1
            char_total += char_accuracy(expected, got)
            if expected == got:
                exact += 1
                verdict = "OK"
            else:
                verdict = "MISS" if not got else "WRONG"
                misses.append((image_path.name, expected, got))
                for e, g in zip(expected, got):
                    if e != g:
                        confusions[f"{e}->{g}"] += 1
        else:
            verdict = "(no truth)"

        print(f"{image_path.name:28} {expected or '—':12} {got or '—':12} {conf}  {verdict}")

        if result is not None and result.crop is not None:
            cv2.imwrite(str(OUT_DIR / f"{image_path.stem}_crop.jpg"), result.crop)

    if scored:
        print("\n" + "=" * 74)
        print(f"exact-match accuracy : {exact}/{scored}  ({100 * exact / scored:.1f}%)")
        print(f"per-character accuracy: {100 * char_total / scored:.1f}%")
        if confusions:
            print("\nmost confused characters (fix these first):")
            for pair, count in confusions.most_common(8):
                print(f"   {pair}   x{count}")
        if misses:
            print("\nmisses:")
            for name, expected, got in misses:
                print(f"   {name}: expected {expected}, read {got or '(nothing)'}")
        print(f"\ncrops written to {OUT_DIR}")
    else:
        print("\nNo ground truth in the filenames — name files <PLATE>.jpg to score a run.")
    return 0


def run_live(seconds: int) -> int:
    app_config = load_config()
    source = app_config.camera_rtsp_url if app_config.camera_mode.upper() == "RTSP" else app_config.camera_index
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"could not open camera ({app_config.camera_mode})")
        return 1

    recognizer = build_recognizer()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"reading for {seconds}s — drive vehicles past the camera\n")

    started = time.monotonic()
    frames = commits = 0
    while time.monotonic() - started < seconds:
        ok, frame = cap.read()
        if not ok:
            continue
        frames += 1
        result = recognizer.process_frame(frame)
        if result is not None:
            commits += 1
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] {result.plate_number:12} conf={result.confidence:.2f} "
                  f"ocr={result.ocr_confidence:.2f} frames={result.frames_seen} "
                  f"raw={result.raw_text!r}")
            cv2.imwrite(str(OUT_DIR / f"{result.plate_number}_{commits}.jpg"), result.crop)
    cap.release()

    print(f"\n{frames} frames, {commits} plates committed. Crops in {OUT_DIR}.")
    print("Rename each crop to its true plate and re-run 'images' on that folder to score it.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    live = sub.add_parser("live", help="read from the configured gate camera")
    live.add_argument("--seconds", type=int, default=60)
    images = sub.add_parser("images", help="score a folder of stills")
    images.add_argument("folder", type=Path)

    args = parser.parse_args()
    if args.mode == "live":
        return run_live(args.seconds)
    return run_images(args.folder)


if __name__ == "__main__":
    sys.exit(main())
