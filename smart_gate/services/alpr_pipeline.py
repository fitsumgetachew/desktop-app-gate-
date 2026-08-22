"""
alpr_pipeline.py — Production ALPR pipeline module
===================================================
This module is the single integration point between the desktop app
and the AI pipeline. The desktop app worker imports PlateRecognizer
and calls process_frame() on each camera frame.

Design principles:
    - Single public method: process_frame(frame) → PipelineResult | None
    - Thread-safe (the desktop app runs the camera in a worker thread)
    - Lazy-loads models on first call (fast app startup)
    - Stateless except for the temporal frame buffer
    - All config via PlateRecognizerConfig dataclass
    - Easy to swap models: just change model_path in config

Integration example (in your desktop camera worker):
    from alpr_pipeline import PlateRecognizer, PlateRecognizerConfig

    config = PlateRecognizerConfig(detector_path="models/detector.onnx")
    recognizer = PlateRecognizer(config)
    recognizer.warmup()  # Call once at app startup

    # In your frame loop:
    result = recognizer.process_frame(frame)
    if result:
        # result.plate_number → string, e.g. "1234"
        # result.confidence   → float 0.0–1.0
        # result.bbox         → (x1, y1, x2, y2) in original pixel coords
        # result.crop         → numpy array of the cropped plate (for evidence)
        prefill_plate_field(result.plate_number)
        save_evidence_image(result.crop)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional, Tuple

import cv2
import numpy as np

from smart_gate.utils.plates import normalize_plate

logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

@dataclass
class PlateRecognizerConfig:
    """
    All tunable parameters in one place.
    Change these here — don't scatter magic numbers through the codebase.
    """
    # Model
    detector_path: str = "models/detector.onnx"
    detector_input_size: int = 640
    detector_conf_threshold: float = 0.45
    detector_iou_threshold: float = 0.45

    # OCR
    ocr_lang: str = "en"
    ocr_conf_threshold: float = 0.65
    use_gpu: bool = False       # Set True if the desktop machine has a CUDA GPU

    # Temporal aggregation
    frame_buffer_size: int = 7
    min_votes_for_commit: int = 3   # How many agreeing frames before committing
    # Below this mean per-character agreement the buffered reads disagree too
    # much to merge — see _consensus. 0.6 keeps a one-glyph wobble across a
    # normal buffer while rejecting a genuinely unstable read.
    min_char_agreement: float = 0.6

    # Preprocessing
    clahe_clip_limit: float = 2.5
    clahe_tile_size: Tuple[int, int] = (8, 8)

    # Plate filter (Phase 2: change regex to match Ethiopian plate format)
    # 5 is the shortest real plate we accept — below that OCR fragments
    # ("AB", "12") would be committed as if they were plates.
    min_plate_length: int = 5
    max_plate_length: int = 12


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    """
    What process_frame() returns when it has a committed plate reading.

    plate_number:  Normalized plate string (e.g. "AA12345" or "1234")
    confidence:    Aggregate confidence 0.0–1.0 from temporal voting
    raw_text:      Raw OCR output before normalization
    ocr_confidence: Per-character confidence from OCR model
    bbox:          (x1, y1, x2, y2) bounding box in original frame pixels
    crop:          numpy array — cropped plate image (for evidence upload)
    frames_seen:   How many frames contributed to this result
    """
    plate_number: str
    confidence: float
    raw_text: str
    ocr_confidence: float
    bbox: Tuple[int, int, int, int]
    crop: np.ndarray
    frames_seen: int


# ── Core pipeline ──────────────────────────────────────────────────────────────

class PlateRecognizer:
    """
    Main ALPR pipeline class. Thread-safe.

    The desktop app creates one instance at startup and calls process_frame()
    from its camera worker thread. All model state is internal.
    """

    def __init__(self, config: PlateRecognizerConfig = None):
        self._config = config or PlateRecognizerConfig()
        self._lock = threading.Lock()
        self._detector = None
        self._input_name = None
        self._ocr = None
        self._loaded = False
        self._plate_buffer: deque = deque(maxlen=self._config.frame_buffer_size)
        self._best_crop: Optional[np.ndarray] = None
        self._best_bbox: Optional[Tuple] = None
        self._best_ocr_conf: float = 0.0
        self._best_raw_text: str = ""

    # ── Public API ────────────────────────────────────────────────────────────

    def warmup(self) -> None:
        """
        Pre-load models and run a dummy inference to warm up the JIT.
        Call once at app startup, not at first frame.
        This prevents the 3-5 second delay on the first real vehicle.
        """
        self._ensure_loaded()
        dummy = np.zeros(
            (1, 3, self._config.detector_input_size, self._config.detector_input_size),
            dtype=np.float32
        )
        for _ in range(3):
            self._detector.run(None, {self._input_name: dummy})
        logger.info("PlateRecognizer warmed up and ready.")

    def process_frame(self, frame: np.ndarray) -> Optional[PipelineResult]:
        """
        Process one camera frame through the full pipeline.

        Args:
            frame: BGR numpy array from OpenCV (your camera worker's frame)

        Returns:
            PipelineResult if a plate has been committed with enough votes,
            None otherwise (keep calling on the next frame).

        The result is committed once MIN_VOTES_FOR_COMMIT consecutive frames
        agree on the same plate. After a result is returned, the buffer is
        cleared so the next vehicle gets a fresh start.
        """
        self._ensure_loaded()

        h, w = frame.shape[:2]

        # Step 1 — Preprocess
        tensor, scale_x, scale_y, pad_x, pad_y = self._preprocess(frame)

        # Step 2 — Detect plates
        try:
            outputs = self._detector.run(None, {self._input_name: tensor})
        except Exception as e:
            logger.error(f"Detector inference failed: {e}")
            return None

        boxes = self._decode_detections(outputs, w, h, scale_x, scale_y, pad_x, pad_y)

        # No plate in frame → return before the OCR stage. OCR is by far the most
        # expensive step, so this early exit is what keeps an idle gate cheap.
        if not boxes:
            return None

        # Step 3 — Pick best box (highest confidence)
        best_box = max(boxes, key=lambda b: b[4])
        x1, y1, x2, y2, det_conf = best_box

        # Step 4 — Crop and enhance
        crop = self._crop_with_margin(frame, x1, y1, x2, y2, w, h)
        if crop.size == 0:
            return None
        enhanced = self._enhance_crop(crop)

        # Step 5 — OCR
        raw_text, ocr_conf = self._run_ocr(enhanced)
        if not raw_text or ocr_conf < self._config.ocr_conf_threshold:
            return None

        # Step 6 — Normalize and validate
        normalized = self._normalize(raw_text)
        if not self._validate(normalized):
            return None

        # Step 7 — Temporal aggregation
        self._plate_buffer.append(normalized)
        if ocr_conf > self._best_ocr_conf:
            self._best_crop = crop.copy()
            self._best_bbox = (x1, y1, x2, y2)
            self._best_ocr_conf = ocr_conf
            self._best_raw_text = raw_text

        # Step 8 — Commit once the buffered reads agree, character by character
        if len(self._plate_buffer) >= self._config.min_votes_for_commit:
            consensus = self._consensus(list(self._plate_buffer))
            if consensus is not None:
                top_plate, agreement = consensus
                result = PipelineResult(
                    plate_number=top_plate,
                    confidence=round(agreement, 3),
                    raw_text=self._best_raw_text,
                    ocr_confidence=round(self._best_ocr_conf, 3),
                    bbox=self._best_bbox or (x1, y1, x2, y2),
                    crop=self._best_crop if self._best_crop is not None else crop,
                    frames_seen=len(self._plate_buffer),
                )
                self.reset()
                return result

        return None

    def _consensus(self, reads: list) -> Optional[Tuple[str, float]]:
        """Merge the buffered reads into one plate by per-position majority.

        Requiring N *identical* strings is what loses plates in the real world:
        OCR rarely fails a whole plate, it wobbles one glyph — a 5 read as an S
        in one frame out of six. Demanding exact agreement throws away five good
        frames because of one bad character, and the vehicle reads as unknown.

        Voting per character position instead keeps the five and outvotes the
        wobble, which is strictly more information than the old exact match used
        (identical reads still produce the same answer at agreement 1.0). Reads
        of different lengths are different readings, not disagreements about one
        glyph, so the most-supported length wins first and only reads of that
        length vote on characters.
        """
        if not reads:
            return None
        # Group by length first — "AA1234" and "AA12345" are not two opinions
        # about the same 7 characters.
        length_counter = Counter(len(read) for read in reads)
        best_length, _ = length_counter.most_common(1)[0]
        candidates = [read for read in reads if len(read) == best_length]
        if len(candidates) < self._config.min_votes_for_commit:
            return None

        chars = []
        agreements = []
        for position in range(best_length):
            column = Counter(read[position] for read in candidates)
            char, votes = column.most_common(1)[0]
            chars.append(char)
            agreements.append(votes / len(candidates))

        agreement = sum(agreements) / len(agreements)
        if agreement < self._config.min_char_agreement:
            # The buffer is genuinely chaotic rather than wobbling on one glyph;
            # committing here would invent a plate no frame actually saw.
            return None
        return "".join(chars), agreement

    def reset(self) -> None:
        """Clear the frame buffer. Call when a vehicle leaves the gate."""
        self._plate_buffer.clear()
        self._best_crop = None
        self._best_bbox = None
        self._best_ocr_conf = 0.0
        self._best_raw_text = ""

    def get_buffer_state(self) -> list:
        """Return current buffer contents (for UI display/debugging)."""
        return list(self._plate_buffer)

    # ── Internal methods ──────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Lazy-load models on first use. Thread-safe."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_detector()
            self._load_ocr()
            self._loaded = True

    def _load_detector(self) -> None:
        try:
            import onnxruntime as ort
        except ImportError:
            raise ImportError("onnxruntime is required: pip install onnxruntime")

        if not os.path.exists(self._config.detector_path):
            raise FileNotFoundError(
                f"Detector model not found: {self._config.detector_path}\n"
                f"Run the Colab notebook to produce detector.onnx, "
                f"then copy it to {self._config.detector_path}"
            )

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 4
        opts.log_severity_level = 3  # Suppress verbose ONNX logs

        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if self._config.use_gpu
            else ["CPUExecutionProvider"]
        )

        self._detector = ort.InferenceSession(
            self._config.detector_path,
            sess_options=opts,
            providers=providers,
        )
        self._input_name = self._detector.get_inputs()[0].name
        logger.info(
            f"Detector loaded: {self._config.detector_path} "
            f"[{self._detector.get_providers()[0]}]"
        )

    def _load_ocr(self) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            raise ImportError("paddleocr is required: pip install paddleocr")

        self._ocr = PaddleOCR(
            use_angle_cls=True,
            lang=self._config.ocr_lang,
            show_log=False,
            use_gpu=self._config.use_gpu,
            rec_char_type="EN",
            det_db_thresh=0.3,
            det_db_box_thresh=0.5,
        )
        logger.info("OCR loaded: PaddleOCR v4")

    def _preprocess(self, frame: np.ndarray):
        """Letterbox resize with CLAHE enhancement."""
        h_orig, w_orig = frame.shape[:2]
        size = self._config.detector_input_size

        # CLAHE enhancement
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(
            clipLimit=self._config.clahe_clip_limit,
            tileGridSize=self._config.clahe_tile_size
        )
        lab = cv2.merge([clahe.apply(l), a, b])
        enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        # Letterbox
        scale = min(size / w_orig, size / h_orig)
        new_w = int(w_orig * scale)
        new_h = int(h_orig * scale)
        resized = cv2.resize(enhanced, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        pad_x = (size - new_w) // 2
        pad_y = (size - new_h) // 2
        padded = cv2.copyMakeBorder(
            resized, pad_y, size - new_h - pad_y, pad_x, size - new_w - pad_x,
            cv2.BORDER_CONSTANT, value=(114, 114, 114)
        )

        tensor = padded[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        tensor = np.expand_dims(tensor, 0)
        return tensor, scale, scale, pad_x, pad_y

    def _decode_detections(self, outputs, orig_w, orig_h, sx, sy, px, py):
        """Decode YOLOv8 ONNX output format: (1, 5, 8400)."""
        preds = outputs[0][0]
        cx, cy, w, h, conf = preds[0], preds[1], preds[2], preds[3], preds[4]
        mask = conf > self._config.detector_conf_threshold
        if not mask.any():
            return []

        cx, cy, w, h, conf = cx[mask], cy[mask], w[mask], h[mask], conf[mask]
        x1 = np.clip((cx - w/2 - px) / sx, 0, orig_w).astype(int)
        y1 = np.clip((cy - h/2 - py) / sy, 0, orig_h).astype(int)
        x2 = np.clip((cx + w/2 - px) / sx, 0, orig_w).astype(int)
        y2 = np.clip((cy + h/2 - py) / sy, 0, orig_h).astype(int)

        # Greedy NMS
        kept = []
        for i in np.argsort(-conf):
            box = [int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i]), float(conf[i])]
            suppress = False
            for k in kept:
                ix1, iy1 = max(box[0], k[0]), max(box[1], k[1])
                ix2, iy2 = min(box[2], k[2]), min(box[3], k[3])
                inter = max(0, ix2-ix1) * max(0, iy2-iy1)
                a_a = (box[2]-box[0]) * (box[3]-box[1])
                a_b = (k[2]-k[0]) * (k[3]-k[1])
                union = a_a + a_b - inter
                if union > 0 and inter/union > self._config.detector_iou_threshold:
                    suppress = True
                    break
            if not suppress:
                kept.append(box)
        return kept

    def _crop_with_margin(self, frame, x1, y1, x2, y2, w, h):
        """Crop plate with a small padding margin."""
        mx = int((x2 - x1) * 0.06)
        my = int((y2 - y1) * 0.10)
        return frame[max(0, y1-my):min(h, y2+my), max(0, x1-mx):min(w, x2+mx)]

    def _enhance_crop(self, crop: np.ndarray) -> np.ndarray:
        """Resize and sharpen plate crop for OCR."""
        target_h = 64
        if crop.shape[0] < target_h:
            s = target_h / crop.shape[0]
            crop = cv2.resize(crop, (int(crop.shape[1]*s), target_h),
                              interpolation=cv2.INTER_CUBIC)
        elif crop.shape[0] > 120:
            s = 120 / crop.shape[0]
            crop = cv2.resize(crop, (int(crop.shape[1]*s), 120),
                              interpolation=cv2.INTER_AREA)
        # Sharpen
        kernel = np.array([[0, -0.5, 0], [-0.5, 3, -0.5], [0, -0.5, 0]])
        crop = cv2.filter2D(crop, -1, kernel)
        # CLAHE
        lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
        return cv2.cvtColor(cv2.merge([clahe.apply(l), a, b]), cv2.COLOR_LAB2BGR)

    def _run_ocr(self, crop: np.ndarray):
        """Run PaddleOCR and return (best_text, best_confidence)."""
        try:
            result = self._ocr.ocr(crop, cls=True)
            if not result or not result[0]:
                return None, 0.0
            best_text, best_conf = None, 0.0
            for det in result[0]:
                if det and len(det) == 2:
                    text, conf = det[1]
                    if conf > best_conf:
                        best_text, best_conf = text, conf
            return best_text, best_conf
        except Exception as e:
            logger.debug(f"OCR error: {e}")
            return None, 0.0

    def _normalize(self, raw: str) -> str:
        """Normalize raw OCR text to the app-wide canonical plate form.

        Delegates to ``smart_gate.utils.plates.normalize_plate`` so the pipeline
        emits exactly the form the allowlist cache and the server store.
        """
        return normalize_plate(raw)

    def _validate(self, text: str) -> bool:
        """Check plate text meets minimum quality bar."""
        return (
            self._config.min_plate_length
            <= len(text)
            <= self._config.max_plate_length
        )
