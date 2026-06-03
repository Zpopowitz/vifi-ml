# Retired artifacts and falsified approaches

**Read this before re-implementing old ideas.** Files listed here were removed or
must not be resurrected without new evidence. Authoritative current truth:

| Topic | Source of truth |
|---|---|
| Operator state | `docs/STATUS.md` |
| CSI LOSO HR (13.90 bpm) | `docs/eval/2026-05-23-loso.json` |
| Radar HR reality (~27 bpm pooled MAE, r≈+0.56) | `docs/RADAR_HR_FINDINGS_2026-05-29.md` |
| SPI capture fix | `docs/radar_spi_firmware/APPLIED_EDITS.md` |
| Stage-2 dataset | `docs/RADAR_DATASET_PROTOCOL.md` |
| Live stack | `docs/LIVE_STACK.md` |
| Board day (partly superseded banner) | `docs/RADAR_STARTUP.md` |

Last updated: 2026-06-03 (context-window purge).

---

## Metrics that do NOT reproduce

- **4.15 bpm cross-session CSI MAE** — retracted. Use **13.90 bpm** (LOSO, 3
  founder sessions). Still wrong on `site/src/content/` until fixed.
- **Radar ~10–11 bpm MAE** — wrong banner text in old `STATUS`/`RADAR_STARTUP`
  snapshots. Pooled spectral picker MAE is **~27 bpm** (tracks, not accurate).
- **Per-fold 3.89 / 4.41 bpm** — does not reproduce. Use **13.94 / 7.96 / 19.78**.
- **Session IDs `session3`, `session4`, `session5`** — never existed on disk.

---

## Approaches falsified on real data (do not re-run as "fixes")

1. **Equal-weight MRC** for HR accuracy — implemented (`radar/dsp.py:mrc_combine`);
   falsified on 2026-05-29 captures. MRC pooled r≈+0.56 but MAE ~27 bpm; best
   single RX flips per capture.
2. **SNR/peakiness/SCR-weighted RX selection** — ranks the heartbeat channel last.
3. **Fixed "use RX0"** — good channel is capture-dependent.
4. **Cross-RX coherence (geomean)** — locks ~62 bpm artifact.
5. **Widen cardiac band past 150 bpm** — no effect; artifact is sub-band.
6. **Correctly keyed respiration harmonic notch** — drops tracking to r≈+0.01
   (heartbeat collides with harmonics).
7. **Hand-tuned spectral peak-picking alone** — at ceiling; oracle HR ~3 bpm @
   20 s with perfect peak choice → need learned selector + more paired data.
8. **CSI fingerprint LOSO for accuracy** — made cross-session MAE worse; per-subject
   calibration shelved.
9. **Synthetic serving model / `/predict/demo`** — removed; API serves `models_real`
   only.

---

## Docs removed from the repo (2026-06-03 purge)

| Path | Why retired |
|---|---|
| `docs/RADAR_SPI_PI_BRINGUP.md` | Pre-fix snapshot: claimed no HR, MRC "not implemented" |
| `docs/RADAR_SPI_FIRMWARE_FIX.md` | Superseded busy-pin theory; fix is `APPLIED_EDITS.md` |
| `docs/RADAR_SPI_RESTART.md` | Pre-EDMA-fix reset recipe |
| `docs/superpowers/plans/2026-05-19-beat-detection-hr.md` | Shelved 2264-line plan; open checkboxes mislead agents |

**cursorignored** in-repo (historical, not action items):

- `docs/RADAR_SPI_DEBUG.md` — investigation log
- `docs/AUDIT_PLAN.md` — PR A–L era

**`docs/superpowers/**`:** entire tree was removed from disk in the same purge window
(landed SP1/SP2 specs; do not treat as open work). Restore from git only if you need
archaeology — not for agent task lists.

---

## Scripts removed from the repo (2026-06-03 purge)

One-off SPI bench scripts under `tools/spi_debug/` and `tools/spi_dbg*.py` /
`tools/spi_byte_dump*.py`. **Kept** (still referenced): `artifact_probe.py`,
`resp_notch_experiment.py`, `dataset_eval.py`, `analyze_mrc_vs_single.py`,
`band_experiment.py`, `rr_probe.py`, `thru_line.py`, `radar_train_hr_selector.py`,
`ftdi_reset.py`, `ti_spi_capture.py`, `dsp_probe.py`, `outlier_rx_test.py`.

---

## Phantom paths (never add without creating the file)

- `hr_net/` — directory never existed in this repo
- `docs/HOME_PILOT_LOG.md`, `docs/FUTURE_ARCHITECTURE.md`,
  `docs/DEMAND_VALIDATION_INTERVIEWS.md` — cited from code; **not on disk**
- Root `MODEL_CARD.md`, `RESULTS.md`, `ROADMAP.md`, `FAQ.md` — content under
  `site/src/content/`

---

## How agents should use this file

- **Do not** treat absence from the index as "never tried" — check here first.
- **Do** update this file when retiring another path or falsifying an approach.
- **Do not** add this file to `.cursorignore` or Obsidian excludes.
