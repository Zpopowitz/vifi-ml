# Competitive landscape — comparable systems + IP posture

Working notes on the contactless vital-signs landscape as it relates to
ViFi's positioning. The focus is on technical comparability and IP /
freedom-to-operate considerations, not market sizing.

**This is orientation, not legal advice.** A proper freedom-to-operate
(FTO) search should be commissioned from IP counsel ahead of the pilot
launch and again ahead of any commercial release. See §"IP risk
posture" below for what to commission and when.

---

## Emerald Innovations (MIT spinoff, Dina Katabi's group)

The most-cited comparable in the academic and clinical-pilot space.
Sets the benchmark for what "contactless monitoring during movement"
looks like as a deployed product.

### Verifiable facts

- Spun out of MIT CSAIL, Katabi lab.
- Wall-mounted device (~iPad form factor), plugs into power, monitors
  occupants without wearables.
- FDA 510(k) clearance for gait monitoring (Parkinson's). Additional
  trials in sleep apnea and breathing monitoring per their public
  materials.
- Operating band per FCC filings: roughly 5.46–7.25 GHz with ~1.7 GHz
  of swept bandwidth.
- Technology bet: **FMCW (frequency-modulated continuous wave) radar
  with antenna arrays**, *not* WiFi CSI. This is the most important
  technical fact about Emerald relative to ViFi.

### Verifiable published papers from the Katabi group

(I am confident these exist and the descriptions match my recollection.
For citation-quality references, verify via Google Scholar before
publication.)

- WiTrack / WiTrack 2.0 (Adib et al., NSDI 2014 / 2015) — through-wall
  3D motion tracking
- Vital-Radio (Adib et al., CHI 2015) — vital signs via radio
  reflections
- RF-Pose (Zhao et al., CVPR 2018) — body pose through walls via RF
- RF-Avatar / RF-Pose3D (2019) — 3D skeleton from RF
- BodyCompass (Yue et al., 2020) — sleep posture monitoring
- Parkinson's gait at-home monitoring (Liu et al., Nature Medicine
  2022)

### Why FMCW radar is the differentiator

WiFi CSI on commodity ESP32 hardware has 20–40 MHz of bandwidth →
range resolution of ~7–15 m. You cannot isolate a chest-sized voxel.
FMCW with 1–2 GHz of bandwidth → range resolution of 7–15 cm. **Two
orders of magnitude.** Combined with antenna arrays (typical 4–8
antennas vs ViFi's 1 RX), Emerald can localize the subject in 3D to
~10 cm and sample chest reflections from that specific 3D region.

This is what enables "monitoring during ambulation":
1. Spatial isolation via FMCW + array.
2. Camera-supervised neural networks (RF-Pose family) trained on
   synchronized RGB cameras to predict body keypoints from RF
   reflections. ~100+ hours of synchronized data in the training corpus.
3. Motion-aware signal extraction: estimate gross motion (gait) from
   pose, regress it out, extract residual chest displacement for
   vital signs.
4. Long-term subject identification via gait + habit patterns for
   multi-person homes.

### Can ViFi reproduce this?

Honest answer: **not with this hardware.** The motion robustness is
downstream of the FMCW + array choice; the algorithms are downstream of
that. WiFi CSI on ESP32-S3 lacks the spatial resolution and antenna
count to do equivalent processing. You can make WiFi CSI more motion-
*tolerant* (motion gating, subspace decomposition, reference antennas —
see `docs/FUTURE_ARCHITECTURE.md`), but "walking around the house"
monitoring is a different product needing different hardware (FMCW or
UWB radar).

**ViFi's lane:** vital signs during rest and sleep, with subject
stationary or near-stationary. That's where WiFi CSI is competitive
and where the literature has the most prior art.

---

## Other comparables (briefly)

The following are positioned to highlight where ViFi sits in the space.
Not exhaustive.

- **Google Nest Hub (Soli radar)** — 60 GHz radar for in-bed
  sleep/breathing. Different band, similar lane to where ViFi could go.
  Consumer-grade, not clinical.
- **Sleepiz / Cardio-Inflammatoire / Equivital** — various wearable
  competitors. Not strictly comparable (wearable, not contactless).
- **Academic WiFi-CSI groups** — CMU (Romit Roy Choudhury), Northwestern
  (Aggelos Katsaggelos), Tsinghua, Beihang, Politecnico di Milano. Most
  publish open-source-ish prototypes; few have commercial products.
  These are the prior-art bedrock for ViFi's approach.

---

## IP risk posture

**Not legal advice. Treat as orientation; commission an FTO before
pilot launch.**

### MIT TLO / Emerald patent estate

MIT TLO holds the patents on the Katabi group's work; Emerald licenses
them exclusively for the commercial vertical. The patent estate likely
covers:

- Specific FMCW signal processing techniques for body monitoring
- Methods for camera-supervised RF pose estimation (the RF-Pose family)
- Specific neural network architectures applied to RF reflections
- Multi-person localization and identification via body RF reflections
- Through-wall and behind-wall sensing techniques

A non-exhaustive search of Google Patents / USPTO for the named
inventors and CSAIL assignees would surface the active claim language.

### Where ViFi is lower-risk

- **WiFi CSI on commodity ESP32 hardware.** Different physics, different
  band, dramatically less overlap with the Emerald patent estate.
- **Standard signal processing** (FFT, Butterworth, subspace
  decomposition, autocorrelation). Decades old; not patentable as such.
- **Vital signs extraction from CSI during stationary periods.** Covered
  by hundreds of academic papers across multiple groups; the general
  technique is broadly in the prior art.
- **Empty-room baselining and reference-channel subtraction.** Radar
  literature from the 1960s–70s; well outside any modern patent's
  novelty range.
- **The XGBoost-on-handcrafted-features pipeline.** Generic ML pattern;
  the 9-dim features are physically motivated and broadly published.

### Where ViFi would be higher-risk

- Using **camera supervision** to train ML models on RF data for pose
  or vital signs (the RF-Pose family approach). Patent likelihood high.
- **FMCW radar processing for vital signs during ambulation.** Would
  only apply if ViFi switched hardware to FMCW; not relevant on WiFi
  CSI.
- **Multi-person identification via RF body fingerprinting.** Likely
  covered.
- **Through-wall monitoring claims.**
- Marketing language that parallels Emerald's product positioning even
  when the underlying implementation is prior art — invites scrutiny.

### Recommended IP risk-management

1. **Stay in WiFi CSI hardware for the foreseeable future.**
2. **Don't market "monitor during ambulation"** — that's Emerald's
   differentiator. ViFi's lane is rest / sleep / stationary.
3. **Commission an FTO search** before clinical pilot launch and again
   before commercial release. Cost: ~$5K–15K for a focused search.
   What an FTO delivers: active patents in the space, claim-by-claim
   read-on analysis vs ViFi's implementation, recommended design-
   arounds.
4. **Keep clean lab notebooks / commit history** establishing
   independent development. Prior-art defense relies on demonstrating
   independent derivation.
5. **Cite prior art aggressively** in any publications or patents ViFi
   files. Establishes that the techniques used are not novel-to-them.
6. **Be especially careful with any RF-Pose-adjacent work.** If ViFi
   ever adds camera supervision (training on synchronized RGB-D + CSI
   data), get IP counsel involved before any implementation work.

---

## What this means for the architecture

The `docs/FUTURE_ARCHITECTURE.md` roadmap is deliberately staying in
the WiFi-CSI lane. None of the proposed additions (rolling-PCA, EMA
calibration, reference antenna, multi-subcarrier reference, domain-
adversarial training, masked-autoencoding pre-training) involve FMCW
processing or camera supervision. All are within the published
academic prior art for WiFi-CSI vital signs.

If ViFi ever wants to claim motion-during-ambulation capability, that
will require:
1. A hardware transition (FMCW or UWB radar)
2. A proper FTO conducted on the specific new methods
3. A different go-to-market position than current

For pre-pilot and pilot, **stay in the lane**. The technique space is
rich enough to reach clinical-grade rest/sleep monitoring without ever
brushing the Emerald patent estate.
