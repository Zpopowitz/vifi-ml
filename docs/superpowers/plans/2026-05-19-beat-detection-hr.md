<!-- /autoplan restore point: /home/zpopowitz1/.gstack/projects/Zpopowitz-vifi-ml/main-autoplan-restore-20260519-211208.md -->
# Beat-Detection HR Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ViFi's spectral-estimation HR pipeline (XGBoost on hand-crafted frequency features + per-session calibration, currently producing flat-line predictions with r=0.076 against ground truth on elevated-HR data) with beat-by-beat detection from CSI: matched-filter detection per subcarrier × stream, multi-stream voting, Kalman tracking. Per-beat F1 > 0.9 target; per-window HR MAE < 2 bpm at resting, < 3 bpm at elevated. HRV / apnea / arrhythmia as free downstream outputs.

**Architecture:** Three phases.
- **Phase 1 (Week 1):** Data fidelity upgrade (extract per-beat RR-intervals from H10 — currently discarded), single-stream beat detector spike, per-beat F1 eval against H10 ground truth, **go/no-go decision** based on F1.
- **Phase 2 (Week 2):** Multi-stream voting, Kalman HR tracker, HRV outputs.
- **Phase 3 (Week 3):** Production wiring — new eval harness, new model artifact format (no XGBoost), inference worker rewrite, dashboard, API contracts. Deprecate the XGBoost path.

**Tech Stack:** Python 3.12, numpy, scipy.signal, bleak (existing H10 BLE), pytest TDD. Reuses `preprocess.calibrate_cfo_sfo` (PhaseBeat phase calibration) and `multipath.subtract_top_components` (PCA subspace removal). No new external dependencies.

---

## Background

The current HR pipeline failed on yesterday's elevated-HR capture (`session_20260519T231733Z`):

| Metric | Value | Interpretation |
|---|---|---|
| Pearson r (true vs predicted HR) | **0.076** | Predictions contain essentially no HR information |
| Slope of best-fit line | 0.05 | Would be 1.0 if model tracked |
| MAE | 7.58 bpm | |
| Bias | −7.57 bpm | Every error in same direction |

Root causes (see `memory/project_hr_model_ceiling.md`):

1. **Variance-rank subcarrier selection** (`preprocess.py:369-373`) picks subcarriers responsive to whatever moves most — which is breathing (~5-15 mm), not heart (~0.5 mm).
2. **HR_BAND_HZ ceiling at 1.8 Hz = 108 bpm** (`config.py:55-58`). Today's HR was 89-103 with peaks at 105-108.
3. **Amplitude-only features** ignore phase, which has ~10-100× better sensitivity to small pulsatile motion.
4. **Per-session calibration** bakes in whatever HR the subject had in the first 30s as the "baseline" (`first_capture_report.py:341-368`).
5. **XGBoost regressor can't extrapolate** beyond training distribution. r=0.076 + slope=0.05 is exactly what saturation at the training mean looks like.

Surgical patches to the existing pipeline could improve things, but the architectural ceiling is fundamental:
- 10s FFT window gives 0.1 Hz resolution = 6 bpm minimum granularity (4× zero-pad → 1.5 bpm floor).
- XGBoost saturates outside training distribution.
- Per-session calibration is load-bearing on an assumption (resting baseline) that doesn't always hold.
- No path to ROADMAP features (apnea, arrhythmia, HRV) from spectral estimation.

## Decision: why beat detection

We're switching from "estimate average HR over a window via spectral peak finding" to "detect each cardiac event, then compute HR from inter-beat intervals."

Why:
- Heart rate isn't a band — it's a series of discrete events. Modeling it as events matches the physics.
- No band ceiling. HR can be 40 or 180 bpm with the same architecture.
- No per-session calibration. The detector doesn't learn a baseline.
- HRV, apnea, arrhythmia all come free (they're event-level statistics).
- The Polar H10 ground truth is per-beat (RR-intervals), giving us ~60× more supervision per session.
- Failure modes are interpretable: "missed beats during 90-95s" vs. opaque XGBoost output.

Why not deep learning end-to-end:
- 4 sessions × ~250 windows/session = ~1000 training examples. Modern CNN architectures have 10^5+ params. Massively underdetermined.
- Single subject. Deep models would learn person-specific multipath, fail to generalize.
- Physics is well-understood. Don't pay learning cost for what we can encode.
- Interpretability matters for FDA path (post-funding per ROADMAP).
- Reconsider when we have 50+ sessions across 5-10 subjects.

## Architecture overview

```
LAYER 1 — Data acquisition (UNCHANGED)
  ESP32-S3 ── CSI ────────── @ ~100 Hz × 128 subcarriers complex
  Polar H10 ── RR-intervals ─ @ ms-precision (USE THESE — currently discarded)
  Vernier GDX-RB ── force ──── @ 10 Hz (already shipped via PR #58)
     │
     ▼
LAYER 2 — Signal conditioning (REWORKED: existing primitives, new ranking)
  • Packet-rate resampling to uniform 100 Hz                [reuse]
  • PhaseBeat CFO/SFO calibration                           [reuse: calibrate_cfo_sfo]
  • Multipath PCA subspace removal (K=2)                    [reuse: multipath.subtract_top_components]
  • HR-band SNR scoring per subcarrier                      [NEW: replaces variance rank]
  • Select top-K subcarriers by HR-band SNR                 [NEW]
  • Output amplitude AND calibrated-phase streams           [NEW: 2× streams]
     │
     ▼
LAYER 3 — Beat detection (NEW)
  per subcarrier × {amplitude, phase}:
  • Bandpass [0.5, 4] Hz (broader than HR — captures pulse shape)
  • Cross-correlate against learned cardiac template
  • Local-maxima peak picking, min separation 300 ms
  • Output: list[(t_candidate, correlation_magnitude)]
     │
     ▼
LAYER 4 — Beat consolidation (NEW)
  • DBSCAN-style time-window clustering (±50 ms)
  • Cluster confidence = votes / total_streams
  • Output: list[(t_beat, confidence)]
     │
     ▼
LAYER 5 — Tracking (NEW)
  • State: (HR_t, dHR_t/dt)
  • Kalman filter, observation = 60 / IBI
  • Outlier rejection at observation level
  • Output: smooth HR estimate + uncertainty
     │
     ▼
LAYER 6 — Derived metrics (NEW, FREE)
  • HRV: SDNN, RMSSD, pNN50 from IBI series
  • Apnea: gap > N seconds since last detected beat
  • Arrhythmia: rolling stddev of IBI
     │
     ▼
LAYER 7 — Reporting & API (REWORKED)
  • Per-window HR MAE (legacy metric, comparability)
  • Per-beat MAE in ms (NEW, primary metric)
  • Bus emits beat events
  • Dashboard shows detected beats overlaid on CSI
```

## File structure

### New files

```
beat_detector/
  __init__.py
  signal_conditioning.py       # PhaseBeat + PCA + HR-band SNR ranking + top-K selection
  template.py                  # Learn and store the cardiac template (per subject)
  matched_filter.py            # Cross-correlation + local maxima beat candidate detection
  voting.py                    # Multi-subcarrier × multi-stream candidate consolidation
  tracking.py                  # Kalman filter for smooth HR + uncertainty
  hrv.py                       # SDNN, RMSSD, pNN50 from IBI series
  events.py                    # Apnea / arrhythmia detection from beat patterns
  pipeline.py                  # End-to-end orchestration
  session_loader.py            # Load complex CSI + H10 RR-intervals + metadata

tools/
  beat_eval.py                 # Beat-level eval harness vs H10 RR-intervals
  beat_train.py                # Template learning (replaces train.py for HR)
  beat_report.py               # Per-session/per-window report (replaces first_capture_report.py)

tests/
  test_beat_session_loader.py
  test_beat_signal_conditioning.py
  test_beat_template.py
  test_beat_matched_filter.py
  test_beat_voting.py
  test_beat_tracking.py
  test_beat_hrv.py
  test_beat_events.py
  test_beat_pipeline_e2e.py
  test_beat_eval.py
  test_hr_logger_rri.py        # RR-interval logging coverage
```

### Files to modify

- `hr_logger.py` — capture H10 RR-intervals (currently discarded); CSV schema v2 with sidecar
- `tools/analyze_session.py` — handle hr_log.csv v2 (RR-interval rows)
- `tools/run_paired_session.py` — switch auto-report to `beat_report.py` (Phase 3)
- `tools/inference_worker.py` — replace prediction loop with beat pipeline (Phase 3)

### Files to retire (Phase 3 end)

- `preprocess.py::extract_features` (the 9-dim path; keep primitives: `calibrate_cfo_sfo`, `bandpass_filter`, `_peak_freq_in_band`)
- `train.py` (XGBoost trainer)
- `calibration.py` (per-session calibration)
- `quality.py` (Mahalanobis OOD on features — replaced by confidence-based suppression)
- `tools/first_capture_report.py` (HR path; RR/non-HR portions kept if any)

---

## Phase 1: Data fidelity + beat detector spike

**Goal of Phase 1:** Determine empirically whether beat detection from our CSI captures is viable. Output: per-beat F1 score against H10 ground truth.

**Decision gate at end of Phase 1:**
- F1 > 0.9 on all 4 sessions → confident continue to Phase 2
- F1 > 0.7 on at least one session → continue, focus on remaining
- F1 < 0.5 across all sessions → halt, apply surgical patches as fallback, document

### Task 1.1: Extend hr_logger to capture RR-intervals from H10

**Why:** The Polar H10 BLE Heart Rate Service emits both HR and RR-intervals on every notification. `hr_logger.py` currently parses only HR. Without per-beat RR-intervals we have no beat-level ground truth.

**Files:**
- Modify: `hr_logger.py` (the BLE notification handler + CSV writer)
- Test: `tests/test_hr_logger_rri.py`

- [ ] **Step 1.1.1: Read the H10 BLE Heart Rate Measurement parsing in hr_logger.py**

```bash
grep -n "characteristic\|notification\|flags\|hr_value\|rr_value\|rr_interval" /home/zpopowitz1/vifi-ml/hr_logger.py
```

Look for the BLE notification handler that parses the HR measurement. The HR Measurement characteristic (0x2A37) has format:
- Byte 0: flags (bit 0 = HR value format, bit 4 = RR-interval present)
- Byte 1+: HR value (uint8 if flag bit 0 == 0, uint16 if == 1)
- Following bytes: RR-intervals (uint16, in 1/1024 second units), if flag bit 4 set, packed little-endian, multiple per notification

- [ ] **Step 1.1.2: Write failing test for RR-interval parsing**

Create `tests/test_hr_logger_rri.py`:
```python
"""Tests for hr_logger H10 RR-interval extraction (HR Measurement char 0x2A37)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hr_logger import parse_hr_measurement  # noqa: E402


def test_hr_only_no_rri_flag():
    """flags=0x00 (uint8 HR, no RRI). Single byte HR=72."""
    data = bytes([0x00, 72])
    hr, rri = parse_hr_measurement(data)
    assert hr == 72
    assert rri == []


def test_hr_uint16_no_rri():
    """flags=0x01 (uint16 HR, no RRI). HR=300 (high range)."""
    data = bytes([0x01, 0x2C, 0x01])  # little-endian 300
    hr, rri = parse_hr_measurement(data)
    assert hr == 300
    assert rri == []


def test_hr_uint8_with_rri():
    """flags=0x10 (uint8 HR, RRI present). One RR-interval = 800ms."""
    rri_units = int(round(0.800 * 1024))  # 819
    data = bytes([0x10, 72, rri_units & 0xFF, (rri_units >> 8) & 0xFF])
    hr, rri = parse_hr_measurement(data)
    assert hr == 72
    assert len(rri) == 1
    assert abs(rri[0] - 800.0) < 1.0  # within 1 ms


def test_hr_with_multiple_rri():
    """Two RR-intervals packed in one notification."""
    r1 = int(round(0.800 * 1024))
    r2 = int(round(0.820 * 1024))
    data = bytes([0x10, 72,
                  r1 & 0xFF, (r1 >> 8) & 0xFF,
                  r2 & 0xFF, (r2 >> 8) & 0xFF])
    hr, rri = parse_hr_measurement(data)
    assert hr == 72
    assert len(rri) == 2
    assert abs(rri[0] - 800.0) < 1.0
    assert abs(rri[1] - 820.0) < 1.0


def test_truncated_packet_safe():
    """Truncated RRI block doesn't crash; returns whatever's parseable."""
    data = bytes([0x10, 72, 0x00])  # only 1 byte after HR
    hr, rri = parse_hr_measurement(data)
    assert hr == 72
    assert rri == []
```

- [ ] **Step 1.1.3: Run to confirm fail (function doesn't exist yet)**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_hr_logger_rri.py -v
```

Expected: `ImportError: cannot import name 'parse_hr_measurement' from 'hr_logger'`

- [ ] **Step 1.1.4: Implement `parse_hr_measurement` in hr_logger.py**

Add at module scope in `hr_logger.py`:
```python
def parse_hr_measurement(data: bytes) -> tuple[int, list[float]]:
    """Parse the BLE Heart Rate Measurement characteristic (0x2A37).

    Returns (hr_bpm, rr_intervals_ms). RR-intervals are reported in
    1/1024-second units on the wire; we convert to milliseconds.
    Bit 0 of flags selects uint8 vs uint16 HR. Bit 4 indicates the
    presence of one or more RR-interval pairs after the HR field.
    Truncated packets are tolerated — we parse what we can.
    """
    if not data:
        return 0, []
    flags = data[0]
    hr_uint16 = bool(flags & 0x01)
    rri_present = bool(flags & 0x10)
    idx = 1
    if hr_uint16:
        if len(data) < idx + 2:
            return 0, []
        hr = int.from_bytes(data[idx:idx + 2], "little")
        idx += 2
    else:
        if len(data) < idx + 1:
            return 0, []
        hr = data[idx]
        idx += 1
    rri: list[float] = []
    if rri_present:
        while idx + 2 <= len(data):
            rr_units = int.from_bytes(data[idx:idx + 2], "little")
            rri.append(rr_units * 1000.0 / 1024.0)
            idx += 2
    return hr, rri
```

- [ ] **Step 1.1.5: Run to confirm pass**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_hr_logger_rri.py -v
```

Expected: 5 passed.

- [ ] **Step 1.1.6: Wire `parse_hr_measurement` into the BLE notification handler**

Find the existing notification handler in `hr_logger.py` (search for `def.*notification` or the bleak `start_notify` call). Replace the existing inline HR parsing with a call to `parse_hr_measurement`, and propagate the rri list.

- [ ] **Step 1.1.7: Extend CSV schema to a new v2 format**

New schema (parallel to `rr_logger` v2 pattern):
```
timestamp_unix, hr_bpm, rr_interval_ms
```
- One row per HR notification. Most notifications include 1-2 RR-intervals; emit one row per RR-interval (with the same hr_bpm value across rows in the same notification).
- If a notification has no RR-intervals (rare; older H10 firmware or RFU), emit one row with empty `rr_interval_ms`.

Also write sidecar `hr_log.csv.meta.json`:
```json
{
  "schema_version": 2,
  "device": "Polar H10",
  "ble_address": "...",
  "columns": ["timestamp_unix", "hr_bpm", "rr_interval_ms"],
  "started_at_utc": "...",
  "notes": "rr_interval_ms is the per-beat inter-beat interval in milliseconds. One row per beat; rows in the same notification share hr_bpm."
}
```

- [ ] **Step 1.1.8: Update existing hr_logger tests for new schema, run full suite**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_hr_logger_rri.py tests/test_hr_logger_bus.py -v
```

Expected: all pass. If older tests break on the schema change, update them — this is a deliberate format bump.

- [ ] **Step 1.1.9: Commit**

```bash
cd /home/zpopowitz1/vifi-ml && git checkout -b feat/beat-detection-hr && git add hr_logger.py tests/test_hr_logger_rri.py && git commit -m "feat(hr): capture per-beat RR-intervals from H10 (schema v2)

H10 BLE Heart Rate Measurement char 0x2A37 emits per-beat RR-intervals
in 1/1024-second units. Previously discarded; now parsed via
parse_hr_measurement and written to hr_log.csv v2 as one row per beat.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.2: Session data loader

**Why:** We need a single function that loads a session into a clean in-memory object: complex CSI matrix, packet timestamps, H10 beat timestamps, session metadata. Reused by all Phase 1 modules.

**Files:**
- Create: `beat_detector/__init__.py` (empty marker)
- Create: `beat_detector/session_loader.py`
- Test: `tests/test_beat_session_loader.py`

- [ ] **Step 1.2.1: Write failing test**

`tests/test_beat_session_loader.py`:
```python
"""Tests for beat_detector.session_loader."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.session_loader import (  # noqa: E402
    SessionData,
    load_session,
)


def _write_minimal_session(tmp: Path) -> Path:
    """Synthesize a minimal session directory for tests."""
    sd = tmp / "session_test"
    sd.mkdir()
    (sd / "session.json").write_text(json.dumps({
        "subject_id": "test",
        "room_id": "test",
        "posture": "seated",
        "post_cardio": False,
        "wifi_channel": 1,
    }))
    # Minimal capture.txt with 5 CSI rows (handled by parse_capture_file).
    # In a real test we'd shell out to data_gen; here we mock with the
    # smallest possible parser-compatible content.
    cap = sd / "capture.txt"
    cap.write_text(
        "CSI_DATA,0,,,,,,,,,1\n"  # placeholder; real loader uses parser
    )
    # H10 v2 hr_log.csv: 3 beats at known RR-intervals
    t0 = 1_700_000_000.0
    rows = [
        (t0, 60, 1000.0),       # 60 bpm, 1000 ms RRI
        (t0 + 1.0, 60, 1000.0),
        (t0 + 2.0, 75, 800.0),  # 75 bpm, 800 ms RRI
    ]
    pd.DataFrame(rows, columns=["timestamp_unix", "hr_bpm", "rr_interval_ms"]).to_csv(
        sd / "hr_log.csv", index=False
    )
    (sd / "hr_log.csv.meta.json").write_text(json.dumps({"schema_version": 2}))
    return sd


def test_session_data_has_required_fields(tmp_path, monkeypatch):
    """SessionData has csi, packet_ts, h10_beats, metadata."""
    sd = _write_minimal_session(tmp_path)

    # Stub parse_capture_file to return a tiny complex matrix
    import beat_detector.session_loader as mod
    def fake_parse(_path, **_kw):
        # 10 packets, 64 subcarriers, complex
        csi = (np.random.randn(10, 64) + 1j * np.random.randn(10, 64)).astype(np.complex64)
        ts = np.linspace(0, 0.1, 10)
        return csi, ts
    monkeypatch.setattr(mod, "_parse_capture_complex", fake_parse)

    s = load_session(sd)
    assert isinstance(s, SessionData)
    assert s.csi.shape == (10, 64)
    assert s.csi.dtype == np.complex64
    assert s.packet_ts.shape == (10,)
    # Beat timestamps derived from RR-intervals: cumulative ms from start
    # of first H10 row. 3 RR-intervals → 3 beat timestamps.
    assert s.h10_beats.shape == (3,)
    # Beat 0 at first H10 row timestamp; beat 1 = 1000 ms later; beat 2 = 800 ms later.
    assert abs(s.h10_beats[1] - s.h10_beats[0] - 1.000) < 1e-3
    assert abs(s.h10_beats[2] - s.h10_beats[1] - 0.800) < 1e-3
    assert s.metadata["subject_id"] == "test"
```

- [ ] **Step 1.2.2: Run to confirm fail**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_session_loader.py -v
```

Expected: ImportError on `beat_detector.session_loader`.

- [ ] **Step 1.2.3: Implement session loader**

`beat_detector/__init__.py`:
```python
"""ViFi beat detector: per-beat HR detection from WiFi CSI."""
```

`beat_detector/session_loader.py`:
```python
"""Load a paired-capture session into a SessionData object for beat detection.

Reads:
  - capture.txt           via tools.csi_capture parser (complex CSI + packet timestamps)
  - hr_log.csv (v2)       per-beat RR-intervals from H10
  - hr_log.csv.meta.json  schema version verification
  - session.json          subject/room/posture metadata

Returns a SessionData dataclass with:
  csi          : complex64 array, shape (N_packets, N_subcarriers)
  packet_ts    : float64 array, shape (N_packets,), seconds since first packet
  h10_beats    : float64 array of beat timestamps in seconds since first packet
  metadata     : dict from session.json
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class SessionData:
    csi: np.ndarray             # complex64 (N, K)
    packet_ts: np.ndarray       # float64 (N,) seconds since first packet
    h10_beats: np.ndarray       # float64 (M,) seconds since first packet
    metadata: dict[str, Any]


def _parse_capture_complex(path: Path):
    """Thin wrapper around the existing complex-CSI parser. Stubbed in tests."""
    from tools.csi_capture import parse_capture_file
    return parse_capture_file(path, return_complex=True)


def _h10_beats_from_rri(hr_df: pd.DataFrame, t0_packet: float) -> np.ndarray:
    """Derive beat timestamps (seconds since t0_packet) from RR-interval rows.

    Each row of hr_df has timestamp_unix (notification arrival time) and
    rr_interval_ms (the interval ending at that beat). The beat itself
    happened at `timestamp_unix - rr_interval_ms/1000`, but for our
    purposes we treat the notification timestamp as the beat time
    (sub-100ms BLE jitter is below our matching tolerance).
    """
    if hr_df.empty:
        return np.zeros(0, dtype=np.float64)
    valid = hr_df.dropna(subset=["rr_interval_ms"])
    if valid.empty:
        return np.zeros(0, dtype=np.float64)
    beats_unix = valid["timestamp_unix"].to_numpy(dtype=np.float64)
    return (beats_unix - t0_packet).astype(np.float64)


def load_session(session_dir: Path) -> SessionData:
    session_dir = Path(session_dir)
    metadata = json.loads((session_dir / "session.json").read_text())

    csi, packet_ts_abs = _parse_capture_complex(session_dir / "capture.txt")
    if packet_ts_abs is None or len(packet_ts_abs) == 0:
        raise ValueError(f"capture.txt at {session_dir} has no packet timestamps")
    packet_ts_abs = np.asarray(packet_ts_abs, dtype=np.float64)
    t0 = float(packet_ts_abs[0])
    packet_ts = packet_ts_abs - t0

    hr_path = session_dir / "hr_log.csv"
    if not hr_path.exists():
        raise FileNotFoundError(f"missing {hr_path}")
    hr_df = pd.read_csv(hr_path)
    if "rr_interval_ms" not in hr_df.columns:
        raise ValueError(
            f"hr_log.csv at {session_dir} is schema v1 (no rr_interval_ms). "
            f"Re-capture with the v2 logger to enable beat-level ground truth."
        )
    h10_beats = _h10_beats_from_rri(hr_df, t0)

    return SessionData(
        csi=csi.astype(np.complex64) if csi.dtype != np.complex64 else csi,
        packet_ts=packet_ts,
        h10_beats=h10_beats,
        metadata=metadata,
    )
```

- [ ] **Step 1.2.4: Run to confirm pass**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_session_loader.py -v
```

Expected: 1 passed.

- [ ] **Step 1.2.5: Commit**

```bash
cd /home/zpopowitz1/vifi-ml && git add beat_detector/ tests/test_beat_session_loader.py && git commit -m "feat(beat): session data loader (complex CSI + H10 beat timestamps + metadata)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.3: Signal conditioning — HR-band SNR ranking + top-K selection

**Why:** The current variance-rank subcarrier selection picks RR-dominated subcarriers. Replace with HR-band SNR ranking so the selected subcarriers actually carry HR signal.

**Files:**
- Create: `beat_detector/signal_conditioning.py`
- Test: `tests/test_beat_signal_conditioning.py`

- [ ] **Step 1.3.1: Write failing test**

`tests/test_beat_signal_conditioning.py`:
```python
"""Tests for beat_detector.signal_conditioning."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.signal_conditioning import (  # noqa: E402
    hr_band_snr,
    rank_subcarriers_by_hr_snr,
    condition_session,
)


def test_hr_band_snr_synthetic():
    """A pure 1.2 Hz signal (= 72 bpm) buried in white noise should
    have HR-band SNR > 1. Pure noise alone should have HR-band SNR ~ 1."""
    fs = 100.0
    duration = 10.0
    t = np.arange(0, duration, 1 / fs)
    rng = np.random.default_rng(42)
    pure_signal = np.sin(2 * np.pi * 1.2 * t) + 0.3 * rng.standard_normal(len(t))
    noise_only = rng.standard_normal(len(t))

    snr_signal = hr_band_snr(pure_signal, fs)
    snr_noise = hr_band_snr(noise_only, fs)

    assert snr_signal > 2.0, f"pure-signal SNR too low: {snr_signal}"
    assert snr_noise < 1.5, f"noise-only SNR too high: {snr_noise}"
    assert snr_signal > snr_noise * 1.5


def test_rank_subcarriers_picks_signal_carriers():
    """Given a (T, K) matrix where only subcarriers 5,10,20 carry HR signal
    and the rest are noise, those three should be among the top by HR SNR."""
    fs = 100.0
    duration = 10.0
    t = np.arange(0, duration, 1 / fs)
    rng = np.random.default_rng(0)
    K = 32
    sig_carriers = {5, 10, 20}
    x = rng.standard_normal((len(t), K)).astype(np.float32)
    for k in sig_carriers:
        x[:, k] += 2.0 * np.sin(2 * np.pi * 1.2 * t).astype(np.float32)

    top = rank_subcarriers_by_hr_snr(x, fs, k=4)
    # Three signal carriers should appear in the top-4.
    assert len(set(top) & sig_carriers) >= 2, (
        f"top-4 by HR SNR = {top}; expected ≥2 of {sig_carriers}"
    )


def test_condition_session_outputs_two_streams():
    """condition_session returns (amp_envelope, phase_envelope) at fs."""
    # Synthesize a tiny complex CSI: 10s @ 100 Hz × 16 subcarriers.
    fs = 100.0
    duration = 10.0
    n = int(duration * fs)
    t = np.arange(n) / fs
    rng = np.random.default_rng(7)
    csi = (rng.standard_normal((n, 16)) + 1j * rng.standard_normal((n, 16))).astype(np.complex64)

    amp_env, phase_env = condition_session(csi, packet_ts=t.astype(np.float64), fs_target=fs, top_k=4)

    assert amp_env.ndim == 1
    assert phase_env.ndim == 1
    assert len(amp_env) == len(phase_env)
    assert len(amp_env) <= n  # may be slightly shorter due to filter edge effects
```

- [ ] **Step 1.3.2: Run to confirm fail**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_signal_conditioning.py -v
```

Expected: ImportError on `beat_detector.signal_conditioning`.

- [ ] **Step 1.3.3: Implement signal conditioning**

`beat_detector/signal_conditioning.py`:
```python
"""Signal conditioning for beat detection.

Pipeline (per session):
  complex CSI (N, K)
    → packet-rate resample to uniform fs_target (default 100 Hz)
    → PhaseBeat CFO/SFO calibration (preprocess.calibrate_cfo_sfo)
    → multipath subspace projection (multipath.subtract_top_components, k=2)
    → amplitude envelope build (top-K by HR-band SNR)
    → calibrated-phase envelope build (top-K by HR-band SNR)
    → return (amplitude_envelope, phase_envelope) at fs_target
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt

# HR band for SNR scoring — broader than HR_BAND_HZ in config.py because
# we want to capture pulse-shape harmonics, not just the fundamental.
HR_SNR_BAND_HZ = (0.7, 4.0)
# Total band for "denominator" power.
TOTAL_BAND_HZ = (0.05, 5.0)


def _bandpass_filter(x: np.ndarray, fs: float, low: float, high: float, order: int = 4) -> np.ndarray:
    nyq = 0.5 * fs
    sos = butter(order, [low / nyq, high / nyq], btype="bandpass", output="sos")
    return sosfiltfilt(sos, x, axis=0).astype(np.float32)


def hr_band_snr(x: np.ndarray, fs: float) -> float:
    """Ratio of variance in the HR band to variance outside the HR band
    (within the broader total-of-interest band). Higher = more HR signal."""
    in_band = _bandpass_filter(x, fs, *HR_SNR_BAND_HZ)
    total = _bandpass_filter(x, fs, *TOTAL_BAND_HZ)
    out_band = total - in_band
    in_pwr = float(np.var(in_band)) + 1e-12
    out_pwr = float(np.var(out_band)) + 1e-12
    return in_pwr / out_pwr


def rank_subcarriers_by_hr_snr(x: np.ndarray, fs: float, k: int) -> list[int]:
    """Return indices of the top-k subcarriers by HR-band SNR.

    x: (T, K) signal, real-valued (e.g., amplitude or unwrapped phase).
    """
    if x.ndim != 2:
        raise ValueError(f"expected (T, K), got {x.shape}")
    snrs = np.array([hr_band_snr(x[:, i], fs) for i in range(x.shape[1])])
    return np.argsort(snrs)[-k:][::-1].tolist()


def _resample_to_uniform(csi: np.ndarray, packet_ts: np.ndarray, fs_target: float) -> np.ndarray:
    """Resample complex CSI from non-uniform packet_ts to uniform fs_target grid."""
    if len(packet_ts) < 2:
        raise ValueError("need ≥2 packets to resample")
    duration = float(packet_ts[-1] - packet_ts[0])
    n_target = int(round(duration * fs_target))
    if n_target < 8:
        raise ValueError(f"session too short to resample (n_target={n_target})")
    t_uniform = np.linspace(packet_ts[0], packet_ts[-1], n_target)
    real_r = np.empty((n_target, csi.shape[1]), dtype=np.float32)
    imag_r = np.empty_like(real_r)
    for i in range(csi.shape[1]):
        real_r[:, i] = np.interp(t_uniform, packet_ts, csi[:, i].real)
        imag_r[:, i] = np.interp(t_uniform, packet_ts, csi[:, i].imag)
    return (real_r + 1j * imag_r).astype(np.complex64)


def _amp_envelope_from_csi(csi_uniform: np.ndarray, fs: float, top_k: int) -> np.ndarray:
    """Build 1-D amplitude envelope from complex CSI."""
    amps = np.abs(csi_uniform).astype(np.float32)
    amps = amps - amps.mean(axis=0, keepdims=True)
    # Apply multipath subspace removal (k=2; existing utility).
    try:
        from multipath import subtract_top_components
        amps = subtract_top_components(amps, k=2)
    except ImportError:
        pass
    picks = rank_subcarriers_by_hr_snr(amps, fs, k=top_k)
    picked = amps[:, picks]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def _phase_envelope_from_csi(csi_uniform: np.ndarray, fs: float, top_k: int) -> np.ndarray:
    """Build 1-D calibrated-phase envelope.

    Uses preprocess.calibrate_cfo_sfo for PhaseBeat-style CFO/SFO removal,
    then differentiates wrapped phase across packets (chest motion shows up
    as a small per-sample rotation).
    """
    try:
        from preprocess import calibrate_cfo_sfo
        cal = calibrate_cfo_sfo(csi_uniform)
    except ImportError:
        cal = csi_uniform
    phase = np.angle(cal).astype(np.float64)
    phase_unwrapped = np.unwrap(phase, axis=0)
    phase_deriv = np.diff(phase_unwrapped, axis=0).astype(np.float32)
    phase_deriv = phase_deriv - phase_deriv.mean(axis=0, keepdims=True)
    picks = rank_subcarriers_by_hr_snr(phase_deriv, fs, k=top_k)
    picked = phase_deriv[:, picks]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def condition_session(
    csi: np.ndarray,
    packet_ts: np.ndarray,
    fs_target: float = 100.0,
    top_k: int = 8,
) -> tuple[np.ndarray, np.ndarray]:
    """Full conditioning: complex CSI + packet_ts → (amplitude, phase) envelopes
    at uniform fs_target rate.

    Returns:
      amplitude_envelope: float32 (N,)
      phase_envelope:     float32 (N-1,)   — one shorter due to diff
    """
    csi_uniform = _resample_to_uniform(csi, packet_ts, fs_target)
    amp_env = _amp_envelope_from_csi(csi_uniform, fs_target, top_k)
    phase_env = _phase_envelope_from_csi(csi_uniform, fs_target, top_k)
    return amp_env, phase_env
```

- [ ] **Step 1.3.4: Run to confirm pass**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_signal_conditioning.py -v
```

Expected: 3 passed.

- [ ] **Step 1.3.5: Commit**

```bash
cd /home/zpopowitz1/vifi-ml && git add beat_detector/signal_conditioning.py tests/test_beat_signal_conditioning.py && git commit -m "feat(beat): signal conditioning — HR-band SNR ranking + dual streams

Replaces variance-rank subcarrier selection (which picks RR-dominated
carriers) with HR-band SNR ranking. Outputs both amplitude and
calibrated-phase envelopes at uniform fs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.4: Cardiac template learner

**Why:** The matched filter needs a template. Learn it from H10-aligned beat segments in training data.

**Files:**
- Create: `beat_detector/template.py`
- Test: `tests/test_beat_template.py`

- [ ] **Step 1.4.1: Write failing test**

`tests/test_beat_template.py`:
```python
"""Tests for beat_detector.template — cardiac template learning."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.template import (  # noqa: E402
    extract_beat_segments,
    learn_template,
)


def test_extract_segments_around_beats():
    fs = 100.0
    duration = 10.0
    n = int(duration * fs)
    signal = np.random.default_rng(0).standard_normal(n).astype(np.float32)
    beats = np.array([1.0, 2.5, 4.0])  # seconds
    window_ms = 200.0  # ±100 ms around each beat
    segments = extract_beat_segments(signal, fs, beats, window_ms)
    expected_len = int(round(window_ms / 1000.0 * fs))  # 20 samples
    assert segments.shape == (3, expected_len)


def test_learn_template_outputs_unit_norm():
    """Template is unit-norm and centered on zero mean."""
    fs = 100.0
    n_segments = 50
    seg_len = 20
    rng = np.random.default_rng(0)
    # Synthesize segments: each is a Gaussian-modulated pulse + noise.
    t = np.linspace(-1, 1, seg_len)
    pulse = np.exp(-t**2 / 0.3**2) * np.cos(2 * np.pi * 1 * t)
    segments = np.array([pulse + 0.3 * rng.standard_normal(seg_len) for _ in range(n_segments)])

    template = learn_template(segments)
    assert template.shape == (seg_len,)
    assert abs(np.linalg.norm(template) - 1.0) < 1e-6, "template should be unit norm"
    assert abs(np.mean(template)) < 1e-6 + 1e-4  # roughly zero-mean


def test_template_resembles_underlying_pulse():
    """When segments share an underlying pulse shape, the learned
    template should correlate strongly with that shape."""
    fs = 100.0
    seg_len = 20
    t = np.linspace(-1, 1, seg_len)
    pulse = np.exp(-t**2 / 0.3**2) * np.cos(2 * np.pi * 1 * t)
    rng = np.random.default_rng(0)
    segments = np.array([pulse + 0.5 * rng.standard_normal(seg_len) for _ in range(100)])

    template = learn_template(segments)
    pulse_norm = pulse / np.linalg.norm(pulse)
    corr = abs(np.dot(template, pulse_norm))
    assert corr > 0.85, f"template should match pulse shape; corr={corr}"
```

- [ ] **Step 1.4.2: Run to confirm fail**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_template.py -v
```

Expected: ImportError.

- [ ] **Step 1.4.3: Implement template learner**

`beat_detector/template.py`:
```python
"""Cardiac template learning for beat detection.

A template is a unit-norm, zero-mean 1-D waveform representing the
average cardiac pulse shape as it appears in conditioned CSI. Learned
once per subject from H10-aligned segments of training data.
"""
from __future__ import annotations

import numpy as np


def extract_beat_segments(
    signal: np.ndarray,
    fs: float,
    beat_times_s: np.ndarray,
    window_ms: float = 200.0,
) -> np.ndarray:
    """Extract a fixed-length window centered on each beat.

    signal: 1-D conditioned envelope at fs Hz
    beat_times_s: beat timestamps in seconds from signal start
    window_ms: total window length (split equally before/after the beat)

    Returns: (n_beats, window_samples) array. Beats whose window falls
    outside the signal range are skipped.
    """
    win_samples = int(round(window_ms / 1000.0 * fs))
    half = win_samples // 2
    n = len(signal)
    out = []
    for t in beat_times_s:
        idx = int(round(t * fs))
        lo = idx - half
        hi = idx - half + win_samples
        if lo < 0 or hi > n:
            continue
        out.append(signal[lo:hi])
    if not out:
        return np.zeros((0, win_samples), dtype=np.float32)
    return np.stack(out).astype(np.float32)


def learn_template(segments: np.ndarray) -> np.ndarray:
    """Compute a unit-norm zero-mean template from beat segments.

    Uses robust averaging (median) to suppress outliers from missed
    beats / motion artifacts. The result is zero-meaned then normalized
    to unit L2 norm.
    """
    if segments.ndim != 2 or segments.shape[0] == 0:
        raise ValueError(f"need (n_beats, len) with n_beats>0, got {segments.shape}")
    template = np.median(segments, axis=0).astype(np.float32)
    template = template - template.mean()
    norm = np.linalg.norm(template)
    if norm < 1e-12:
        raise ValueError("learned template is degenerate (zero norm)")
    return template / norm
```

- [ ] **Step 1.4.4: Run to confirm pass**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_template.py -v
```

Expected: 3 passed.

- [ ] **Step 1.4.5: Commit**

```bash
cd /home/zpopowitz1/vifi-ml && git add beat_detector/template.py tests/test_beat_template.py && git commit -m "feat(beat): cardiac template learning from H10-aligned segments

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.5: Matched-filter beat candidate detector

**Files:**
- Create: `beat_detector/matched_filter.py`
- Test: `tests/test_beat_matched_filter.py`

- [ ] **Step 1.5.1: Write failing test**

`tests/test_beat_matched_filter.py`:
```python
"""Tests for beat_detector.matched_filter."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.matched_filter import detect_beat_candidates  # noqa: E402


def test_detects_synthetic_beats():
    """Plant 10 beats at known times in a clean signal; detector should find them."""
    fs = 100.0
    duration = 15.0
    n = int(duration * fs)
    t = np.arange(n) / fs
    seg_len = 20
    pulse_t = np.linspace(-1, 1, seg_len)
    pulse = (np.exp(-pulse_t**2 / 0.3**2) * np.cos(2 * np.pi * 1 * pulse_t)).astype(np.float32)
    pulse = pulse - pulse.mean()
    pulse = pulse / np.linalg.norm(pulse)

    signal = np.zeros(n, dtype=np.float32)
    beat_times = np.arange(1.0, 14.0, 1.0)  # 13 beats at 1s spacing
    rng = np.random.default_rng(0)
    for bt in beat_times:
        idx = int(round(bt * fs))
        lo = idx - seg_len // 2
        hi = lo + seg_len
        if 0 <= lo and hi <= n:
            signal[lo:hi] += pulse * 5.0
    signal += 0.5 * rng.standard_normal(n).astype(np.float32)

    candidates = detect_beat_candidates(signal, fs, pulse, min_sep_ms=400)
    detected_times = np.array([c[0] for c in candidates])
    # All 13 beats should be detected within ±50 ms.
    matched = sum(any(abs(detected_times - bt) < 0.05) for bt in beat_times)
    assert matched >= 12, f"only matched {matched}/13"


def test_respects_min_separation():
    """Two correlations 100 ms apart with min_sep_ms=400 must collapse."""
    fs = 100.0
    n = 1000
    template = np.array([0, 1, 0], dtype=np.float32) / np.sqrt(1)
    signal = np.zeros(n, dtype=np.float32)
    signal[100] = 1.0
    signal[110] = 0.9  # 100 ms after, would also peak
    candidates = detect_beat_candidates(signal, fs, template, min_sep_ms=400)
    assert len(candidates) == 1
```

- [ ] **Step 1.5.2: Run to confirm fail**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_matched_filter.py -v
```

- [ ] **Step 1.5.3: Implement matched-filter detector**

`beat_detector/matched_filter.py`:
```python
"""Matched-filter beat candidate detection.

Cross-correlate a learned cardiac template against a conditioned signal,
find local maxima of the correlation with a minimum-separation constraint
(physiological — no two beats closer than ~300 ms for the highest plausible
HR of 200 bpm).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import correlate, find_peaks


def detect_beat_candidates(
    signal: np.ndarray,
    fs: float,
    template: np.ndarray,
    min_sep_ms: float = 300.0,
    correlation_threshold: float = 0.0,
) -> list[tuple[float, float]]:
    """Find beat candidates by matched filtering.

    signal: 1-D conditioned envelope at fs Hz
    template: unit-norm, zero-mean cardiac template
    min_sep_ms: minimum separation between detected beats (default 300 ms = 200 bpm max)
    correlation_threshold: minimum correlation magnitude to accept

    Returns: list of (t_seconds, correlation_magnitude) tuples, sorted by time.
    """
    if signal.ndim != 1:
        raise ValueError(f"expected 1-D signal, got {signal.shape}")
    if template.ndim != 1:
        raise ValueError(f"expected 1-D template, got {template.shape}")
    # Center signal (template is already zero-mean).
    s = signal - signal.mean()
    # Normalize signal for unit-template-vs-signal correlation interpretation.
    s_norm = s / (np.std(s) + 1e-12)
    corr = correlate(s_norm, template, mode="same")
    # find_peaks with minimum distance = min_sep_ms / 1000 * fs samples.
    min_dist = max(1, int(round(min_sep_ms / 1000.0 * fs)))
    peaks, _ = find_peaks(corr, distance=min_dist, height=correlation_threshold)
    return [(float(p) / fs, float(corr[p])) for p in peaks]
```

- [ ] **Step 1.5.4: Run to confirm pass**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_matched_filter.py -v
```

- [ ] **Step 1.5.5: Commit**

```bash
cd /home/zpopowitz1/vifi-ml && git add beat_detector/matched_filter.py tests/test_beat_matched_filter.py && git commit -m "feat(beat): matched-filter beat candidate detection

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.6: Beat-level eval (single-stream baseline)

**Why:** Before going to multi-stream voting, get a baseline F1 on a single-stream (amplitude) detector. This is the smallest viable detector and the strictest test of whether the signal even exists.

**Files:**
- Create: `beat_detector/eval.py`
- Test: `tests/test_beat_eval.py`

- [ ] **Step 1.6.1: Write failing test**

`tests/test_beat_eval.py`:
```python
"""Tests for beat_detector.eval — per-beat precision/recall/F1."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.eval import match_beats, beat_metrics  # noqa: E402


def test_perfect_match():
    detected = [1.0, 2.0, 3.0]
    truth = [1.0, 2.0, 3.0]
    matches = match_beats(detected, truth, tol_ms=100)
    assert matches == [(0, 0), (1, 1), (2, 2)]


def test_one_extra_detection_is_false_positive():
    detected = [1.0, 1.5, 2.0]  # 1.5 has no match
    truth = [1.0, 2.0]
    matches = match_beats(detected, truth, tol_ms=100)
    # Both truth beats matched, 1.5 unmatched.
    assert len(matches) == 2


def test_missing_detection_is_false_negative():
    detected = [1.0]
    truth = [1.0, 2.0]
    matches = match_beats(detected, truth, tol_ms=100)
    assert len(matches) == 1


def test_metrics_perfect():
    m = beat_metrics(detected=[1.0, 2.0, 3.0], truth=[1.0, 2.0, 3.0], tol_ms=100)
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["f1"] == 1.0
    assert m["n_tp"] == 3
    assert m["n_fp"] == 0
    assert m["n_fn"] == 0


def test_metrics_mixed():
    m = beat_metrics(
        detected=[1.0, 1.5, 2.0],  # 3 detections: 1.0 and 2.0 match, 1.5 is FP
        truth=[1.0, 2.0, 3.0],     # 3 truth: 3.0 missed
        tol_ms=100,
    )
    assert m["n_tp"] == 2
    assert m["n_fp"] == 1
    assert m["n_fn"] == 1
    assert m["precision"] == 2/3
    assert m["recall"] == 2/3
    assert abs(m["f1"] - 2/3) < 1e-9
```

- [ ] **Step 1.6.2: Run to confirm fail**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_eval.py -v
```

- [ ] **Step 1.6.3: Implement eval**

`beat_detector/eval.py`:
```python
"""Beat-level evaluation against H10 ground truth."""
from __future__ import annotations

from typing import Iterable


def match_beats(
    detected: Iterable[float],
    truth: Iterable[float],
    tol_ms: float = 100.0,
) -> list[tuple[int, int]]:
    """Greedy bipartite match: each truth beat matched to its nearest
    unmatched detected beat within ±tol_ms.

    Returns list of (detected_idx, truth_idx) pairs.
    """
    detected_list = list(detected)
    truth_list = list(truth)
    tol_s = tol_ms / 1000.0
    matched = []
    used_det = set()
    for ti, t_t in enumerate(truth_list):
        best_di = None
        best_dt = tol_s + 1
        for di, t_d in enumerate(detected_list):
            if di in used_det:
                continue
            dt = abs(t_d - t_t)
            if dt < best_dt:
                best_dt = dt
                best_di = di
        if best_di is not None and best_dt <= tol_s:
            matched.append((best_di, ti))
            used_det.add(best_di)
    return matched


def beat_metrics(
    detected: Iterable[float],
    truth: Iterable[float],
    tol_ms: float = 100.0,
) -> dict[str, float | int]:
    detected_list = list(detected)
    truth_list = list(truth)
    matches = match_beats(detected_list, truth_list, tol_ms=tol_ms)
    tp = len(matches)
    fp = len(detected_list) - tp
    fn = len(truth_list) - tp
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {
        "n_detected": len(detected_list),
        "n_truth": len(truth_list),
        "n_tp": tp,
        "n_fp": fp,
        "n_fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }
```

- [ ] **Step 1.6.4: Run to confirm pass**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/pytest tests/test_beat_eval.py -v
```

- [ ] **Step 1.6.5: Commit**

```bash
cd /home/zpopowitz1/vifi-ml && git add beat_detector/eval.py tests/test_beat_eval.py && git commit -m "feat(beat): per-beat precision/recall/F1 vs ground truth

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.7: Phase 1 verdict — run on a real session

**Why:** This is the **decision gate**. Take a real session, run conditioning → template learning (from first half) → matched-filter detection (on second half) → eval against H10. Get the F1.

**Files:**
- Create: `tools/beat_phase1_eval.py` (one-off script)

- [ ] **Step 1.7.1: Capture a fresh resting-HR session with v2 hr_logger**

**Manual hardware step.** Subject strapped in H10 + Vernier belt + ESP32-S3 captures running, ~10 minutes at resting HR (after settling). The session must be captured AFTER Task 1.1 is merged (so RR-intervals are in `hr_log.csv`).

Once captured, rsync from Pi:
```bash
PI_IP=$(powershell.exe -NoProfile -Command "(Test-Connection -ComputerName vifi-pi-room1.local -Count 1 -ErrorAction Stop).IPV4Address.IPAddressToString" 2>/dev/null | tr -d '\r\n')
rsync -av "zpopowitz@$PI_IP:/home/zpopowitz/vifi-ml/data/captures/founder/$(ssh -o HostName=$PI_IP pi 'ls -t /home/zpopowitz/vifi-ml/data/captures/founder | head -1')/" /home/zpopowitz1/vifi-ml/data/captures/founder/
```

- [ ] **Step 1.7.2: Write the Phase 1 verdict script**

`tools/beat_phase1_eval.py`:
```python
"""Phase 1 verdict: single-stream beat detection F1 on a real session.

Splits the session in half:
  - First half: learn template from H10-aligned beat segments
  - Second half: detect beats with matched filter, eval vs H10

Outputs F1 and writes a diagnostic plot showing detected vs ground-truth beats.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.session_loader import load_session
from beat_detector.signal_conditioning import condition_session
from beat_detector.template import extract_beat_segments, learn_template
from beat_detector.matched_filter import detect_beat_candidates
from beat_detector.eval import beat_metrics


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session_dir", type=Path)
    p.add_argument("--fs", type=float, default=100.0)
    p.add_argument("--top-k", type=int, default=8)
    p.add_argument("--stream", choices=["amplitude", "phase"], default="amplitude")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    sd = load_session(args.session_dir)
    print(f"loaded session {args.session_dir.name}")
    print(f"  CSI: {sd.csi.shape}, span {sd.packet_ts[-1]:.1f}s")
    print(f"  H10 beats: {len(sd.h10_beats)}")

    amp_env, phase_env = condition_session(sd.csi, sd.packet_ts, fs_target=args.fs, top_k=args.top_k)
    env = amp_env if args.stream == "amplitude" else phase_env
    fs = args.fs

    # 50/50 split by beat count
    mid_t = float(sd.h10_beats[len(sd.h10_beats) // 2])
    print(f"  split at t={mid_t:.1f}s")

    # Train template from first half
    train_beats = sd.h10_beats[sd.h10_beats < mid_t]
    train_env = env[: int(mid_t * fs)]
    train_segments = extract_beat_segments(train_env, fs, train_beats, window_ms=200)
    print(f"  template learned from {len(train_segments)} beats")
    template = learn_template(train_segments)

    # Detect on second half
    test_beats_truth = sd.h10_beats[sd.h10_beats >= mid_t] - mid_t  # rebase to second-half start
    test_env = env[int(mid_t * fs):]
    candidates = detect_beat_candidates(test_env, fs, template, min_sep_ms=300)
    detected_times = [c[0] for c in candidates]
    metrics = beat_metrics(detected_times, list(test_beats_truth), tol_ms=100)

    print()
    print(f"PHASE 1 VERDICT [{args.stream} stream]:")
    print(f"  precision = {metrics['precision']:.3f}")
    print(f"  recall    = {metrics['recall']:.3f}")
    print(f"  F1        = {metrics['f1']:.3f}  ({metrics['n_tp']} TP, "
          f"{metrics['n_fp']} FP, {metrics['n_fn']} FN)")

    # Diagnostic plot
    out = args.out or args.session_dir / f"beat_phase1_{args.stream}.png"
    fig, ax = plt.subplots(figsize=(14, 4))
    t_axis = np.arange(len(test_env)) / fs
    ax.plot(t_axis, test_env, color="#1D5C6E", linewidth=0.7, label="conditioned signal")
    for tt in test_beats_truth:
        ax.axvline(tt, color="#0E9F66", alpha=0.3, linewidth=1.0)
    for td in detected_times:
        ax.axvline(td, color="#C44", alpha=0.5, linewidth=1.0, linestyle="--")
    ax.set_xlabel("t (s) in second half")
    ax.set_ylabel("conditioned envelope")
    ax.set_title(f"{args.session_dir.name} [{args.stream}]  F1={metrics['f1']:.3f}")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print(f"  plot: {out}")

    sys.exit(0 if metrics["f1"] >= 0.7 else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 1.7.3: Run Phase 1 verdict on the fresh session**

```bash
cd /home/zpopowitz1/vifi-ml && ./.venv/bin/python tools/beat_phase1_eval.py data/captures/founder/<latest_session> --stream amplitude
./.venv/bin/python tools/beat_phase1_eval.py data/captures/founder/<latest_session> --stream phase
```

- [ ] **Step 1.7.4: Apply decision criteria**

Record results in `docs/superpowers/plans/2026-05-19-beat-detection-hr.md` (this file) at the "Phase 1 Verdict" section below.

Decision:
- **F1 ≥ 0.9** on either stream → confident continue to Phase 2
- **F1 ≥ 0.7** on either stream → continue to Phase 2 with caveats; focus on improving the weaker stream
- **F1 < 0.7** on both streams → halt overhaul; switch to surgical patches (HR-aware ranking + widen HR_BAND + v2 features) as fallback; revisit beat detection when we have more sessions

- [ ] **Step 1.7.5: Commit verdict**

```bash
cd /home/zpopowitz1/vifi-ml && git add tools/beat_phase1_eval.py docs/superpowers/plans/2026-05-19-beat-detection-hr.md && git commit -m "phase1: beat detector verdict — F1=<X> on <stream> stream

Continue to Phase 2: <yes/no/conditional>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Phase 1 Verdict (filled in at execution time):**

> _(to be filled in by Task 1.7.4)_
>
> Session: `___`
> Amplitude-stream F1: `___`
> Phase-stream F1: `___`
> Decision: `___`

---

## Phase 2: Multi-stream voting + Kalman tracking + HRV

**Prerequisite:** Phase 1 verdict ≥ 0.7. If lower, halt and apply surgical-patch fallback.

### Task 2.1: Multi-stream beat consolidation

**Files:**
- Create: `beat_detector/voting.py`
- Test: `tests/test_beat_voting.py`

- [ ] **Step 2.1.1: Write failing test**

`tests/test_beat_voting.py`:
```python
"""Tests for beat_detector.voting — multi-stream beat candidate consolidation."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.voting import consolidate_candidates  # noqa: E402


def test_single_stream_passthrough():
    """One stream's candidates pass through with confidence proportional to votes/total."""
    streams = [[(1.0, 0.9), (2.0, 0.8)]]
    beats = consolidate_candidates(streams, cluster_window_ms=50, min_votes=1)
    assert len(beats) == 2
    assert abs(beats[0][0] - 1.0) < 0.001
    assert beats[0][1] == 1.0  # all 1/1 streams voted


def test_multi_stream_agreement_high_confidence():
    """Three streams agree on a beat → confidence = 3/3."""
    streams = [
        [(1.0, 0.9)],
        [(1.02, 0.8)],   # 20 ms later
        [(0.98, 0.85)],  # 20 ms earlier
    ]
    beats = consolidate_candidates(streams, cluster_window_ms=50, min_votes=2)
    assert len(beats) == 1
    assert abs(beats[0][0] - 1.0) < 0.05
    assert beats[0][1] == 1.0


def test_outlier_stream_filtered():
    """One stream sees a phantom beat that two others don't → rejected if min_votes=2."""
    streams = [
        [(1.0, 0.9), (5.0, 0.5)],  # 5.0 is the phantom
        [(1.01, 0.85)],
        [(0.99, 0.92)],
    ]
    beats = consolidate_candidates(streams, cluster_window_ms=50, min_votes=2)
    # Only the 1.0-beat cluster has ≥2 votes.
    assert len(beats) == 1
    assert abs(beats[0][0] - 1.0) < 0.05
```

- [ ] **Step 2.1.2: Implement consolidation**

`beat_detector/voting.py`:
```python
"""Multi-stream beat candidate consolidation.

Each "stream" (subcarrier × {amp, phase}) emits a list of (t, mag) candidates.
This module consolidates them into a single per-beat estimate by clustering
candidates within a small time window and requiring a minimum vote count
(robustness to single-stream false positives).
"""
from __future__ import annotations


def consolidate_candidates(
    streams: list[list[tuple[float, float]]],
    cluster_window_ms: float = 50.0,
    min_votes: int = 2,
) -> list[tuple[float, float]]:
    """Consolidate beat candidates across streams.

    streams: list of per-stream candidate lists, each [(t_seconds, magnitude), ...]
    cluster_window_ms: ±half-width for grouping candidates as the same beat
    min_votes: minimum streams that must contribute a candidate to a cluster

    Returns: list of (t_beat, confidence) tuples, sorted by time.
             confidence = (votes received) / (number of streams)
    """
    if not streams:
        return []
    flat = [(t, mag, sid) for sid, s in enumerate(streams) for (t, mag) in s]
    flat.sort()
    win_s = cluster_window_ms / 1000.0
    n_streams = len(streams)

    clusters: list[list[tuple[float, float, int]]] = []
    current: list[tuple[float, float, int]] = []
    for (t, mag, sid) in flat:
        if current and t - current[-1][0] <= win_s:
            current.append((t, mag, sid))
        else:
            if current:
                clusters.append(current)
            current = [(t, mag, sid)]
    if current:
        clusters.append(current)

    out: list[tuple[float, float]] = []
    for c in clusters:
        sids = set(s for (_, _, s) in c)
        if len(sids) < min_votes:
            continue
        weighted_t = sum(t * mag for (t, mag, _) in c) / sum(max(mag, 1e-12) for (_, mag, _) in c)
        confidence = len(sids) / n_streams
        out.append((weighted_t, confidence))
    out.sort()
    return out
```

- [ ] **Step 2.1.3: Run + commit**

```bash
./.venv/bin/pytest tests/test_beat_voting.py -v
git add beat_detector/voting.py tests/test_beat_voting.py && git commit -m "feat(beat): multi-stream candidate consolidation with vote-count confidence"
```

---

### Task 2.2: Kalman HR tracker

**Files:**
- Create: `beat_detector/tracking.py`
- Test: `tests/test_beat_tracking.py`

- [ ] **Step 2.2.1: Write failing test**

`tests/test_beat_tracking.py`:
```python
"""Tests for beat_detector.tracking — Kalman HR estimator."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.tracking import HRTracker  # noqa: E402


def test_steady_state_converges():
    """Constant 75 bpm IBI input — tracker should converge to 75 bpm."""
    tracker = HRTracker(initial_hr=70.0)
    t = 0.0
    ibi = 0.8  # 75 bpm = 800 ms
    for _ in range(20):
        t += ibi
        hr, std = tracker.update(t, ibi)
    assert abs(hr - 75.0) < 1.0
    assert std < 5.0  # uncertainty has shrunk


def test_outlier_rejected():
    """An impossible IBI (50 bpm jump in one beat) should be rejected."""
    tracker = HRTracker(initial_hr=75.0)
    # Run steady-state to converge
    t = 0.0
    for _ in range(20):
        t += 0.8
        tracker.update(t, 0.8)
    # Inject outlier: IBI of 0.3s (200 bpm)
    t += 0.3
    hr_before = tracker.state[0]
    accepted = tracker.is_observation_valid(0.3)
    assert not accepted
    # State unchanged when we skip the update
    if not accepted:
        pass  # caller decides to skip update
```

- [ ] **Step 2.2.2: Implement tracker**

`beat_detector/tracking.py`:
```python
"""Scalar-state Kalman filter for HR tracking.

State: [HR, dHR/dt]
Observation: 60 / IBI for each new beat
Process model: HR drifts slowly; rate-of-change drifts even more slowly.
"""
from __future__ import annotations

import numpy as np


class HRTracker:
    """Track HR over time from beat events using a 2-state Kalman filter."""

    def __init__(
        self,
        initial_hr: float = 70.0,
        initial_uncertainty_bpm: float = 30.0,
        process_var_hr: float = 1.0,         # bpm^2/s — slow drift allowed
        process_var_dhr: float = 0.1,        # (bpm/s)^2/s — rate-of-change drift
        measurement_var_bpm: float = 4.0,    # bpm^2 — per-beat measurement noise
        min_plausible_hr: float = 30.0,
        max_plausible_hr: float = 200.0,
    ):
        self.state = np.array([initial_hr, 0.0])
        self.P = np.diag([initial_uncertainty_bpm ** 2, 10.0])
        self.q_hr = process_var_hr
        self.q_dhr = process_var_dhr
        self.R = measurement_var_bpm
        self.last_t: float | None = None
        self.min_hr = min_plausible_hr
        self.max_hr = max_plausible_hr

    def is_observation_valid(self, ibi_s: float) -> bool:
        if ibi_s <= 0:
            return False
        hr_obs = 60.0 / ibi_s
        if hr_obs < self.min_hr or hr_obs > self.max_hr:
            return False
        # Reject obs more than 3 sigma from current state estimate
        sigma = np.sqrt(self.P[0, 0] + self.R)
        if abs(hr_obs - self.state[0]) > 3 * sigma + 10:  # +10 bpm soft margin
            return False
        return True

    def update(self, t_beat: float, ibi_s: float) -> tuple[float, float]:
        if not self.is_observation_valid(ibi_s):
            return float(self.state[0]), float(np.sqrt(self.P[0, 0]))
        if self.last_t is None:
            dt = 0.0
        else:
            dt = float(t_beat - self.last_t)
        F = np.array([[1.0, dt], [0.0, 1.0]])
        Q = np.array([[self.q_hr * dt, 0.0], [0.0, self.q_dhr * dt]])
        self.state = F @ self.state
        self.P = F @ self.P @ F.T + Q

        hr_obs = 60.0 / ibi_s
        H = np.array([[1.0, 0.0]])
        y = hr_obs - (H @ self.state)[0]
        S = (H @ self.P @ H.T)[0, 0] + self.R
        K = (self.P @ H.T) / S
        self.state = self.state + (K.flatten() * y)
        self.P = self.P - K @ H @ self.P
        self.last_t = t_beat
        return float(self.state[0]), float(np.sqrt(self.P[0, 0]))
```

- [ ] **Step 2.2.3: Run + commit**

```bash
./.venv/bin/pytest tests/test_beat_tracking.py -v
git add beat_detector/tracking.py tests/test_beat_tracking.py && git commit -m "feat(beat): Kalman HR tracker with observation outlier rejection"
```

---

### Task 2.3: HRV outputs

**Files:**
- Create: `beat_detector/hrv.py`
- Test: `tests/test_beat_hrv.py`

- [ ] **Step 2.3.1: Write failing test**

`tests/test_beat_hrv.py`:
```python
"""Tests for HRV metrics."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.hrv import compute_hrv  # noqa: E402


def test_constant_ibi_zero_variability():
    """Identical IBIs → SDNN = 0, RMSSD = 0, pNN50 = 0."""
    ibis_ms = [800.0] * 30
    hrv = compute_hrv(ibis_ms)
    assert hrv["sdnn_ms"] == 0.0
    assert hrv["rmssd_ms"] == 0.0
    assert hrv["pnn50"] == 0.0


def test_alternating_ibi_high_rmssd():
    """Alternating IBI 800 / 850 ms → RMSSD = 50 ms, pNN50 = 1.0."""
    ibis_ms = [800.0, 850.0] * 15
    hrv = compute_hrv(ibis_ms)
    assert abs(hrv["rmssd_ms"] - 50.0) < 1.0
    assert hrv["pnn50"] == 1.0


def test_typical_resting_hrv():
    """Random IBIs around 800 ms with realistic variation → physiological range."""
    rng = np.random.default_rng(0)
    ibis = 800.0 + rng.standard_normal(60) * 30.0
    hrv = compute_hrv(ibis.tolist())
    assert 20.0 < hrv["sdnn_ms"] < 50.0
```

- [ ] **Step 2.3.2: Implement HRV**

`beat_detector/hrv.py`:
```python
"""Heart Rate Variability metrics from inter-beat intervals.

Implements:
  SDNN   - standard deviation of NN (normal-to-normal) intervals
  RMSSD  - root mean square of successive differences
  pNN50  - fraction of successive differences > 50 ms

All inputs in milliseconds; all outputs in milliseconds (or unitless fraction).
"""
from __future__ import annotations

import numpy as np


def compute_hrv(ibis_ms: list[float]) -> dict[str, float]:
    if len(ibis_ms) < 2:
        return {"sdnn_ms": 0.0, "rmssd_ms": 0.0, "pnn50": 0.0, "n": len(ibis_ms)}
    arr = np.asarray(ibis_ms, dtype=np.float64)
    diffs = np.diff(arr)
    return {
        "n": len(arr),
        "sdnn_ms": float(np.std(arr, ddof=0)),
        "rmssd_ms": float(np.sqrt(np.mean(diffs ** 2))),
        "pnn50": float(np.mean(np.abs(diffs) > 50.0)),
    }
```

- [ ] **Step 2.3.3: Run + commit**

```bash
./.venv/bin/pytest tests/test_beat_hrv.py -v
git add beat_detector/hrv.py tests/test_beat_hrv.py && git commit -m "feat(beat): HRV metrics (SDNN, RMSSD, pNN50)"
```

---

### Task 2.4: End-to-end pipeline + integration test

**Files:**
- Create: `beat_detector/pipeline.py`
- Test: `tests/test_beat_pipeline_e2e.py`

- [ ] **Step 2.4.1: Write integration test (synthetic session)**

`tests/test_beat_pipeline_e2e.py`:
```python
"""End-to-end synthetic session test for the full beat pipeline."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beat_detector.pipeline import BeatPipeline  # noqa: E402


def test_synthetic_session_recovers_75_bpm():
    """Plant 75-bpm beats in synthetic CSI; pipeline should recover HR ≈ 75."""
    fs = 100.0
    duration = 60.0
    n = int(duration * fs)
    n_sub = 16
    rng = np.random.default_rng(0)

    # Background noise
    csi = (rng.standard_normal((n, n_sub)) + 1j * rng.standard_normal((n, n_sub))).astype(np.complex64)
    # Inject 75-bpm beats in subcarriers 3,7,11 (random selection)
    seg_len = 20
    t_pulse = np.linspace(-1, 1, seg_len)
    pulse = (np.exp(-t_pulse ** 2 / 0.3 ** 2) * np.cos(2 * np.pi * 1 * t_pulse)).astype(np.complex64)
    beat_period_s = 60.0 / 75.0
    truth_beats = []
    t = 1.0
    while t < duration - 1.0:
        idx = int(t * fs)
        for k in (3, 7, 11):
            lo = idx - seg_len // 2
            hi = lo + seg_len
            if 0 <= lo and hi <= n:
                csi[lo:hi, k] += pulse * 3.0
        truth_beats.append(t)
        t += beat_period_s

    packet_ts = np.arange(n) / fs

    pipeline = BeatPipeline(fs=fs, top_k=4)
    # Provide truth beats for template learning (in production, from H10)
    pipeline.train_template(csi, packet_ts, np.array(truth_beats[:len(truth_beats) // 2]))

    detected = pipeline.detect(csi, packet_ts)
    times = [t for (t, _) in detected]

    # Should recover ~75 detected beats; HR estimate near 75
    from beat_detector.eval import beat_metrics
    m = beat_metrics(times, truth_beats, tol_ms=100)
    assert m["f1"] > 0.85, f"synthetic F1 = {m['f1']}"

    # Median IBI ≈ 800 ms
    if len(times) > 1:
        ibis = np.diff(times)
        median_hr = 60.0 / np.median(ibis)
        assert abs(median_hr - 75.0) < 3.0
```

- [ ] **Step 2.4.2: Implement pipeline**

`beat_detector/pipeline.py`:
```python
"""End-to-end beat detection pipeline.

Stages:
  train_template(csi, packet_ts, truth_beats) — learn per-stream templates
  detect(csi, packet_ts) → list[(t_beat, confidence)]
  track(beats) → list[(t_beat, hr_bpm, hr_std)]
"""
from __future__ import annotations

import numpy as np

from beat_detector.signal_conditioning import condition_session
from beat_detector.template import extract_beat_segments, learn_template
from beat_detector.matched_filter import detect_beat_candidates
from beat_detector.voting import consolidate_candidates
from beat_detector.tracking import HRTracker


class BeatPipeline:
    def __init__(self, fs: float = 100.0, top_k: int = 8):
        self.fs = fs
        self.top_k = top_k
        self.template_amp: np.ndarray | None = None
        self.template_phase: np.ndarray | None = None

    def _condition(self, csi: np.ndarray, packet_ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return condition_session(csi, packet_ts, fs_target=self.fs, top_k=self.top_k)

    def train_template(self, csi: np.ndarray, packet_ts: np.ndarray, truth_beats: np.ndarray):
        amp_env, phase_env = self._condition(csi, packet_ts)
        # Phase env is one shorter due to diff — align beat times to amp's frame
        amp_segs = extract_beat_segments(amp_env, self.fs, truth_beats, window_ms=200)
        phase_segs = extract_beat_segments(phase_env, self.fs, truth_beats, window_ms=200)
        if len(amp_segs):
            self.template_amp = learn_template(amp_segs)
        if len(phase_segs):
            self.template_phase = learn_template(phase_segs)

    def detect(self, csi: np.ndarray, packet_ts: np.ndarray) -> list[tuple[float, float]]:
        amp_env, phase_env = self._condition(csi, packet_ts)
        streams: list[list[tuple[float, float]]] = []
        if self.template_amp is not None:
            streams.append(detect_beat_candidates(amp_env, self.fs, self.template_amp))
        if self.template_phase is not None:
            streams.append(detect_beat_candidates(phase_env, self.fs, self.template_phase))
        if not streams:
            raise RuntimeError("no templates trained; call train_template first")
        return consolidate_candidates(streams, cluster_window_ms=50, min_votes=1)

    def track(self, beats: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
        """Run Kalman tracker over detected beats. Returns (t, hr_bpm, hr_std)."""
        tracker = HRTracker()
        out: list[tuple[float, float, float]] = []
        last_t: float | None = None
        for (t, _conf) in beats:
            if last_t is None:
                last_t = t
                continue
            ibi = t - last_t
            hr, std = tracker.update(t, ibi)
            out.append((t, hr, std))
            last_t = t
        return out
```

- [ ] **Step 2.4.3: Run + commit**

```bash
./.venv/bin/pytest tests/test_beat_pipeline_e2e.py -v
git add beat_detector/pipeline.py tests/test_beat_pipeline_e2e.py && git commit -m "feat(beat): end-to-end pipeline (condition → detect → vote → track)"
```

---

### Task 2.5: Real-session Phase 2 verdict

- [ ] **Step 2.5.1: Update `tools/beat_phase1_eval.py` to use full pipeline**

Switch from single-stream baseline to the multi-stream BeatPipeline. Add HRV reporting.

- [ ] **Step 2.5.2: Run + record verdict in this plan**

```bash
./.venv/bin/python tools/beat_phase1_eval.py data/captures/founder/<latest_session> --pipeline full
```

Record Phase 2 metrics here:
- Per-beat F1
- Per-window HR MAE
- HRV-SDNN vs H10's RR-interval-derived SDNN
- (compare to Phase 1 single-stream numbers)

---

## Phase 3: Production wiring

### Task 3.1: New eval harness CLI

**Files:**
- Create: `tools/beat_report.py` (replaces HR portion of `tools/first_capture_report.py`)

- [ ] Step 3.1.1: Replicate the legacy report's per-window MAE alongside the new per-beat metrics, so we can publish both side-by-side during transition.
- [ ] Step 3.1.2: Write to `report.json` schema v2 with: `summary.per_beat`, `summary.per_window`, `summary.hrv`, `windows[]`.
- [ ] Step 3.1.3: Update `tools/run_paired_session.py` auto-report invocation to call beat_report.py.
- [ ] Step 3.1.4: Verify on a real session; commit.

---

### Task 3.2: Model artifact format

**Files:**
- Create: `models/v2_beat/template_amp.npy`, `template_phase.npy`, `metadata.json` (after first real training)

`metadata.json`:
```json
{
  "feature_set_version": "v2_beat",
  "subject_id": "founder",
  "training_sessions": ["session_..."],
  "fs": 100.0,
  "top_k": 8,
  "pca_k": 2,
  "template_window_ms": 200.0,
  "trained_at_utc": "...",
  "git_commit": "..."
}
```

- [ ] Step 3.2.1: Implement `tools/beat_train.py` — loads multiple sessions, learns templates, writes model artifact.
- [ ] Step 3.2.2: Add inference-side metadata verification (template dim matches expectations, fs matches).
- [ ] Step 3.2.3: Document training procedure in README.md (replace existing HR train docs).

---

### Task 3.3: Inference worker rewrite

**Files:**
- Modify: `tools/inference_worker.py` (replace prediction logic with beat pipeline)
- Test: `tests/test_inference_worker.py` (extend coverage for beat path)

- [ ] Step 3.3.1: Add `BeatInferenceMode` class that:
    - Loads beat template from model artifact at startup
    - On each CSI window, runs `BeatPipeline.detect()` + tracker update
    - Emits beat events + HR estimate to bus
- [ ] Step 3.3.2: Switch the worker's default mode to beat mode (feature flag `VIFI_INFERENCE_MODE=beat|legacy` for safety).
- [ ] Step 3.3.3: Update bus schema to include beat events: `bus_publish({topic: hr.beat.<patient_id>, payload: {t_beat, confidence}})`.
- [ ] Step 3.3.4: Test with synthetic input; commit.

---

### Task 3.4: Dashboard updates

**Files:**
- Modify: `dashboard/` (HTML/CSS/JS — see existing structure)

- [ ] Step 3.4.1: Subscribe to new `hr.beat.<patient_id>` topic; draw beat tick marks on the time axis.
- [ ] Step 3.4.2: Add HRV widget (SDNN, RMSSD).
- [ ] Step 3.4.3: Add per-beat confidence histogram.
- [ ] Step 3.4.4: Visual QA via the existing browse/dashboard skill flow.

---

### Task 3.5: API contract updates

**Files:**
- Modify: `api.py` (the `/predict/csi`, `/predict/capture`, `/api/v1/stream` endpoints)

- [ ] Step 3.5.1: Extend response schema:
    ```json
    {
      "hr_bpm": 75.2,
      "hr_std_bpm": 1.8,
      "beats_per_window": [...],
      "hrv": {"sdnn_ms": 42.3, "rmssd_ms": 38.1, "pnn50": 0.12},
      "confidence": 0.92
    }
    ```
- [ ] Step 3.5.2: Maintain backwards-compatible `hr_bpm` top-level field.
- [ ] Step 3.5.3: Update OpenAPI/Pydantic models.
- [ ] Step 3.5.4: Update API tests.

---

### Task 3.6: Retire the XGBoost path

**Files (delete or refactor):**
- Modify: `preprocess.py` — remove `extract_features` (keep `calibrate_cfo_sfo`, `bandpass_filter`)
- Delete: `train.py`
- Delete: `calibration.py`
- Delete: `quality.py`
- Modify: `tools/first_capture_report.py` — remove HR prediction path
- Update: `README.md`, `docs/QUICKSTART.md`, `docs/STATUS.md` to reflect new architecture

- [ ] Step 3.6.1: Audit all imports of deleted modules; update or remove call sites.
- [ ] Step 3.6.2: Delete model artifacts in `models/` for legacy XGBoost; document in `models/.gitignore` what stays.
- [ ] Step 3.6.3: Run full test suite; remove tests that exercised deleted code.
- [ ] Step 3.6.4: Update memory: write new `project_hr_pipeline_v2.md` describing beat architecture; mark `project_hr_model_ceiling` as superseded.
- [ ] Step 3.6.5: Final commit + PR with full migration notes.

---

### Task 3.7: End-to-end production verification

- [ ] Step 3.7.1: Hardware capture: 5-min resting, 5-min post-cardio.
- [ ] Step 3.7.2: Run the full new pipeline end-to-end on both captures.
- [ ] Step 3.7.3: Compare to legacy XGBoost baseline numbers (resting only — XGBoost can't do post-cardio).
- [ ] Step 3.7.4: Verify dashboard, API, audit log all work end-to-end.
- [ ] Step 3.7.5: Update README headline (currently "4.15 bpm cross-session HR MAE…") with new beat-pipeline numbers.

---

## Test strategy

| Layer | Test type | Synthetic? | Real data? |
|---|---|---|---|
| signal_conditioning | unit | yes | no |
| template | unit | yes | no |
| matched_filter | unit | yes | no |
| voting | unit | yes | no |
| tracking | unit (Kalman convergence) | yes | no |
| hrv | unit | yes | no |
| eval | unit | yes | no |
| pipeline (E2E) | integration | yes | optional |
| Phase 1 verdict | integration | no | required |
| Phase 2 verdict | integration | no | required |
| inference_worker | unit + bus integration | yes | no |
| API contracts | unit + e2e | yes | no |
| Production verification | end-to-end | no | required |

All unit tests live in `tests/`, run with `./.venv/bin/pytest -m "not e2e"` in CI. Integration tests gated on real-data presence skip cleanly when data isn't available.

---

## Risk register

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| 1 | Beat-level SNR insufficient in our bedroom geometry | Medium | Phase 1 verdict gate; surgical-patch fallback |
| 2 | Template doesn't generalize across postures | Medium | Per-subject + per-posture templates; fall back to subject-specific |
| 3 | Multipath dominates pulsatile signal even after PCA | Low–Medium | PCA k tunable; rolling-PCA stub exists; can ablate |
| 4 | Compute too heavy for Pi real-time | Low | Matched filter via scipy is FFT-based; profile early |
| 5 | Phase 3 production wiring breaks dashboard/API contracts | Medium | Feature flag legacy ↔ beat; gradual rollout |
| 6 | Backwards-compat with v1 hr_log.csv reads | Low | Add legacy reader to `analyze_session.py` analogous to rr v1 reader |
| 7 | Single-subject template won't transfer to subject 2 | High (when subject 2 arrives) | Out of scope for this plan; addressed when subject 2 captures begin |

---

## Rollback plan

**If Phase 1 fails (per-beat F1 < 0.5 across all sessions):**
- Halt overhaul. Apply surgical-patch fallback:
  1. HR-aware subcarrier ranking (preprocess.py change)
  2. Widen HR_BAND_HZ to (0.7, 3.0)
  3. HR-only bandpass for amplitude features
  4. Retrain XGBoost with the new feature distribution
- Document why beat detection was infeasible in this geometry.
- Keep `beat_detector/` modules for future revisit when conditions change (new room, more subjects, etc).

**If Phase 2 fails (per-window HR MAE worse than legacy 4.15):**
- Investigate: is it beat detection or tracking? Use the per-beat F1 from Phase 1 as the diagnostic.
- If beat detection is fine but tracking is wrong, tune Kalman parameters (process noise vs measurement noise).
- If beat detection is wrong, return to Phase 1 design.

**If Phase 3 wiring breaks production:**
- Feature flag flips back to legacy XGBoost path immediately.
- Beat pipeline kept available as offline analysis tool.
- Investigate + fix; re-attempt.

---

## Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-19 | Beat detection over spectral estimation | r=0.076 + flat-line on elevated HR; architectural ceiling of FFT-window approach |
| 2026-05-19 | Matched filter over deep learning | 4 sessions ≠ enough data for end-to-end DL; physics is well-understood |
| 2026-05-19 | Per-subject template initially | Multipath geometry varies; single subject right now anyway |
| 2026-05-19 | Kalman tracker over particle filter | Linear-Gaussian model fits HR dynamics well; particle filter overkill at this scope |
| 2026-05-19 | Drop per-session calibration | Load-bearing on "first 30s = baseline resting" assumption that broke yesterday |
| 2026-05-19 | Keep complex CSI capture format unchanged | Future-proof for retraining + future DL approaches |

---

## GSTACK REVIEW REPORT

**Pipeline:** /autoplan — 4 phases (CEO, Design, Eng, DX). Codex unavailable (not installed); each phase ran single-voice via an independent Claude review subagent. UI scope detected (dashboard task); DX scope detected (new module + CLI + API).

### Consensus tables

```
CEO REVIEW
  Dimension                             Verdict
  ───────────────────────────────────── ─────────────────────────────
  Premises valid?                       PARTIAL — SNR premise understated
  Right problem to solve?               YES — events > spectral is sound
  Scope calibration correct?            NO — Phase 1 builds before gate
  Alternatives explored?                YES — patches/DL rejections sound
  6-month trajectory sound?             PARTIAL — HRV/apnea oversold
  Verdict: WITH CHANGES — not as written

ENG REVIEW
  Dimension                             Verdict
  ───────────────────────────────────── ─────────────────────────────
  Architecture sound?                   YES — module decomposition good
  Algorithm correctness?                NO — 3 HIGH bugs (B1/B2/B3)
  Test coverage sufficient?             NO — degenerate paths untested
  Blocking dependencies honest?         NO — parse_capture_file bug
  Edge cases handled?                   NO — no nil/empty/degenerate path
  Verdict: NOT READY — fix A1/B1/B2/B3/B6 first

DESIGN REVIEW
  Dimension                             Verdict
  ───────────────────────────────────── ─────────────────────────────
  Information hierarchy serves user?    NO — HR no longer clear hero
  Interaction states specified?         NO — warmup/low-conf/signal-lost
  Confidence histogram appropriate?     NO — engineer artifact, not clinician
  Design-system aligned?                NO — DESIGN.md ignored
  Verdict: fix histogram + states before implementing 3.4

DX REVIEW
  Dimension                             Verdict
  ───────────────────────────────────── ─────────────────────────────
  Plan executable as artifact?          PARTIAL — Phase 3 is a sketch
  Module/API ergonomics?                MOSTLY — one off-by-one shape bug
  CLI consistency?                      NO — beat_eval vs beat_phase1_eval
  Error handling actionable?            PARTIAL — empty-RRI path opaque
  Docs/migration scoped?                NO — doc consumers undercounted
  Verdict: fix items 1/7/10 before end-to-end execution
```

### Cross-phase themes (flagged independently by 2+ reviewers)

- **THEME 1 — `parse_capture_file` dependency is wrong.** Eng (A1) + DX (item 2). VERIFIED against source: the function is in `tools/parse_csi_capture.py` (not `tools/csi_capture.py`) and with `return_complex=True` returns a 3-tuple `(amps, complex_csi, timestamps_s)`, not a 2-tuple. The plan's `session_loader` and its test stub both encode the wrong contract. Critical — breaks Phase 1 immediately.
- **THEME 2 — Phase 3 is under-specified.** DX (item 1) + CEO (defer Phase 3). Phase 3 tasks are one-line sketches vs Phase 1's full TDD detail; an executing agent would stall or guess.
- **THEME 3 — over-deletion of unrelated shipped code.** CEO + Eng. `calibration.py` also holds `RollingFingerprintTracker` (shipped multi-subject walk-in detector); `quality.py` is the Mahalanobis OOD detector. Neither is the XGBoost HR regressor. Task 3.6 as written destroys roadmap features.

### Decision Audit Trail

| # | Phase | Decision | Class | Principle | Rationale |
|---|-------|----------|-------|-----------|-----------|
| 1 | Eng | Fix `parse_capture_file`: import from `tools.parse_csi_capture`, unpack 3-tuple `(amps, csi, packet_ts)`; fix test stub to match real arity | Mechanical | P5 explicit | Verified against source — plan as written crashes |
| 2 | Eng | Fix `_h10_beats_from_rri`: reconstruct beat times by cumulatively summing RR-intervals, not reusing the shared notification timestamp | Mechanical | P5 explicit | H10 batches 1-2 RRIs/notification → duplicate beat timestamps as written |
| 3 | Eng | Fix `HRTracker` process noise: proper constant-velocity Q (dt³/3, dt²/2, dt structure) instead of `diag([q*dt, q*dt])` | Mechanical | P5 explicit | Wrong Q makes filter overconfident after missed beats |
| 4 | Eng | Fix `HRTracker` outlier gate: test against the predicted prior covariance, not the shrunk posterior; raise `q_hr` to admit physiological HR ramps | Mechanical | P1 completeness | As written, gate rejects genuine post-cardio ramps → recreates the flat-line failure the plan exists to fix |
| 5 | Eng | Fix matched-filter normalization: sliding/local normalization (or rely on peak shape), not global-std; calibrate `correlation_magnitude` before using it as a voting weight | Mechanical | P5 explicit | Global-std normalization makes confidence non-comparable across the session |
| 6 | Eng | Add degenerate-path tests: empty session, all-NaN CSI, zero/one beat, first-beat dt=0, single-IBI HRV; make the hollow `test_outlier_rejected` real; add the post-cardio-ramp test | Mechanical | P1 completeness | Highest-risk behavior (ramp tracking) currently untested |
| 7 | CEO | Add Task 1.0: measure CSI↔H10 clock offset via breathing-signal cross-correlation before building anything | Mechanical | P2 boil-lakes | The −7.57 bias is also the signature of a timestamp-alignment bug; rule it out cheaply or the gate proves the wrong thing |
| 8 | CEO | Task 1.7 go/no-go must run on elevated-HR (post-cardio) data — ideally 2 sessions — not resting | Mechanical | P1 completeness | The failure was on elevated HR; gating on the easy resting case proves nothing |
| 9 | CEO+Eng | Task 3.6 deletes ONLY the XGBoost HR regressor + per-session HR calibration. Keep `RollingFingerprintTracker` in `calibration.py` and `quality.py`'s OOD detector intact | Mechanical | P4 DRY | Those serve unrelated shipped roadmap features |
| 10 | DX | Merge `beat_phase1_eval.py` into a single `tools/beat_eval.py` with a `--phase {1,2}` flag | Mechanical | P4 DRY, P5 explicit | Dual harness names confuse; `beat_eval.py` had no implementing task |
| 11 | DX | Fix `condition_session` shape: pad phase envelope to equal length (prepend one zero) so both streams share one sample grid | Mechanical | P5 explicit | Off-by-one between amp/phase streams will bite window code |
| 12 | DX | Expand Task 3.6 doc migration: enumerate every consumer of deleted modules + `hr_log.csv` v1 schema (README, QUICKSTART, STATUS, ESP32_SETUP, RESULTS, ROADMAP, CLAUDE.md, retrain_on_real, first_capture_report, cross_subject_eval, audit_subscriber) | Mechanical | P1 completeness | Doc/consumer scope materially undercounted |
| 13 | Design | Phase 3 dashboard: drop the per-beat confidence histogram from the operator view; use a single trust indicator on the HR card. Specify warmup / low-confidence / signal-lost states. Use DESIGN.md tokens (`--signal` for beat ticks) | Mechanical | P5 explicit | Histogram is an engineer artifact; signal-lost is the most clinically important unspecified state |
| T1 | CEO | Phase 1 restructure — spike-first | **Taste → RESOLVED** | P3 pragmatic | User delegated the call. ADOPTED: Phase 1 = hr_logger RR upgrade + hardware capture + ONE throwaway spike script + verdict. The 6-module tree is built in Phase 2 only if the gate passes. Spike is 1 file vs 16; a failed gate deletes 1 file. |
| T2 | CEO+DX | Phase 3 — defer to re-plan after Phase 2 verdict | **Taste → RESOLVED** | P6 bias-to-action | User delegated the call. ADOPTED: Phase 3 becomes a re-plan checkpoint. Production-wiring detail depends on what Phase 1+2 reveal; speculative detail now would be rewritten or discarded. Design findings (decision #13) carried to that re-plan. |

**Autoplan verdict:** APPROVED — all 13 mechanical fixes + 2 taste decisions resolved. Core architectural bet (beat detection over spectral estimation) endorsed by CEO ("physically sound") and Eng ("promising architecture"). Single-voice review (codex unavailable).

### Premises surfaced (for user confirmation)

1. **Beat-level SNR is sufficient on ESP32-S3 hardware.** Risk register says "Medium" — the CEO review argues "High": per-beat detection from ~0.5 mm chest motion has been demonstrated in the literature on $500 MIMO NICs (PhaseBeat, Intel 5300), not on $25 single-antenna ESP32-S3 boards. This is the load-bearing bet.
2. **ESP32-S3 phase is recoverable after CFO/SFO calibration.** ViFi has never shipped a phase feature. The plan treats `calibrate_cfo_sfo` as proven; it has no real-data validation.
3. **The r=0.076 failure is architectural, not a CSI↔H10 timestamp-alignment bug.** Decision #7 (Task 1.0) de-risks this rather than assuming it.


---

## REVISED EXECUTION STRUCTURE (autoplan-approved — supersedes the phase breakdown above)

The Phase 1 / 2 / 3 task detail above remains the reference for *code content* (with the
13 audit-trail fixes applied at execution time). The *sequencing* is restructured:

### Phase 1 — Spike + Verdict (prove it before building)

1. **Task 1.1 — hr_logger RR-interval capture** (REAL code, committed, not gated).
   Per the detailed Task 1.1 above. Needed to capture beat-level ground truth at all,
   and independently valuable as better HR truth for any approach. **This is the one
   piece executable now with no hardware.**
2. **Task 1.2 — Hardware capture.** Elevated-HR (post-cardio) session(s) with the v2
   logger — ideally 2. Resting session too, as a regression reference. (Audit #8.)
   Requires the user strapped in; deferred to a session when that's possible.
3. **Task 1.3 — The spike.** ONE throwaway script `tools/beat_spike.py`:
   timestamp-alignment check via breathing cross-correlation (audit #7) → load →
   condition → learn template → matched-filter detect → F1 vs H10. Not refactored,
   not modularized. Runs on the elevated-HR capture.
4. **Task 1.4 — Verdict.** Apply the F1 go/no-go criteria. Record in this file.
   Pass → Phase 2. Fail (<0.5) → surgical-patch fallback.

### Phase 2 — Build the module tree (only if the Phase 1 gate passes)

Refactor the proven spike into `beat_detector/` with TDD: session_loader,
signal_conditioning, template, matched_filter, voting, tracking, hrv, pipeline,
evaluation. This absorbs the detailed task code from old Phase 1 (1.2–1.6) and old
Phase 2 (2.1–2.5) — **with all 13 audit-trail fixes applied** (the `parse_capture_file`
3-tuple, the Kalman Q + prior-covariance gate, matched-filter normalization, the
cumulative-sum beat timestamps, degenerate-path tests, etc.).

### Phase 3 — Re-plan checkpoint (deferred)

Not detailed now. Once Phase 2 produces a detector beating 4.15 bpm on real LOSO,
re-plan Phase 3 (eval harness, model artifact, inference worker, dashboard, API,
XGBoost retirement) with full TDD detail. Audit #9 (delete only the XGBoost HR path,
keep `RollingFingerprintTracker` + OOD), #12 (full doc-migration consumer list), and
#13 (dashboard: trust indicator not histogram, specify states, DESIGN.md tokens)
are carried into that re-plan.
