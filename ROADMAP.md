# ViFi Roadmap

Contactless patient monitoring on ~$50 of commodity WiFi hardware per hospital bed. HR is validated on real hardware today; everything else runs on the same CSI stream and the same pair of ESP32-S3 nodes.

---

## Status board

| Capability | Status | Where |
|---|---|---|
| **Heart rate (HR)** | **Shipped — 4.15 bpm cross-session MAE on real ESP32-S3 hardware** ([RESULTS.md](./RESULTS.md)) | `train.py`, `tools/retrain_on_real.py` |
| Respiratory rate (RR) | Pipeline shipped, awaiting paired RR ground truth | `train.py`, `preprocess.py` |
| Synthetic CSI generator | Shipped (sanity check) | `data_gen.py` |
| Multi-subcarrier CSI ingest | Shipped — live API endpoint | `output/api.py` |
| ESP32 capture + HR logger | Shipped — hands-free 2-min paired capture | `tools/csi_capture.py`, `hr_logger.py` |
| FastICA unmixing | Shipped (simulated demo) | `output/dashboard.py` |
| Presence / occupancy | Shipped (variance threshold) | `modules/presence.py` |
| Apnea detection | Stub, returns HTTP 501 | `modules/apnea.py` |
| Transient-event logger | Stub | `modules/transient_events.py` |
| Gait / walking-speed | Stub (WiGait reference) | `modules/gait.py` |
| Fall detection | Stub (WiFall reference) | `modules/falls.py` |
| 4-receiver array | Stub | `modules/four_node_sync.py` |

`GET /roadmap` returns this manifest live from the API.

---

## In flight (next 4 weeks)

| Milestone | Target | Deliverable |
|---|---|---|
| Multi-subject HR validation | May 2026 | 10+ subjects, varied HR ranges, target cross-subject MAE <3 bpm |
| Multi-room validation | May 2026 | 3+ rooms, fixed subject, measure setup-specific bias |
| Respiratory-rate paired captures | June 2026 | Add Vernier Go Direct respiration belt as RR ground truth |

---

## Stage 2 (months 2–4)

| Capability | Module | Prior art | Ground truth |
|---|---|---|---|
| Apnea detection | `modules/apnea.py` | ApneaApp (UW 2015) | recording pulse oximeter |
| Transient-event logger | `modules/transient_events.py` | none — clinical wedge | same HR/RR stream |
| Improved subcarrier features | `preprocess.py` | per-subcarrier ensemble | — |
| Phase-domain features | `preprocess.py` | PhaseBeat (CFO/SFO calibration) | — |

---

## Stage 3 (months 4–8)

| Capability | Module | Prior art | Ground truth |
|---|---|---|---|
| Gait / walking speed | `modules/gait.py` | WiGait (MIT CSAIL 2018) | timed course, pressure mat |
| Fall detection | `modules/falls.py` | WiFall | actors + crash mat |
| 4-receiver multi-patient array | `modules/four_node_sync.py` | ICA + AoA | 4x ESP32-S3 array |

---

## Stage 4 (months 6–12)

- First hospital pilot (5–10 beds, wellness-grade, pre-FDA)
- IRB submission and approval
- Subject-level cross-validation across diverse demographics

---

## Stage 5 (months 12–18)

- FDA 510(k) Class II submission for vitals monitoring
- ISO 13485 Quality Management System
- Clinical validation study at academic medical center
- Estimated cost: ~$300K (clinical study + consultant + QMS + filing)

---

## Long-term / research (12+ months)

- Heart Rate Variability (beat-to-beat precision; requires phase work)
- Arrhythmia classification (downstream of HRV)
- Sleep staging (overnight recordings + polysomnogram partnership)

---

## Out of scope (wrong physics for WiFi CSI)

| Capability | Why not | Alternative |
|---|---|---|
| Blood oxygen (SpO2) | Optical sensor, not RF | $5 PPG add-on module |
| Body temperature | IR sensor, not RF | $10 IR thermometer add-on |
| Blood pressure | Open research problem industry-wide | None pursued |
| ECG waveform | Requires direct skin contact | None pursued |

---

## Hardware BOM

### Per 2-node room (single-patient monitoring)

| Item | Qty | ~$ |
|---|---|---|
| ESP32-S3-DevKitC-1U-N8R8 | 2 | 40 |
| Dual-band 2.4/5 GHz RP-SMA antenna | 2 | 8 |
| IPEX1 U.FL → RP-SMA Female pigtail, 8" | 2 | 6 |
| **Total per room** | | **~$54** |

### Per 4-node room (multi-patient, Stage 3)

Add 2 more ESP32-S3 nodes + antennas + pigtails: ~$54 additional → **~$108 per shared room**.

### Per first capture kit (development)

Add Polar H10 chest strap ($90) for HR ground truth during dataset collection.

---

## Regulatory path

**Wellness-grade products first** (presence, falls, gait): no FDA clearance required. Target: first paying hospital customer 9–12 months post-funding.

**Vitals-grade products** (HR, RR, apnea): FDA 510(k) Class II. Target: ~18 months post-funding, ~$300K all-in.
