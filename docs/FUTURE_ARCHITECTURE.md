# Future architecture — cross-environment robustness + model upgrades

The current pipeline (XGBoost on 9-dim handcrafted CSI features + per-
session calibration + Mahalanobis OOD) achieves ~4.15 bpm cross-session
HR MAE **within its training domain**. The 2026-05-16 home pilot
(`docs/HOME_PILOT_LOG.md`) demonstrated the expected failure mode: MAE
collapses to ~17 bpm when antenna geometry or room multipath change
materially. This doc lists the architectural improvements that address
cross-environment robustness and longer-term model performance, ordered
by ROI for our hardware/data scale.

For the live milestone plan see `docs/AUDIT_PLAN.md`; this doc is the
"what comes after the basic pipeline" research roadmap.

---

## Cross-environment robustness — no hardware change

### A1. Rolling-PCA subspace decomposition (Tier 1, highest ROI)

**Scope honest:** the bedroom_1 17.77 bpm regression
([`docs/HOME_PILOT_LOG.md`](./HOME_PILOT_LOG.md)) had three plausible
contributing factors: antenna mismatch with training data, HR
out-of-training-distribution, and room multipath. A1 addresses **only
the third**, and arguably not the largest of the three. The expected
near-term win from collecting bedroom_1 paired sessions with patch
antennas and retraining `models_real/` (the short-term step in the
home-pilot log) is likely larger than A1 in isolation. A1 is on the
roadmap because cross-environment multipath robustness is the dominant
long-term blocker once the training-distribution gap is closed.

The dominant multipath structure of a room lives in the top 1–3
principal components of the CSI covariance matrix. Subject vital signs
live in lower-energy components. A sliding-window SVD (~30–60 s window),
projecting out the top-K components, removes static-multipath energy
without needing an explicit empty-room baseline. As the room drifts
(temperature, humidity, neighbors), the top components drift with it —
so multipath drift is automatically tracked.

- Origin: "principal component clutter rejection" from airborne radar
  ground-moving-target indication (1970s). The technique is decades old
  and broadly published. Out of any modern patent's novelty range.
- Compute cost: 192-subcarrier × N-window SVD per minute. Pi 5 handles
  it in milliseconds.
- Interpretability: high. The removed components can be plotted.
- FDA story: clean — well-grounded prior art, no "black box" concerns.
- Estimated dev effort: 1–2 days. ~50 lines added to `preprocess.py`,
  parametrized by `--pca-components-removed K --pca-window-s W` so it
  can be ablated.

### A2. Adaptive baseline EMA (Tier 1)

Today `calibration.py` does a one-shot 30 s calibration at session start
using subject-present data. Extension: maintain `H_baseline` as an
exponential moving average of CSI with τ ≈ 5–15 min (much slower than
vital signs, faster than thermal drift), gated to update only during
"quiet" windows (HR/RR band variance below a threshold). Multipath
drift is tracked passively; patient motion auto-excluded from the
baseline.

- Math: keep τ > 30 × (1 / HR_freq) to avoid eating into the HR signal.
- Closest existing hook: `calibration.py:103-156`. Extension is ~30
  lines.
- Combines well with A1 — PCA removes the dominant room structure, EMA
  tracks slow drift on what remains.

### A3. Tighter spectral gating

`preprocess.py` already runs a Butterworth 0.1–3 Hz bandpass. Separate
the HR (0.8–3 Hz) and RR (0.1–0.5 Hz) bands into independent analyses
with explicit band-stop filters preventing inter-band leakage. RR-
coupled chest motion is the dominant interferer for HR extraction;
isolating the HR band cleanly is the cheapest accuracy win.

- Estimated effort: half day. Pure parameter tuning, regression-tested.

---

## Cross-environment robustness — small hardware addition

### A4. Reference antenna (best SNR improvement available)

Add a second ESP32-S3 RX positioned to **not see the subject** — e.g.,
pointing at a corner of the room, perpendicular to the TX→subject axis.
Its CSI is dominated by room-only multipath. Subtract reference-antenna
CSI from primary-antenna CSI to get a subject-only signal that is
automatically drift-corrected.

- Identical principle to auxiliary-antenna sidelobe cancellation in
  radar (1960s).
- ALFA APA-M25 patch antennas are directional → reference-antenna beam
  doesn't overlap with primary-antenna beam. Clean geometry.
- Hardware cost: $25–30. One more ESP32-S3 + one more patch antenna +
  one more U.FL pigtail.
- Pi already has multiple USB ports. `csi_capture.py` would gain a
  second-port flag and emit a stacked stream.
- Expected SNR improvement: 10–15 dB in typical rooms based on radar
  literature for analogous setups.
- Estimated dev effort: 1 weekend after hardware lands.

---

## Cross-environment robustness — algorithmic

### A5. Multi-subcarrier reference channel selection

Not every subcarrier sees the chest. Identify the chest-sensitive
subcarriers (those whose variance correlates with known HR during the
first session minutes) versus the room-only subcarriers (those that
don't). The room-only subcarriers serve as a live reference for
multipath drift; subtract their drift from the chest-sensitive ones.

- Common-mode rejection applied to CSI.
- Identification: correlate per-subcarrier variance with Polar HR
  during the calibration window; classify above-threshold subcarriers
  as chest-sensitive.
- Ships without a separate empty-room capture once the per-subject
  identification is cached.

### A6. Empty-room + subject-present differential

Capture 60–90 s of empty-room CSI, store as `baseline.txt` in the
session directory, subtract from subject-present CSI before feature
extraction. Cleaner than A5 in theory; in practice, the baseline drifts
between scan and session start, so this is best combined with A1 or A4
which adapt to drift.

- Operationally: requires patient to step out of the room or the
  baseline to be captured before they enter. Awkward for sleep
  monitoring; viable for daytime pilots.
- `modules/presence.py:71-86` already accepts empty-room CSI for
  presence-threshold calibration — adjacent infrastructure.
- Expected RR improvement: dramatic (RR is dominated by chest motion;
  background subtraction removes the static-multipath confound).
- Expected HR improvement: moderate (HR is dominated by RR-coupled
  motion in the same subcarriers; background subtraction doesn't
  separate HR from RR).

### A7. Domain adaptation (DANN, CORAL)

Train the model with a gradient-reversal branch that classifies which
room / antenna / subject the current window came from. The reversal
forces the encoder to produce representations that are invariant to
those nuisance factors. Reference: Ganin et al. 2016, "Domain-
Adversarial Training of Neural Networks."

- Requires labeled data across ≥5 rooms / antenna configs to be
  effective.
- Becomes feasible once the corpus grows past 10 paired sessions
  across ≥3 distinct environments.

---

## Long-term model upgrades

### B1. 1D CNN feature extractor alongside handcrafted features

A small 1D CNN over the same 10 s windows trained end-to-end to predict
HR, *fused* with the existing 9-dim handcrafted vector in the XGBoost.
Hybrid models often beat either alone, and the handcrafted features
provide a strong "always works" floor for FDA review.

- Estimated effort: 2 weeks, including training pipeline + ensemble
  fusion.
- Data ceiling: pays off at ~10+ paired sessions.

### B2. CNN-Transformer with self-supervised pre-training (the real
target architecture)

```
Raw CSI (subcarriers × time window)
        ↓
   1D CNN per-subcarrier → per-subcarrier temporal features
        ↓
   Transformer encoder (4–6 layers, 128–256 dim, ~2M params)
        ↓
   Heads: HR (quantile), RR (quantile), presence, identity
        ↓
   Domain-adversarial branch (gradient-reversed room/antenna classifier)
```

- Pre-train on 500+ hours of *unlabeled* CSI (collected 24/7, no Polar
  needed) using masked-autoencoding. Reference techniques: BERT (Devlin
  et al. 2018), MAE (He et al. 2022).
- Fine-tune the heads on ~40–60 hours of paired (CSI + Polar + Vernier)
  data across multiple rooms and subjects.
- Quantile heads output `HR ∈ [p10, p50, p90]` instead of point
  predictions — clinically more useful, FDA-friendly.
- Domain-adversarial branch addresses cross-environment generalization
  natively.
- Estimated dev effort: 1–3 months when data is in hand.
- Compute: ~2M params, Pi 5 inference <50 ms per window.

**This is the M3-timeframe architecture.** Don't pre-build it; the
data has to land first. But start collecting the unlabeled corpus now
— continuous-capture mode that just dumps CSI to disk during quiet
hours. Even if unused for 6 months, it's the long-pole resource that
unlocks the upgrade.

### B3. Physics-informed multipath modeling

Parametrize the room as a set of dominant multipath paths with known
geometry; model thermal/humidity drift as phase shifts on those paths;
estimate drift parameters live; subtract. Heavy engineering, best
suited to a permanent installation where the room model can be cached.
M4 timeframe.

### B4. ICA / blind source separation

Treat CSI as a mixture of independent sources (heartbeat, breath, body
sway, ambient drift) and separate them via Independent Component
Analysis. `sklearn.decomposition.FastICA` provides mature library
support. Pays off most when sources are non-Gaussian (heartbeats are —
narrow spectral peaks). Less interpretable than PCA; complementary
rather than competing.

---

## Suggested sequencing

| Milestone | Add | Why this milestone |
|---|---|---|
| **Next 2 weeks (immediate)** | A1, A2, A3 | Pure-code, no hardware, no new data. Addresses the bedroom_1 failure mode directly. |
| **Once 3rd antenna + 2nd ESP32 arrive** | A4 | Single largest SNR improvement; requires hardware. |
| **After 10 paired sessions across 3 rooms** | A5, B1 | Per-subject reference selection + CNN ensemble. Needs data. |
| **After 100+ hours unlabeled CSI captured** | B2 | The big architectural upgrade. Needs the pre-training corpus. |
| **M4 / pre-FDA** | A7, B3 | Domain adaptation + physics modeling for cross-site claims. |

---

## What this doc deliberately does not propose

- Switching to deep learning end-to-end **now**. With ~4 paired
  sessions of training data, deep models overfit catastrophically. The
  current XGBoost-on-handcrafted-features architecture is correct for
  our current data scale.
- Replacing the existing pipeline. A1–A6 are *additions* to
  `preprocess.py` / `calibration.py`, not rewrites. The synthetic-model
  fallback path stays unchanged.
- Buying hardware speculatively. The reference antenna (A4) is the only
  hardware that pays off pre-data-collection.

---

## Cross-references

- Empirical failure case driving this roadmap: `docs/HOME_PILOT_LOG.md`,
  session 2026-05-16.
- Competitive context (Emerald comparison, FMCW vs WiFi CSI, IP
  considerations): `docs/COMPETITIVE_LANDSCAPE.md`.
- Sequenced milestone work: `docs/AUDIT_PLAN.md`.

---

## What's wired today

| Item | Status | Notes |
|---|---|---|
| A1 — `subtract_top_components` | **wired** | `multipath.py` + `preprocess.build_envelope_from_amps`; controlled by `VIFI_PCA_COMPONENTS_REMOVED` (default 0 = no-op). |
| A1 — `RollingPCASuppressor` | stub | `multipath.py` — `update()`/`transform()` raise `NotImplementedError`. xfail tests in `tests/test_multipath.py` lock the contract. |
| Train/serve K version barrier | **wired** | `metadata.json::pca_k` + `inference_worker._resolve_pca_k_from_metadata` refuses mismatched starts. |
| Envelope-builder consolidation | **wired** | Four formerly-duplicated sites (`api.py` ×2, `tools/inference_worker.py`, `preprocess.py`) collapsed to one canonical: `preprocess.build_envelope_from_amps`. Parity locked by `tests/test_envelope_builder_parity.py`. |
| MAE-vs-Polar quality gate | **wired** | `tools/first_capture_report.py` emits WARN at MAE≥8 bpm, CRITICAL at MAE≥12 bpm. |
| A2 — adaptive baseline EMA | not started | spec only |
| A3 — tighter spectral gating | not started | spec only |
| A4 — reference antenna | not started | hardware-gated |
| A5–A7 | not started | spec only |
| B1–B4 | not started | M3+ |
