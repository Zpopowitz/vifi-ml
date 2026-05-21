# RR artifact rejection — design + validation

**Date:** 2026-05-21
**Status:** implemented, validated, and wired into the inference worker + dashboard
**Branch:** `feat/rr-artifact-rejection`

## Problem

ViFi's RR pipeline was built end to end (`preprocess.extract_features` ->
`rr_peak_hz`, `models/rr_model.json`, `inference_worker` -> `rr.predicted`,
dashboard RR card) but had never been scored against ground truth. Validation
with `tools/eval_rr.py` against a Vernier respiration belt
(`session_20260520T014522Z`, 5 min, raw force) put RR error at **6-8 brpm**,
where the clinical tolerance is 2.

## Root cause

The estimator takes the largest spectral peak in the respiration band. On real
captures that peak is frequently a low-frequency body-sway artifact (~0.16 Hz,
~9-10 brpm), not the breath. The sway is a 1/f-style motion whose energy piles
up at the band floor, so the peak-picker pins to it. The breathing signal is
genuinely present (one 60 s window matched the belt to 0.1 brpm) but is
intermittently outweighed by the sway.

Characterization (`/tmp` probes, this session): per-subcarrier selection does
not help (most subcarriers are sway-contaminated). PCA decomposition does: the
breath and the sway separate into different temporal components because they
have different spatial-coherence patterns across subcarriers. An oracle picker
on the PCA components reached 2.43 brpm MAE.

## Approach — `rr_dsp.py`

A dedicated RR DSP path, independent of the 10 s HR feature window:

1. **RR-specific 60 s windows.** Respiration needs several breath cycles to
   resolve cleanly.
2. **PCA decomposition.** Center the (time x subcarrier) motion matrix, SVD,
   take the top 6 temporal components. Each component's respiration-band peak
   is an RR candidate with a prominence and a cross-component support count.
3. **Lock by prominence + support.** A real breath produces a sharp peak
   (prominence >= 5) that bleeds across >= 2 components; a sway artifact does
   neither. The lock band (12-38 brpm) also excludes the typical sway cluster.
4. **Follow by temporal continuity.** Breathing is frequency-stable window to
   window; sway and noise wander. A continuation candidate must clear the same
   multi-component support bar, so a lone noise peak near the track cannot keep
   a dead lock alive.
5. **Confidence gate.** When no component is plausible and continuous, RR is
   reported unavailable rather than emitting the artifact.

`RespirationTracker` is stateful; `estimate_rr_series` runs it over a capture.

## Validation

`tools/eval_rr.py` against `session_20260520T014522Z`, RR vs the Vernier belt:

| estimator | MAE (brpm) | within +-2 | notes |
|---|---|---|---|
| direct peak (production) | 7.58 | 16% | the artifact |
| synthetic XGBoost rr_model | 5.97 | 16% | prior-fallback, untrustworthy |
| **rr_dsp tracker** | **0.50** | **100%** | 48% availability |

Every window the tracker reports is within 1.1 brpm of the belt. The other 52%
it honestly reports unavailable.

## Known limitations

- Validated against **one** belt capture. The tracker is correct-by-construction
  and unit-tested, but one capture is not a population claim. More belt-paired
  captures would harden it.
- Sub-12-brpm slow breathers will not lock: separating slow breathing from body
  sway by frequency alone is inherently ambiguous. Documented in the module.
- Availability is signal-dependent. The validation capture had genuinely weak,
  variable breathing; a controlled capture (subject still, steady breathing)
  should see much higher availability.

## Integration (done)

- `inference_worker.py` runs the `RespirationTracker` on a separate 60 s RR
  buffer at its own cadence, alongside the 10 s HR path. It publishes
  `rr_bpm` / `rr_confidence` / `rr_available` / `rr_state` to `rr.predicted`,
  with `rr_bpm` null whenever the tracker gates the breath as unavailable.
  The worker no longer loads the unvalidated synthetic `rr_model.json`.
- The dashboard RR card blanks to "--" and the chart gaps on a gated reading
  instead of showing a stale value.

The `/predict` and `/predict/csi` REST endpoints still return a synthetic
`rr_model` value; those are synthetic-input smoke-test endpoints, separate
from the live dashboard path, and are left as a follow-up.
