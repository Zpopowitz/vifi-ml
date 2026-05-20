# ViFi v2 — 60 GHz Radar Architecture Plan

> **Status:** architecture + phased roadmap, hardened by independent CEO + engineering
> review (see the GSTACK REVIEW REPORT at the end). Phases 0–1 are detailed; Phases
> 2–4 are roadmap-level and get re-planned once Phase 1 reveals the real radar data.

**Goal:** Genuine **beat-by-beat** heart monitoring — individual heartbeat detection,
inter-beat intervals (IBI), heart-rate variability (HRV), a path to arrhythmia — from a
60 GHz mmWave radar, starting on the TI IWRL6432BOOST dev board.

**Why this exists:** ViFi's first life on WiFi CSI proved beat-by-beat is *physically
impossible* on 2.4 GHz single-antenna WiFi (~0.05 rad phase swing per beat, below the
per-beat noise floor; ~1.26 rad at 60 GHz). The owner ordered an IWRL6432BOOST on
2026-05-20. Backstory: `memory/project_radar_pivot.md`, `project_hr_data_bottleneck.md`.

**Tech stack:** TI IWRL6432 (single-chip 60 GHz FMCW radar), TI mmWave SDK, Code
Composer Studio / SysConfig / Uniflash, an FTDI C232HM-DDHSL-0 USB-to-SPI cable
(see §3 — a *required* second purchase), Python (numpy/scipy + PyTorch for the ML phase),
Polar H10 as per-beat ground truth (`hr_logger.py` v2, kept as-is).

---

## 0. Open strategic question — answer this before Phase 1

The independent strategy review raised a real challenge that is **the owner's call, not
an engineering decision**: *beat-by-beat is a capability the radar enables — but who
specifically needs contactless per-beat HRV on a still subject, badly enough that a
$30 chest strap isn't fine for them?* ViFi's founding "why" (trend monitoring of
averaged vitals between nursing rounds) is arguably served by averaged HR + RR alone.

This plan proceeds on the owner's explicit choice to pursue beat-by-beat. But Phase 0
includes an explicit task to **write the one-page demand thesis**. If it can't be
written, the honest move is to reframe v2 around *averaged trend monitoring done well
on radar* (still a real upgrade over WiFi) rather than beat-by-beat as an end in
itself. This is surfaced, not decided.

---

## 1. Background — how this radar produces a vital-signs signal

FMCW radar is a fundamentally different model from WiFi CSI — no subcarriers, no
variance-ranking:

1. **Chirp.** The IWRL6432 transmits a linear frequency sweep. Echoes return delayed.
2. **Range FFT.** Mixing echo with the transmitted chirp yields a beat frequency
   proportional to range. An FFT per chirp produces a **range profile** — a complex
   value per range bin (~4 cm bins at ~3.7 GHz sweep bandwidth).
3. **Static clutter removal (MTI).** The wall/furniture returns dominate every bin and
   are static. Subtracting the slow-time mean (or a first-order IIR) per bin removes
   them — **mandatory**, before anything else touches the phase.
4. **Range-bin selection + tracking.** The chest occupies one (or a few adjacent)
   range bins; the subject also drifts slightly, so the chest bin must be *tracked*,
   not fixed (range-bin migration corrupts a fixed-bin phase series). Multiple RX
   antennas give angle/Doppler to find and track the subject and reject clutter.
5. **Phase extraction — done properly.** After clutter removal, the chest bin's
   complex IQ samples trace an **arc** on the I/Q plane. A residual DC offset shifts
   that arc's centre off the origin; naïve `atan2(Q,I)` on an off-centre arc produces
   a *distorted, harmonic-rich* phase even for pure sinusoidal motion. The correct
   chain is: **DC-offset estimation (circle-fit / NLLS) → DACM phase demodulation →
   unwrap**. And because a 0.5 mm beat gives ~1.26 rad (> π), per-beat phase *will*
   wrap — unwrapping is mandatory and fragile, not polish.
6. **Vital-signs band split.** The displacement signal holds respiration (large,
   0.1–0.5 Hz) and the heartbeat (small, ~0.8–2.5 Hz).

**Radar's real advantages over WiFi:** ~25–30 dB more per-beat SNR from wavelength,
**plus range-gating** (~4 cm bins isolate the chest from clutter; WiFi's ~7.5 m range
resolution made gating impossible).

**What radar does NOT fix:** the respiration-harmonic problem. Breathing chest motion
is large and non-sinusoidal; its 4th–8th harmonics land squarely in the 0.8–2.5 Hz
cardiac band. This collision is *geometric, not SNR-limited* — more SNR does not remove
it. It is the same trap that bit WiFi, and it must be handled explicitly (§3 Phase 2).

---

## 2. Target architecture

```
IWRL6432 radar (60 GHz FMCW)
  → chirp frames  (frame rate ~100 Hz for per-beat IBI; see Phase 1b)
  → range FFT  ──────────────→ range profile (complex, per ~4 cm bin)
  → static clutter removal (MTI: slow-time mean / IIR subtraction)
  → subject range-bin selection + TRACKING (range + Doppler + presence)
  → phase extraction:  DC-offset circle-fit → DACM demodulation → unwrap
  → vital-signs band processing
       respiration band   0.1–0.5 Hz
       cardiac band       ~0.8–2.5 Hz   + respiration-harmonic handling
  → beat detection
       Phase 2: classical DSP (peak / template on the cardiac phase signal)
       Phase 3: ML beat-morphology model (radarODE / AirECG style) if DSP falls short
  → IBI series → HR, HRV (SDNN/RMSSD/pNN50), arrhythmia flags
  → ESP32-S3  (compute / connectivity)
  → dashboard / API / audit log   (existing ViFi infrastructure)
```

**Carries over (reuse):** ESP32-S3; `hr_logger.py` v2 (H10 per-beat ground truth — used
as-is); `rr_logger.py` v2; dashboard / API / audit log; the capture-orchestration
pattern; the evaluation discipline (LOSO, honest metrics).

**New:** FMCW DSP module (`radar/`) — range FFT, MTI, range-bin tracking, circle-fit +
DACM phase extraction; the TI mmWave SDK toolchain; radar firmware/config; an FTDI-based
host capture tool.

**Dies (unused, not destructively deleted):** `preprocess.py` CSI feature extraction;
`hr_net/` (CSI LSTM); the ESP32 CSI capture path; `calibrate_cfo_sfo`.

**Hardware front-end note — the BOOST's antenna.** The IWRL6432BOOST ships with a
**fixed on-board FR4 PCB antenna array** — etched into the board, no RF connector, not
swappable. **No lens is included, and TI sells no lens accessory for it.** This is the
antenna every TI vital-signs reference demo uses, and it is sufficient for the v2
working range (~0.3–1 m; mount the sensor ~0.7 m from the chest). Range extension — a
dielectric lens or an external high-gain antenna — is therefore a *custom-hardware*
effort, not an off-the-shelf accessory; it belongs in Phase 4 if placement flexibility
ever demands it, not Phase 1/2. The software range levers (longer coherent
integration; beamforming across the existing TX/RX channels) need no hardware change.

---

## 3. Phased roadmap with decision gates

### Phase 0 — While the board ships (now, ~1–3 weeks, no radar hardware)

- [x] **Order an FTDI C232HM-DDHSL-0 USB-to-SPI cable (~$25–35) now.** Critical — see
      Phase 1b: the IWRL6432's UART exposes only *processed* output; raw range-cube
      data needs SPI, and the BOOST has **no onboard SPI FTDI chip**. The
      C232HM-DDHSL-0 is the exact cable TI's SDK docs and the
      `ti_iwrl6432_spi_data_stream` reference build both specify (a generic FT232H
      breakout is the same chip, but TI's tooling and pinout are written for this
      cable). It ships as 10 colour-coded flying-lead sockets — no extra connector to
      buy; you wire ~5–6 (SCLK, MOSI, MISO, CS, GND, optionally a GPIO handshake) onto
      the BOOST's 0.1″ headers.
- [x] **Confirm the raw-data path from TI docs, before the board arrives.** Read the
      MMWAVE-L-SDK *Motion-and-Presence-Detection* demo docs + the TI E2E thread on
      IWRL6432 SPI ADC capture, and the `ti_iwrl6432_spi_data_stream` repo. Key fact to
      internalise: **raw streaming requires low-power mode OFF (`lowPowerCfg 0`)** and
      is incompatible with the headline low-power vital-signs demo. Findings:
      `docs/RADAR_PHASE0_NOTES.md` §1 — incl. the C232HM wiring map, the Path A (raw
      ADC) vs Path B (range cube) firmware decision, and the S1/SOP switch settings.
- [ ] Install the TI toolchain: MMWAVE-L-SDK, **SysConfig**, Code Composer Studio,
      Uniflash. Version-pinned checklist + install gotchas: `docs/RADAR_PHASE0_NOTES.md` §2.
- [x] Study the FMCW vital-signs model — TI's mmWave vital-signs lab; the
      circle-fit/DACM phase-demodulation literature. Summary: `docs/RADAR_PHASE0_NOTES.md` §3.
- [x] Read the per-beat ML references: radarODE (arXiv 2408.01672), AirECG
      (`github.com/LangchengZhao/AirECG`). Summary + datasets: `docs/RADAR_PHASE0_NOTES.md` §4.
- [x] **Write the one-page demand thesis** (see §0). Who needs contactless per-beat on
      a still subject, and why isn't a chest strap enough? Draft: `docs/RADAR_DEMAND_THESIS.md`
      (awaiting owner sign-off).

### Phase 1a — Smoke test on the stock demo (~1–2 days with hardware, no code)

The cheapest possible de-risk, before building anything:

- [ ] Flash TI's stock vital-signs demo. Sit still ~0.7 m in front, ~5 min, H10 on.
- [ ] Compare the demo's *own averaged-HR output* to the H10.

> **GATE 0 — does the radar see your heart at all?** If TI's processed averaged HR
> tracks the H10 even loosely, the radar is working and worth the Phase 1b build. If it
> is garbage even on TI's own pipeline, stop and debug placement/config first. Cost: a
> day, zero new code.

### Phase 1b — Raw-data capture + per-beat signal verification (~2–3 weeks with hardware)

> Re-baselined from "1 week" per the engineering review — CCS + SysConfig + a custom
> chirp config + SPI streaming firmware + a host capture tool + H10 time-sync is
> realistically 2–3 weeks for someone new to the TI toolchain.

- [ ] Flash the **Motion-and-Presence-Detection** demo (not the OOB demo) with
      `lowPowerCfg 0`; wire the C232HM-DDHSL-0 for SPI range-cube streaming. Check the
      wire-colour → BOOST-pin map against TI's docs / the `loeens` repo, and set the
      board's debug/SPI DIP switches per the EVM user guide — miswiring can damage the
      board.
- [ ] **Pin the chirp configuration explicitly:** frame rate **~100 Hz** (Phase 0
      research revised this up from ≥30–50 Hz: the OOB demo's ~20 Hz quantizes IBI to
      ±50 ms and 30 fps still gave weak HRV in the literature — see
      `docs/RADAR_PHASE0_NOTES.md` §3), ~3.75 GHz sweep bandwidth (for ~4 cm range
      bins), samples-per-chirp, slope, idle time. Document them.
- [ ] Build the host-side capture tool: stream the range cube to disk, timestamped,
      run simultaneously with `hr_logger.py`.
- [ ] **Radar ↔ H10 time synchronization.** The H10 is BLE with variable latency;
      unsynced timestamps are the dominant IBI-error source. Capture a shared sync
      event at the start of every session (e.g. a sharp tap, visible as a motion spike
      to the radar and as an artifact you can mark on the H10 stream).
- [ ] Capture paired sessions: seated, still, ~0.7 m, ~5 min; then a second
      distance/posture.
- [ ] **Verification analysis:** MTI → track the chest bin → circle-fit + DACM phase →
      cardiac band. Then: (a) does the spectrum show a clean cardiac peak at the H10's
      rate? (b) are beat-aligned 200 ms segments self-similar?

> **GATE 1 — go/no-go.** PASS = beat-aligned segments are self-similar **AND their
> repetition period matches the H10 IBI within tolerance** (the period-match clause is
> essential — a strong respiration harmonic is *also* periodic and self-similar and
> would otherwise pass a naïve correlation test). FAIL = debug range bin / distance /
> motion / chirp config before building Phase 2.

### Phase 2 — Classical-DSP beat detection (weeks ~4–6 with hardware)

- [ ] `radar/` module, tested: MTI clutter removal; range-bin selection + **tracking**
      (handle migration; coherently combine adjacent bins); **DC-offset circle-fit +
      DACM** phase extraction; unwrap.
- [ ] **Respiration-harmonic handling — explicit, not assumed.** The harmonic collision
      is geometric. Mitigate with a harmonic-comb notch keyed to the measured
      respiration fundamental, and/or exploit that cardiac *higher* harmonics can exceed
      respiratory ones in the upper cardiac band (arXiv 2407.07380).
- [ ] Beat detection on the cardiac phase signal: peak detection and/or template
      matching.
- [ ] **Motion gating — required, not optional.** Use the radar's Doppler / range
      signal to detect gross body motion (a moving body is a large, obvious radar
      signal — the easiest thing radar does). During motion, *suppress* vital-signs
      output — emit an explicit "motion — no reading" state — and resume cleanly when
      the subject settles. This is the difference between a demo that only works if the
      subject holds still and a monitor that survives a real bed. The distinction
      matters: this *gates* motion, it does not *track vitals through* motion — the
      latter is infeasible for any contactless modality.
- [ ] Eval harness: per-beat F1, IBI error (ms), HR MAE (bpm) vs H10 — scoring IBI/HRV
      (which cancels the constant electromechanical delay, §4), not absolute timing.
      Also report **coverage**: the fraction of capture time with a valid (non-gated)
      reading — the honest measure of how usable the monitor is in a real setting.

> **GATE 2.** Target to continue *without* ML: per-beat F1 ≥ 0.9, IBI error ≤ ~30 ms on
> a still single subject. The engineering review flags this as realistic but optimistic
> for pure classical DSP — budget for the upper end and expect Phase 3.

### Phase 3 — ML beat-morphology model (conditional — only if Gate 2 fails, with a documented reason)

- [ ] radarODE / AirECG-style model reconstructing beat morphology from the radar phase
      signal.
- [ ] Training data: multi-subject ViFi captures + a public radar dataset (MMECG —
      35 subjects, 10 h ECG-synced; the 2026 "Age-Balanced Subject-Varied mmWave Vital
      Signs" dataset).
- [ ] Honest leave-one-subject-out evaluation.

> Treat Phase 3 as the *probable* path, not a fallback — but do not start it until
> Gate 2 has actually failed and the failure is documented. Do not pre-commit to a
> specific architecture.

### Phase 4 — Productization (DEFERRED — post-validation AND post-demand-thesis only)

Custom AoP board, ESP32 integration, dashboard/API for beat-level data, the
reliability/ops story. **Do not start any Phase 4 work until beat-by-beat is validated
*and* the §0 demand thesis identifies a real buyer.** It is the most capital-intensive,
highest-risk work behind the weakest premise.

---

## 4. Evaluation methodology

- **Ground truth:** Polar H10 RR-intervals via `hr_logger.py` v2.
- **Metrics:** per-beat F1, IBI error (ms), HR MAE (bpm), HRV error (SDNN/RMSSD/pNN50).
- **Time synchronization:** explicitly handled — a shared sync event per session
  (§3 Phase 1b). Unsynced radar/H10 clocks are the dominant IBI-error source.
- **Electromechanical delay:** the radar measures *mechanical* chest motion; the H10
  measures *electrical* activity. There is a QRS-to-contraction delay of tens of ms.
  It is roughly constant, so it **cancels in IBI/RMSSD differences** — score those.
  For absolute per-beat F1, calibrate and subtract a constant lag first, or score IBI
  only.
- **Splits:** leave-one-session-out, then leave-one-subject-out as subjects accrue.
  Never shuffled overlapping windows.
- **Honesty rule:** report multi-seed mean ± std; state distance/posture/motion for
  every result.

---

## 5. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Raw-data access needs an FTDI C232HM-DDHSL-0 cable + the right demo + low-power OFF** — the BOOST has no onboard SPI FTDI chip | **Critical** | Order the C232HM-DDHSL-0 in Phase 0; confirm the SPI path + wire-colour/pin map from TI docs before the board arrives; Phase 1b uses the Motion-and-Presence demo, `lowPowerCfg 0` |
| 2 | DSP chain omits mandatory steps (MTI, DC-offset circle-fit, harmonic handling) | High | Now explicit Phase 2 tasks (§3) |
| 3 | Respiration harmonics contaminate the cardiac band — geometric, not fixed by SNR | High | Harmonic-comb notch; exploit cardiac higher harmonics; named Phase 2 task |
| 4 | Radar ↔ H10 time-sync failure inflates IBI error | High | Shared sync event per session; score IBI/HRV (delay cancels) |
| 5 | Range-bin migration as the subject shifts corrupts a fixed-bin phase series | Medium | Bin tracking + adjacent-bin coherent combination |
| 6 | Phase-unwrap failure under transient body motion | Medium | Motion-segment detection + exclusion; v2 scope is still subjects |
| 7 | Gate 2 not reachable by classical DSP → Phase 3 ML is the real, unbounded project | High | Phase 3 time-boxed when reached; or accept averaged HR+RR as v2 |
| 8 | Phase 1b underestimated — TI toolchain learning curve | High | Re-baselined to 2–3 weeks; Phase 1a smoke test de-risks first |
| 9 | Solo-founder scope overrun (SDK + DSP + ML + RF PCB + recruiting + v1 stack) | High | Defer Phase 4 entirely; defer Phase 3 until Gate 2 fails; don't port the v1 dashboard/API until there's beat data |
| 10 | Oscillator/thermal drift over a multi-minute capture | Low–Med | Detrend; keep captures short; characterise in Phase 1b |
| 11 | Motion *tracking* (reading vitals during active movement) — infeasible for any contactless modality; a ~0.5 mm heartbeat is swamped by cm-scale body motion | High | Out of scope, permanently. But motion *gating* (detect movement → suppress output → resume on settle) is **in** v2 scope as a Phase 2 task — radar detects motion trivially. Posture-coverage and multi-person stay out of v2. |
| 12 | **No identified user needs contactless per-beat more than a chest strap** | **Critical (strategic)** | §0 demand-thesis task; owner's call |
| 13 | Scope thrash — running radar AND WiFi in parallel | Medium | WiFi CSI is shelved. One sensor. |

---

## 6. Cost & timeline

- **Dev hardware:** IWRL6432BOOST ~$200 (ordered) **+ FTDI C232HM-DDHSL-0 USB-to-SPI
  cable ~$25–35 (order now)**. No DCA1000 needed. The BOOST's antenna is fixed on-board
  FR4 PCB — no lens or external antenna to buy (see the hardware front-end note in §2).
- **Production (Phase 4, deferred):** bare IWRL6432 chip ~$10–24 + custom AoP board;
  v2 sensor BOM ~$25–60.
- **Timeline:** Phase 0 now. Phase 1a ~1–2 days. Phase 1b ~2–3 weeks. Phase 2 ~2–3
  weeks. Phase 3 conditional. Phase 4 post-validation + post-demand-thesis.

---

## 7. Honest framing — what v2 will and will not be

**Will:** make genuine beat-by-beat *physically possible* — per-beat detection, IBI,
HRV — when the subject is still (seated/supine, ~0.7 m, single subject), where mmWave
radar is demonstrated against ECG. And, via motion gating, **survive a real setting**
where the subject moves periodically — reading vitals during the still stretches and
honestly reporting "motion — no reading" during the rest, the way hospital telemetry
already handles motion artifacts.

**Will not (in v2):** *track vitals through* active movement (infeasible for any
contactless modality — a ~0.5 mm heartbeat is buried under cm-scale body motion); or
handle arbitrary posture, multiple people, or long range. Those are a multi-year
frontier for the whole field.

**The honest pitch:** "a contactless monitor for a resting / sleeping patient — solid
vitals during the still periods, honest about motion gaps." For the founding use case
(trend monitoring to catch deterioration between nursing rounds), gaps are fine — you
need *enough* clean readings to see a trend, not an unbroken stream; bed-rest and sleep
are mostly still. What v2 is *not* is a wear-it-while-active device. Pitch it as
"medical-grade free-living HRV" and it fails; pitch it as "honest resting-patient
monitoring" and it holds — *if* the §0 demand thesis finds someone who needs it.

---

## GSTACK REVIEW REPORT

Reviewed by two independent agents (no shared context): a CEO/strategy reviewer and a
radar/DSP engineering reviewer. Codex unavailable — single-voice per lens.

### Engineering review — verdict: physically sound, but not ready as first drafted

Caught one **critical** error and several mandatory omissions, all now fixed in this
revision:

| # | Finding | Resolution |
|---|---|---|
| E1 | **Data-access claim was wrong.** UART exposes only processed TLV output, not per-bin phase. Raw range-cube needs SPI + an FTDI USB-SPI adapter + the Motion-and-Presence demo + `lowPowerCfg 0` (incompatible with the low-power vital demo). | §3 Phase 0/1b rewritten; the exact cable (FTDI C232HM-DDHSL-0) verified against TI docs + the `loeens` reference build and added to the BOM; Risk #1 = Critical. |
| E2 | Missing mandatory DSP steps: static clutter removal (MTI), DC-offset circle-fit / DACM phase demodulation. Naïve `atan2` on an off-centre IQ arc gives distorted harmonic-rich phase. | §1, §2, §3 Phase 2 — now explicit named steps. |
| E3 | Chirp configuration unspecified — frame rate, bandwidth, samples/chirp. OOB ~20 Hz frame rate too coarse for ≤30 ms IBI. | §3 Phase 1b — explicit "pin the chirp config" task, ≥30–50 Hz. |
| E4 | "Radar's higher SNR makes respiration-harmonic separation tractable" — **wrong reasoning.** The harmonic collision is geometric. | §1 + §3 Phase 2 corrected; Risk #3. |
| E5 | Gate 1 self-similarity threshold too weak — a respiration harmonic is also self-similar. | Gate 1 strengthened — period must match H10 IBI. |
| E6 | Phase 1 = 1 week unrealistic. | Re-baselined to Phase 1a (~1–2 days) + Phase 1b (~2–3 weeks). |
| E7 | Radar↔H10 time-sync and electromechanical delay unaddressed. | §3 Phase 1b sync task; §4 eval section. |
| E8 | Range-bin migration, phase-unwrap-under-motion, oscillator drift missing from risks. | Added to §5. |

### CEO / strategy review — verdict: with major changes

| # | Finding | Resolution |
|---|---|---|
| C1 | **No demand thesis for beat-by-beat** — chased as an end in itself; the founding "why" is averaged trend monitoring. *Single biggest strategic risk: building a contactless HRV sensor no user needs more than a $30 chest strap.* | **Surfaced, not auto-decided** — §0 + Risk #12. Phase 0 task: write the one-page demand thesis. The owner has explicitly chosen beat-by-beat; this records the open question honestly. |
| C2 | The pivot abandons ViFi's differentiated "$50 WiFi, infrastructure-free" story — radar makes ViFi "one more radar vitals project." | Noted in §0 / §7 — v2 should re-derive its wedge, not inherit the WiFi narrative. |
| C3 | Phase 3 (ML) is unbounded and probably load-bearing. | §3 Phase 3 reframed as the *probable* path, gated on a documented Gate-2 failure. |
| C4 | Solo-founder scope overrun. | Risk #9; Phase 4 deferred; Phase 3 gated; v1 stack not ported until there's beat data. |
| C5 | Cheaper de-risk: don't build a capture tool first — flash TI's stock demo, compare its averaged HR to H10. | Adopted as **Phase 1a / Gate 0**. |

### The one decision that is yours

Everything engineering is fixed in this revision. The one open item is **C1 / §0**: this
plan pursues beat-by-beat because you chose it — but no specific user need has been
articulated for contactless per-beat on a still subject. Phase 0 makes "write the demand
thesis" an explicit deliverable. If you can write it, v2 is well-founded. If you can't,
the honest pivot-within-the-pivot is averaged trend monitoring on radar. That call is
yours; the plan does not make it for you.
