# ViFi Roadmap

Contactless patient monitoring on ~$50 of commodity WiFi hardware per
hospital bed. HR and RR are what's shipped today; everything else runs
on the same CSI stream and the same pair of ESP32-S3 nodes.

## Shipped (synthetic data)

| Capability | Status | Module |
|---|---|---|
| Heart Rate (HR) | 100% within-tolerance on held-out synthetic | `train.py`, `preprocess.py` |
| Respiratory Rate (RR) | 100% within-tolerance on held-out synthetic | `train.py`, `preprocess.py` |
| Multi-subcarrier CSI ingestion | Live endpoint | `output/api.py :: /predict/csi` |
| ESP32 UDP bridge | Live, with simulator | `output/esp32_csi_collector.py` |
| FastICA unmixing (simulated) | Dashboard tab | `output/dashboard.py` |

## In flight (this month)

| Milestone | Target date | Deliverable |
|---|---|---|
| First real-hardware HR/RR capture | 2026-04-26 | Paired CSV: 2x ESP32-S3 + Polar H10 |
| First real-data HR MAE number | 2026-05-03 | MAE vs Polar H10 over 30 min of seated captures |
| 30-subject paired dataset | 2026-06 | Publishable baseline on ESP32-S3 |

## Near-term capabilities (weeks, after real HR/RR works)

| Capability | Module | Prior art | Ground truth |
|---|---|---|---|
| Presence / occupancy | `modules/presence.py` | trivial | walking in / out |
| Apnea detection | `modules/apnea.py` | ApneaApp (UW 2015) | recording pulse-ox, ~$200 |
| Transient-event logger | `modules/transient_events.py` | none — clinical wedge | same HR/RR stream |

## Mid-term capabilities (months)

| Capability | Module | Prior art | Ground truth |
|---|---|---|---|
| Gait / walking speed | `modules/gait.py` | WiGait (MIT CSAIL 2018) | timed course, pressure mat |
| Fall detection | `modules/falls.py` | WiFall | actors + crash mat |
| Multi-patient separation | `modules/four_node_sync.py` | — | 4x ESP32-S3 array |

## Long-term / research (12+ months)

- Heart Rate Variability (beat-to-beat precision, requires phase work)
- Arrhythmia classification (downstream of HRV)
- Sleep staging (overnight recordings + polysomnogram partnership)

## Out of scope (wrong physics for WiFi CSI)

- Blood oxygen (SpO2) -- optical sensor, add as companion module
- Body temperature -- IR sensor, add as companion module
- Blood pressure -- open research problem; not attempting
- ECG waveform -- requires skin contact; not attempting

## Hardware BOM (per 2-node room)

| Item | Qty | ~$ |
|---|---|---|
| ESP32-S3-DevKitC-1U-N8R8 | 2 | 40 |
| Dual-band 2.4/5 GHz RP-SMA antenna | 2 | 8 |
| IPEX1 U.FL -> RP-SMA Female pigtail, 8" | 2 | 6 |
| USB-C data cable | 2 | 10 |
| Polar H10 chest strap (HR ground truth) | 1 | 90 |
| **Total** | | **~$154** |

4-receiver array for multi-patient rooms: add 2 more ESP32-S3 nodes
(~$40 additional).

## Regulatory path

- Wellness-grade deployment first (presence, falls, gait): no FDA
  clearance required. Target: first paying hospital customer in 9-12
  months.
- Vitals-grade deployment: FDA 510(k) Class II. Target: ~18 months,
  ~$300K.
