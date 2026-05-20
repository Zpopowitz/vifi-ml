# ViFi v2 — Phase 0 Research Notes

> **What this is:** the findings from Phase 0 of the radar v2 plan — the
> "while the board ships, no hardware" research phase. It exists so the build
> is not blocked on guesswork when the IWRL6432BOOST arrives.
>
> **Companion docs:** the architecture and phased roadmap live in
> `docs/superpowers/plans/2026-05-20-radar-v2-architecture.md`; the strategic
> "who needs this" question is answered in `docs/RADAR_DEMAND_THESIS.md`.
>
> **Compiled 2026-05-20** from two sourced research sweeps (TI hardware/SDK;
> FMCW DSP + per-beat ML literature). Every non-obvious claim has a source
> link in §7.

---

## 0. Phase 0 checklist status

Mirrors the radar plan §3 Phase 0 list:

| Item | Status | Where |
|---|---|---|
| Order the FTDI C232HM-DDHSL-0 USB-to-SPI cable | ✅ done | owner ordered 2026-05-20 |
| Confirm the raw-data path from TI docs | ✅ done | §1 below |
| Install the TI toolchain | ✅ done | §2 — installed in WSL2, verified by a build |
| Study the FMCW vital-signs model | ✅ done | §3 below |
| Read the per-beat ML references | ✅ done | §4 below |
| Write the one-page demand thesis | ✅ done | `docs/RADAR_DEMAND_THESIS.md` |

All six Phase 0 items are closed. §2 is the as-built record of the toolchain
install; the rest are research findings. Phase 0 ends when the board arrives and
Phase 1a (Gate 0 smoke test) begins.

---

## 1. Hardware and the raw-data path

### The core fact

The IWRL6432BOOST's USB port is **UART only, and UART carries only processed
TLV output** — point cloud, range profile, tracker output. There is **no
raw-ADC or range-cube path over UART or USB**. The board has **no onboard
FTDI/SPI bridge chip** (TI's own doc: *"FCCSP EVM does not have on board SPI
FTDI chip. User has to use external converter cable."*).

Two — and only two — ways to get raw data off the board:

1. **SPI streaming** — push raw ADC or the 1D range cube out the device SPI
   port during frame idle time. Needs an external USB-to-SPI cable. **This is
   our path** (the cable is ordered).
2. **DCA1000EVM + RDIF** — raw ADC over the 60-pin connector to a ~$500
   capture card. Not pursued; no DCA1000 in the plan.

### The cable — confirmed correct

The **FTDI C232HM-DDHSL-0** is the exact cable TI uses in its own raw-ADC
streaming walkthrough. It is an FT232H-based USB-to-MPSSE cable; here the
FT232H is SPI **master**, the radar is SPI **slave**, with a busy/handshake
line for sync.

- **Voltage:** the `-DDHSL-0` suffix is the **3.3 V** variant (the 5 V sibling
  is `C232HM-EDHSL-0`). Confirm the ordered part is **DDHSL**, not EDHSL.
- All evidence (TI wires the 3.3 V cable straight to the BOOST SPI header with
  no level shifter; the `loeens` repo does the same) says the BOOST runs 3.3 V
  digital IO and the cable matches. **Still unverified in a plain TI spec line
  — confirm SPI IO = 3.3 V on the BOOST schematic (page 5) before connecting.**

### Wiring map

TI's setup PDF and the `loeens` firmware README agree exactly:

| IWRL6432 SPI signal | C232HM-DDHSL-0 wire | Note |
|---|---|---|
| MOSI | **yellow** | cable → radar |
| MISO | **green** | radar → cable (the data) |
| Chip Select (CSN) | **brown** | |
| SPI Clock (SCLK) | **orange** | |
| SPI BUSY / data-ready | **grey** | TI net `DCA_LP_HOST_INTR_1`; syncs master to slave |
| GND | **black** | |
| 3.3 V (red) | — | **leave disconnected** — board is USB-powered; do not back-feed the rail |

The `loeens` repo names the SPI header **J2**; TI's PDF shows it only in a
photo without a text designator. **Confirm the header designator and pin
order against the board silkscreen before wiring.**

### Firmware — a real decision before the board arrives

Two firmware paths produce raw SPI data, and they differ entirely (firmware,
host script, post-processing). **Pick one before flashing day:**

| | Path A — TI Motion-and-Presence demo | Path B — `loeens` firmware |
|---|---|---|
| Output | **raw ADC samples** | **post-range-FFT range cube** (per-bin complex IQ) |
| Host parser | TI `adcDataSPIFTDI` → `adcdata.txt` → MATLAB | Python (`pyftdi`/`numpy`/`xarray`) |
| Setup cost | SysConfig toggle + a documented `dpc.c` build patch | minimal firmware, static config in `defines.h` |
| Maturity | official TI, supported | community fork, university-derived, 0–2 stars |
| Fit for ViFi | raw ADC = do range-FFT ourselves | **range cube is the more direct artifact for a phase-based HR pipeline** |

**Leaning Path B** — the range cube (per-range-bin complex IQ) is exactly what
the DSP chain in §3 consumes, so Path B skips re-implementing the range FFT.
Path A is the safer/officially-supported fallback. Decide before flashing.

Either path requires:
- `lowPowerCfg 0` (low-power mode **OFF**) — raw streaming is **incompatible**
  with low-power mode, so it is incompatible with TI's headline low-power
  vital-signs demo. You run a raw-capture demo *or* the low-power vitals demo,
  not both.
- **MMWAVE-L-SDK ≥ 5.4** for SPI ADC streaming on the BOOST (FCCSP package).
- Path A also needs `adcLogging 2` and the SysConfig "ADC Streaming via SPI"
  toggle.

TI's own **vital-signs demo** outputs only processed averaged HR/RR and does
**not** stream raw data — treat it as a processed-output reference (and the
Phase 1a Gate 0 smoke test), not a raw-data source.

### DIP switches / SOP / boot mode

- **SOP (latched at reset):** functional mode = SOP0:1 / SOP1:0; flashing mode
  = SOP0:0 / SOP1:0. **Power-cycle when switching modes.**
- **S1 for SPI capture:** S1.1 = ON (debug), S1.6 = ON (SPI + busy GPIO), all
  other S1 = OFF. S1.5 must route the muxed pins to **SPI, not CAN-FD**.
- Exact rocker positions are in the EVM user guide (SWRU596) only as photos —
  verify against the physical silkscreen.

### Damage risk — the one that matters

SWRU596: *all digital IO pins except NRESET are **non-failsafe** — do not drive
them without VIO present.* Practical rule: **power the BOOST over USB first,
then connect the FTDI signal leads.** Never drive SCLK/MOSI/CS from a powered
cable into an unpowered board, and leave the cable's 3.3 V lead disconnected.

### Reference repos (all verified to exist, dates as of 2026-05-20)

| Repo | Last push | Provides |
|---|---|---|
| `loeens/ti_iwrl6432_spi_data_stream` | 2025-09-04 | firmware — streams the range cube over SPI; README has wiring + switch map |
| `loeens/mmwave-spi-ftdi-reader` | 2025-06-08 | host Python reader for the range cube over the C232HM cable |
| `loeens/xWRL6432-adc-reader` | 2025-06-27 | host reader for raw ADC via **DCA1000** (not SPI) |
| `95lux/ti_iwrl6432boost_dsp` | 2025-05-03 | upstream of the `loeens` firmware |

### Open hardware questions — confirm with board in hand

1. SPI header designator + pin numbering (`loeens` says J2; verify on silkscreen).
2. BOOST digital-IO rail = 3.3 V (all evidence says yes; confirm on schematic p.5).
3. Exact S1/S4/SOP rocker positions (SWRU596 photos).
4. Which SDK version the `loeens` firmware targets (not in its README — check before building, match SDK version or budget porting time).

---

## 2. TI toolchain — installed and verified (Phase 0 item 3 — DONE)

**Status: installed in WSL2 (Ubuntu 24.04) and verified 2026-05-20** by building
a stock SDK example end to end. Host decision: WSL2 Linux, not the Windows host.
The toolchain lives under `~/ti/`.

### As-built — what is installed

| Tool | Version installed | Path |
|---|---|---|
| MMWAVE-L-SDK | 05.05.04.02 | `~/ti/MMWAVE_L_SDK_05_05_04_02` |
| Code Composer Studio | 20.5.1 (mmWave family only) | `~/ti/ccs2051` |
| TI Arm-CLANG compiler | 4.0.4.LTS | bundled in CCS: `~/ti/ccs2051/ccs/tools/compiler/ti-cgt-armllvm_4.0.4.LTS` |
| SysConfig | 1.27.1 | `~/ti/sysconfig_1.27.1` (also bundled in CCS) |
| Uniflash | 9.5.0 | `~/ti/uniflash_9.5.0` |
| mono | 6.8.0 | system (`apt`) — required for the SDK boot-image step |

The IWRL6432 uses the **MMWAVE-L-SDK** ("L" = low-power), not the classic mmWave
SDK. TI's download pages serve only the *latest* of each tool, so the installed
versions are newer than the SDK's reference toolset — `imports.mak` was pinned
to CCS 12.7.1 / compiler 3.2.2.LTS / SysConfig 1.20.0. **The drift turned out
not to matter**; see the verification build below.

### Install procedure as run (reproducible)

All four tools download from ti.com behind a `myti.ti.com` login + export-control
acceptance (Linux installers), then installed unattended:

1. **Privileged prerequisites** (`sudo`, once):
   - `sudo apt-get install -y mono-complete` — the SDK boot-image step
     (`out2rprc.exe`) runs under mono; without it the build fails `mono: not found`.
   - `sudo dpkg --add-architecture i386 && sudo apt-get update && sudo apt-get
     install -y libc6:i386 libstdc++6:i386 zlib1g:i386` — **the MMWAVE-L-SDK
     installer is a 32-bit binary** (`Linux-x86`); a 64-bit-only WSL2 cannot run
     it without i386 multiarch + 32-bit libc. CCS / SysConfig / Uniflash
     installers are 64-bit and need none of this.
2. **Unattended installs** (no display server needed), each into `~/ti/`:
   - `sysconfig-*-setup.run --mode unattended --prefix ~/ti/sysconfig_1.27.1`
   - `uniflash_*.run --mode unattended --prefix ~/ti/uniflash_9.5.0`
   - `ccs_setup_*.run --mode unattended --enable-components PF_MMWAVE --prefix ~/ti/ccs2051`
     (`--enable-components PF_MMWAVE` skips ~16 device families we do not need)
   - `MMWAVE_L_SDK_*.bin --mode unattended --prefix ~/ti` (creates its own
     `MMWAVE_L_SDK_<ver>` subdir)

### SDK configuration — `imports.mak` edits

The SDK build reads `~/ti/MMWAVE_L_SDK_05_05_04_02/imports.mak`, which is
version-pinned. Three edits point it at the as-built toolchain:

| Variable | Pinned default | Edited to |
|---|---|---|
| `CCS_PATH` | `$(TOOLS_PATH)/ccs1271/ccs` | `$(TOOLS_PATH)/ccs2051/ccs` |
| `CGT_TI_ARM_CLANG_PATH` | `ti-cgt-armllvm_3.2.2.LTS` | `ti-cgt-armllvm_4.0.4.LTS` |
| `SYSCFG_PATH` | `$(TOOLS_PATH)/sysconfig_1.20.0` | `$(TOOLS_PATH)/sysconfig_1.27.1` |

`imports.mak` lives inside the installed SDK — redo these edits if the SDK is
ever reinstalled. `CCS_ECLIPSE` (`$(CCS_PATH)/eclipse/eclipse`) does not resolve
(CCS 20 is Theia-based), but command-line `make` builds do not use it.

### Verification build

Built `examples/empty/xwrL64xx-evm/m4fss0-0_nortos/ti-arm-clang` with `make`.
The full chain succeeded: SysConfig 1.27.1 generated the config code with no
version error, tiarmclang 4.0.4 compiled clean under `-Werror`, the link against
the SDK's prebuilt 3.2.2-era libraries succeeded (so the compiler-version gap is
a non-issue), and mono produced `empty.release.appimage` — a flashable boot
image. The toolchain builds IWRL6432 firmware today.

### Deferred — board-gated, one `sudo` round-trip when the BOOST arrives

None of these block *building* firmware; all are needed to *flash / debug*:

1. **Uniflash runtime libs.** Uniflash flagged missing `libusb-0.1.so.4`,
   `libusb-1.0.so.0`, `libcanberra.so.0`, `libgconf-2.so.4`. Note `libgconf-2-4`
   may not be in the Ubuntu 24.04 repos (GConf is deprecated) — confirm at flash
   time; Uniflash CLI flashing may not need it.
2. **CCS USB-driver script.** `~/ti/ccs2051/ccs/install_scripts/install_drivers.sh`
   (XDS110 udev rules) must run as root before CCS / Uniflash can see the board.
3. **WSL2 USB passthrough.** Reaching the BOOST's USB from WSL2 needs
   `usbipd-win` on the Windows side — set up with the board in hand.

### Still-open pre-board actions (not toolchain)

- `pyftdi` for the host capture tool: on WSL2 it works natively (Linux is its
  primary platform) but needs a udev rule and the same `usbipd-win` passthrough;
  the Windows-only Zadig driver swap does not apply.
- Download the BOOST schematic; confirm SPI IO = 3.3 V on page 5 (§1).
- Decide firmware Path A vs Path B (§1) before flashing day.

---

## 3. FMCW vital-signs DSP model

### The processing chain — every stage is mandatory

Raw IQ → chest-displacement waveform:

1. **Range FFT** — FFT per chirp across fast-time samples → a range profile
   (one complex value per ~4 cm bin). Separates the chest from clutter at
   other ranges.
2. **Static clutter removal (MTI)** — subtract the slow-time mean (zero-phase,
   needs a full buffer) or a first-order IIR high-pass (streaming, adds phase
   lag). The static wall/torso return is a huge near-DC vector; without MTI the
   ~0.2–0.5 mm cardiac swing is buried under it.
3. **Range-bin selection + TRACKING** — the chest bin drifts as the subject
   sways/breathes. Track it frame-to-frame; a **fixed** bin gives step
   discontinuities and spurious "beats" every time the target energy migrates
   to a neighbour. This is one of the two most-skipped, most-damaging stages.
4. **DC-offset removal (NLLS circle-fit)** — the IQ samples trace an arc that
   residual clutter + IF imbalance push off-origin. `atan2` measures angle
   *from the origin*, so an off-centre arc makes the phase-vs-displacement map
   nonlinear → **it manufactures spurious respiration harmonics in the cardiac
   band.** Geometric NLLS circle-fit is the best estimator at 60 GHz. The
   second most-skipped, most-damaging stage.
5. **Phase demodulation — DACM, not plain arctangent.** Extended DACM
   (differentiate-and-cross-multiply, then integrate) never wraps. Plain
   arctangent + unwrap mis-counts a 2π cycle on any fast/noisy sample and
   offsets the whole downstream trace.
6. **Unwrap** (if using arctangent) → band-split into respiration (0.1–0.5 Hz)
   and cardiac (~0.8–2.5 Hz).

**Takeaway for the `radar/` module:** stages 3 (tracking) and 4 (circle-fit)
are the ones whose failure injects fake cardiac-band content. Implement the
full chain before any ML.

### The respiration-harmonic problem — the central technical risk

Breathing displacement (~4–12 mm) dwarfs the cardiac component (~0.2–0.5 mm),
and breathing is non-sinusoidal, so its **4th–8th harmonics land at
0.8–2.5 Hz — inside the cardiac band.** This is **geometric, not SNR-limited**:
more transmit power raises the cardiac signal and the respiration harmonics
equally. A sloppy DSP chain (stages 4–5) *manufactures extra* harmonics on top.

Mitigations, baseline → upgrade:
- **Adaptive harmonic-comb notch** keyed to the live respiration fundamental
  (notch 4·f_r, 5·f_r, …). Baseline. Must be adaptive — f_r drifts.
- **Second-derivative + VME** (arXiv 2407.07380): the |2nd derivative| of the
  complex signal weights high frequencies by ω², so cardiac higher harmonics
  outrun respiratory ones in the upper band; Variational Mode Extraction then
  isolates the heartbeat. Cut IBI RMSE 34→26 ms (−23%) and raised coverage
  69%→88% on 79 GHz radar. Also skips phase unwrapping. **The strongest
  classical lever — confirms the plan's call to use 2407.07380.**
- VMD/EEMD decomposition; adaptive RLS/DR-MUSIC; deep-learning separation.

### Chirp configuration — a refinement to the plan

The frame rate **is** the displacement-waveform sampling rate (one sample per
frame). The plan §3 Phase 1b currently says "≥30–50 Hz." **Phase 0 research
revises this up:**

- ~20 Hz (TI OOB demo) quantizes IBI to ±50 ms — fails the ≤30 ms target.
- 30 fps still produced weak HRV in the literature; MMECG samples at 200 Hz.
- **Target ~100 Hz frame rate** (10 ms slow-time resolution, ~3× margin on the
  30 ms IBI target, headroom for peak interpolation).
- **Sweep bandwidth ≈ 3.75 GHz** for ~4 cm range bins (the IWRL6432's 57–64 GHz
  band gives ~7 GHz available — plenty).
- For per-beat work prefer **one TX/RX pair at a high frame rate** over many
  chirps per frame. Watch the frame-idle-time budget: raw/cube data ships
  during idle time, so a too-short frame period or too-large cube overruns the
  SPI transfer (`loeens` real-time cap ≈ 96 KByte cubes).

The radar plan Phase 1b bullet has been updated to point here.

---

## 4. Per-beat ML references

### radarODE (arXiv 2408.01672, IEEE TMC 2025)

Reconstructs an actual **ECG waveform** from mmWave chest motion. Embeds an
**ODE as the decoder** — a parametric ECG-beat-shape prior that constrains
output to physiologically plausible morphology and improves robustness. vs.
benchmark: missed-detection −9%, RMSE −16%, correlation +19%. Captured on a TI
AWR1843 at 77 GHz. No code in the original paper; the successor
**radarODE-MTL** (arXiv 2410.08656) may have a repo.

### AirECG (ACM IMWUT, 2024)

Reconstructs ECG via a **cross-domain diffusion model** with a
calibration-guidance mechanism to suppress generative hallucination — and works
on **cardiac patients / abnormal morphology**, not just healthy subjects.
Pearson 0.955 normal / 0.860 abnormal. Code at
`github.com/LangchengZhao/AirECG` — **but no dataset, no weights, no radar
front-end** are released (the repo ships placeholder data). Radar hardware is
unspecified — a transfer-learning risk if reused on IWRL6432 data.

### Datasets

| Dataset | Size | Radar | Access / licence |
|---|---|---|---|
| **MMECG** (arXiv 2112.06639) | 35 subjects, ~10 h, 200 Hz cardiac, 4 physiological states | TI AWR1843, 77 GHz | `github.com/jinbochen0823/RCG2ECG`; only ~4.55 h released; licence unstated — confirm |
| **Age-Balanced 60 GHz** (Nature Sci Data, 10.1038/s41597-026-07172-9) | 110 subjects, 15 with cardiac conditions | **2× TI IWR6843, 60 GHz** | Zenodo **DOI 10.5281/zenodo.16760683**; **CC BY-NC-ND 4.0** |

**Recommendation:** pursue the **Age-Balanced 60 GHz** dataset first — fully
released, 60 GHz band matches the IWRL6432 (smallest transfer gap), 15 cardiac
cases. Use **MMECG** as the morphology-pretraining set despite partial release.
Licence caveats matter: Age-Balanced is non-commercial/no-derivatives (fine for
research + validation, restricted for a future commercial product); MMECG's
licence is unstated.

---

## 5. What Phase 0 changes or confirms in the radar plan

- **Confirms** the plan's hardware story: UART = processed only, SPI needed,
  the C232HM-DDHSL-0 is the right cable, `lowPowerCfg 0` required. Risk #1 in
  the plan stands and is now de-risked with a concrete wiring map + procedure.
- **Confirms** the plan's DSP design: MTI, DC-offset circle-fit, and DACM are
  all genuinely mandatory; arXiv 2407.07380 is the right harmonic-handling
  lever.
- **Refines** the chirp config: frame-rate target moves from "≥30–50 Hz" to
  **~100 Hz** (§3). Plan Phase 1b updated.
- **New decision surfaced:** Path A (raw ADC) vs Path B (range cube) firmware —
  not anticipated in the plan; leaning Path B (§1).
- **Confirms** the plan's honest framing of Gate 2: best classical-DSP IBI
  RMSE is ~15–26 ms on cooperative, still subjects, with HRV metrics still
  15–30% off. Clearing Gate 2 (F1 ≥ 0.9, IBI ≤ 30 ms) by **pure classical DSP
  is a genuine stretch** — the literature says so outright. Budget for Phase 3
  ML, and expect it to need a beat-shape prior (radarODE/AirECG style), not a
  generic regressor.
- **Datasets:** the plan named "MMECG" and "a 2026 Age-Balanced dataset" — now
  pinned to exact DOIs and licences (§4).

### Open decisions for the owner

1. **Firmware path A vs B** — recommend B (range cube). Decide before flashing.
2. **Frame rate** — adopt ~100 Hz as the Phase 1b chirp-config target.
3. **Dataset** — pull the Age-Balanced 60 GHz set (Zenodo) for Phase 3 prep;
   note the CC BY-NC-ND commercial restriction.

---

## 6. Sources

**TI hardware / SDK:**
- MMWAVE-L-SDK Motion-and-Presence demo — https://software-dl.ti.com/ra-processors/esd/MMWAVE-L-SDK/05_05_00_02/exports/api_guide_xwrL64xx/MOTION_AND_PRESENCE_DETECTION_DEMO.html
- IWRL6432BOOST EVM user guide SWRU596 — https://www.ti.com/lit/ug/swru596/swru596.pdf
- TI E2E "Steps for Raw ADC Data Streaming in IWRL6432" — https://e2e.ti.com/cfs-file/__key/communityserver-discussions-components-files/1023/Steps-for-Raw-ADC-Data-Streaming-in-IWRL6432.pdf
- TI E2E "IWRL6432BOOST: SPI ADC Streaming" — https://e2e.ti.com/support/sensors-group/sensors/f/sensors-forum/1372336/iwrl6432boost-spi-adc-streaming
- FTDI C232HM-DDHSL-0 product page — https://ftdichip.com/products/c232hm-ddhsl-0-2/
- MMWAVE-L-SDK download page — https://software-dl.ti.com/ra-processors/esd/MMWAVE-L-SDK/05_05_00_02/exports/api_guide_xwrL64xx/SDK_DOWNLOAD_PAGE.html
- `loeens/ti_iwrl6432_spi_data_stream` — https://github.com/loeens/ti_iwrl6432_spi_data_stream
- `loeens/mmwave-spi-ftdi-reader` — https://github.com/loeens/mmwave-spi-ftdi-reader

**FMCW DSP:**
- DC-offset / circle-fit at mmWave (Sensors) — https://pmc.ncbi.nlm.nih.gov/articles/PMC9781610/
- FMCW vital-signs DSP review (ACM TOSN) — https://dl.acm.org/doi/full/10.1145/3627161
- Higher-harmonic heartbeat measurement (arXiv 2407.07380) — https://arxiv.org/abs/2407.07380
- HRV-from-radar trade-off study (arXiv 2603.09791) — https://arxiv.org/html/2603.09791
- TI mmWave vital-signs lab user guide — https://e2e.ti.com/cfs-file/__key/communityserver-discussions-components-files/1023/vitalSigns_5F00_lab_5F00_user_5F00_guide_5F00_v1.2UPDATE.pdf
- TI chirp programming app note SWRA553A — https://www.ti.com/lit/an/swra553a/swra553a.pdf

**Per-beat ML + datasets:**
- radarODE (arXiv 2408.01672) — https://arxiv.org/abs/2408.01672
- radarODE-MTL (arXiv 2410.08656) — https://arxiv.org/pdf/2410.08656
- AirECG (ACM IMWUT, 10.1145/3678550) — https://dl.acm.org/doi/10.1145/3678550 ; code https://github.com/LangchengZhao/AirECG
- MMECG / RCG2ECG (arXiv 2112.06639) — https://arxiv.org/abs/2112.06639 ; code https://github.com/jinbochen0823/RCG2ECG
- Age-Balanced 60 GHz dataset (Nature Sci Data) — https://www.nature.com/articles/s41597-026-07172-9 ; data https://doi.org/10.5281/zenodo.16760683
