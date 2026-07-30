# ALPR Phase 1 — Desktop App Integration Guide
## For the AI agent building the desktop app

This document explains exactly how to integrate the AI pipeline into the
existing Python desktop app. The pipeline is fully encapsulated in
`alpr_pipeline.py`. The desktop app only needs to know about
`PlateRecognizer` and `PipelineResult`.

---

## File layout

Drop these two files into your desktop app directory:

```
your_desktop_app/
├── models/
│   └── detector.onnx          ← export from Colab notebook
├── alpr_pipeline.py           ← the pipeline module (this file)
├── requirements_alpr.txt      ← pip install -r this
└── ... (existing app files)
```

---

## Install dependencies

```bash
pip install -r requirements_alpr.txt
```

First install takes 2-3 minutes (PaddleOCR downloads pre-trained models ~80MB).

---

## Integration pattern

### Step 1 — Import and configure

```python
from alpr_pipeline import PlateRecognizer, PlateRecognizerConfig

# Create config — adjust paths and thresholds as needed
config = PlateRecognizerConfig(
    detector_path="models/detector.onnx",
    detector_conf_threshold=0.45,   # Lower = more detections, more noise
    ocr_conf_threshold=0.65,        # Lower = more OCR results, more errors
    min_votes_for_commit=3,         # Higher = more stable, slower response
    frame_buffer_size=7,
    use_gpu=False,                  # Set True if desktop has NVIDIA GPU
)

# Create ONE recognizer instance at app startup (not per-frame)
recognizer = PlateRecognizer(config)
```

### Step 2 — Warm up at startup (CRITICAL)

```python
# Call this ONCE when your camera worker starts, before the gate opens.
# This pre-loads models (~3-5 seconds) so the first vehicle isn't delayed.
recognizer.warmup()
```

### Step 3 — Call process_frame() in your camera loop

```python
# In your existing camera worker thread:
while camera_running:
    ret, frame = cap.read()
    if not ret:
        continue

    # Run the pipeline on every frame
    result = recognizer.process_frame(frame)

    if result:
        # A plate has been committed — enough frames agreed
        # result.plate_number  → normalized string e.g. "AA12345" or "1234"
        # result.confidence    → 0.0–1.0 (from temporal voting)
        # result.ocr_confidence → 0.0–1.0 (from the OCR model directly)
        # result.bbox          → (x1, y1, x2, y2) in original frame pixels
        # result.crop          → numpy array of cropped plate image
        # result.raw_text      → raw OCR output before normalization

        # Prefill the plate field in the guard UI
        emit_signal("plate_detected", result.plate_number, result.confidence)

        # Save the crop as evidence (use your existing evidence capture code)
        save_evidence_image(result.crop, event_id)
```

### Step 4 — Map result to your existing event payload

```python
def build_event_payload(result, gate_id, lane_id, direction, user_id):
    """
    Map PipelineResult to the event payload format
    defined in API_REFERENCE.md POST /events
    """
    return {
        "id": str(uuid.uuid4()),
        "event_time": int(time.time()),
        "gate_id": gate_id,
        "lane_id": lane_id,
        "direction": direction,
        "plate_number_raw": result.raw_text,          # Raw OCR output
        "plate_number_final": result.plate_number,    # Normalized
        "confidence": result.ocr_confidence,          # 0.0–1.0
        "decision": "NEED_MANUAL",                    # Guard confirms
        "decision_source": "AUTO",
        "manual_by_user_id": None,
        "manual_by_username": None,
        "manual_reason_id": None,
        "manual_reason_text": None,
        "manual_note": None,
        "is_offline_event": False,
        "evidence_uploaded_url": None,
    }
```

---

## Guard confirmation workflow

The AI result prefills the plate field. The guard sees it and either:
- **Confirms** → decision stays AUTO/ALLOW or AUTO/DENY based on allowlist check
- **Overrides** → guard changes plate or decision → decision_source becomes MANUAL

This is the existing manual override flow — the AI just removes the typing step.

---

## Handling the crop image for evidence upload

The `result.crop` is a BGR numpy array. Save it with:

```python
import cv2

def save_evidence_image(crop: np.ndarray, event_id: str) -> str:
    """Save plate crop to your evidence directory. Returns file path."""
    path = f"evidence/{event_id}.jpg"
    os.makedirs("evidence", exist_ok=True)
    cv2.imwrite(path, crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return path
```

Then use your existing `POST /events/{id}/evidence` upload flow with this path.

---

## Thread safety

`PlateRecognizer` is thread-safe. Model loading uses a lock. `process_frame()`
holds no locks during inference (ONNX and PaddleOCR are internally thread-safe
for single-session inference). The frame buffer (`plate_buffer`) uses `deque`
which is GIL-safe for single-reader/single-writer (your camera thread).

If you run multiple cameras on the same machine, create a separate
`PlateRecognizer` instance per camera.

---

## Resetting between vehicles

The buffer auto-clears after a result is committed. But if a vehicle sits
at the gate for a long time without a plate being detected (e.g. the car
reversed away before a result), you may want to reset manually:

```python
# Call when the barrier arm closes or vehicle is detected leaving
recognizer.reset()
```

---

## Tuning thresholds for your gate

Start with defaults. After testing at your real gate, adjust:

| Symptom | Adjustment |
|---|---|
| Too many false detections (non-plates flagged) | Raise `detector_conf_threshold` to 0.55 |
| Missing plates at a distance | Lower `detector_conf_threshold` to 0.35 |
| OCR producing garbage text | Raise `ocr_conf_threshold` to 0.75 |
| Slow to commit (waits too long) | Lower `min_votes_for_commit` to 2 |
| Committing on bad reads | Raise `min_votes_for_commit` to 4 |

---

## Phase 2 upgrade path

When you add letter support for Ethiopian plates:
1. In `alpr_pipeline.py` → `_normalize()`: remove or update the digits-only filter
2. In `_validate()`: add your regex for Ethiopian plate format
3. Optionally fine-tune PaddleOCR on Ethiopian plate font crops
4. No changes needed to the desktop app integration code
