# Glossary

Acronyms and terms that reviewers, contributors, and operators
encounter in ViFi.

## Core

- **CSI**: Channel State Information. Per-subcarrier amplitude +
  phase samples emitted by 802.11n/ac receivers. ViFi uses
  amplitude only in v1; phase in planned v2.
- **HR**: Heart rate (beats / minute).
- **RR**: Respiratory rate (breaths / minute).
- **ESP32-S3**: Espressif's WiFi/Bluetooth SoC. Specifically the
  ESP32-S3-DevKitC-1U-N8R8 board with external antenna.
- **Polar H10**: BLE chest-strap HR monitor used as the reference
  ground truth for HR.
- **Vernier GDX-RB**: BLE respiration-belt monitor used as the
  reference for RR.

## DSP

- **Subcarrier**: a frequency bin within an OFDM symbol. The
  ESP32-S3 emits 192 subcarriers per packet (most firmware variants).
- **Top-K subcarriers**: the K subcarriers with highest variance
  over a window; carries most of the motion-relevant signal.
- **Hann window**: cosine-shaped tapering function applied before
  FFT to reduce spectral leakage.
- **Parabolic refinement**: subbin frequency estimation by fitting
  a parabola through the FFT peak and its two neighbors.
- **PhaseBeat**: a published technique (Wang et al. 2017) for
  removing CFO/SFO from CSI phase to expose chest-motion phase
  rotation.

## Hardware

- **CFO**: Carrier Frequency Offset. Mismatch between TX and RX
  oscillators; appears as a global linear phase trend across
  packets.
- **SFO**: Sampling Frequency Offset. Sample-clock skew; appears
  as a per-packet linear phase ramp across subcarriers.
- **BLE**: Bluetooth Low Energy.
- **OTA**: Over-the-Air firmware update.

## ML

- **MAE**: Mean Absolute Error.
- **OOD**: Out-Of-Distribution. Detector flags inputs that don't
  resemble training data.
- **Mahalanobis distance**: a multivariate distance metric used as
  the ViFi OOD signal.
- **Quantile model**: an XGBoost regressor trained on the
  pinball loss to estimate confidence intervals.
- **Drift**: gradual shift in feature distribution over time
  (different population, different hardware, different room).

## Compliance

- **PHI**: Protected Health Information (HIPAA).
- **HIPAA**: U.S. Health Insurance Portability and Accountability Act.
- **FDA**: U.S. Food and Drug Administration.
- **510(k)**: FDA premarket notification pathway for medical devices.
- **De Novo**: FDA pathway for novel device classifications.
- **IEC 62304**: International standard for medical device software
  lifecycle.
- **ISO 14971**: International standard for risk management of
  medical devices.
- **ISO 13485**: International standard for medical device QMS.
- **DPIA / GDPR**: EU data protection impact assessment.
- **SaMD**: Software as a Medical Device.
- **PMS**: Postmarket Surveillance.
- **MDR**: Medical Device Reporting (FDA adverse-event procedure).

## Architecture

- **Bus**: the message-passing layer; in ViFi this is Redis Streams.
- **Topic**: a stream identifier on the bus, e.g.
  `csi.raw.alice`.
- **Cursor**: pointer into a stream; consumers track which messages
  they've seen.
- **DLQ**: Dead-Letter Queue. Where unprocessable messages go.
- **WS**: WebSocket.
- **CSP**: Content Security Policy (HTTP header).
- **HSTS**: HTTP Strict-Transport-Security.
- **mTLS**: mutual TLS.

## Operational

- **SLO**: Service Level Objective.
- **SLI**: Service Level Indicator (the measurement).
- **SLA**: Service Level Agreement (the contractual promise).
- **RPO**: Recovery Point Objective.
- **RTO**: Recovery Time Objective.
- **MTTR**: Mean Time To Recovery.
- **PSUR**: Periodic Safety Update Report.

## Project

- **Class II SaMD**: Medical device classification ViFi most likely
  fits under (cardiac monitor accessory).
- **Predicate device**: an existing FDA-cleared device that the
  applicant claims substantial equivalence to.
- **Pilot**: a limited deployment for clinical evaluation before
  full launch.
