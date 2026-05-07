# Deployment

How to put ViFi in a real environment. Three deployment shapes, each
with hardware shopping list, network topology, and step-by-step
provisioning.

For HIPAA implications of each, see `HIPAA_PILOT_CHECKLIST.md`.
For the milestone-aligned view of which one fits when, see
`IMPLEMENTATION_PLAN.md`.

---

## Three deployment shapes

| Shape | When | Cost (1 room) | Cost (10 rooms) | Network |
|---|---|---|---|---|
| **Single-host (laptop / mini PC)** | Pre-pilot demo, single subject | $0 (your laptop) or ~$170 | n/a | LAN |
| **Edge boxes + central server** ⭐ | Multi-room pilot | ~$340 | ~$1.4K | LAN |
| **Edge boxes + cloud central** | Multi-clinic, post-pilot | ~$170 + cloud | ~$170 + cloud | Internet |

The middle option is what most clinical pilots want. ⭐ recommended
default for the first pilot.

---

## Shape 1 — Single-host (today's setup)

Everything runs on one machine. Good for demos, single-subject
research, building investor decks.

```
┌──────────────────────────────────────────────────────┐
│ Your laptop / a mini PC                              │
│   ESP32 USB ───┐                                     │
│   Polar BLE ───┤  ← hardware loggers (host scripts)  │
│   Vernier BLE ─┘                                     │
│                                                      │
│   docker compose up → Redis + API + workers + SPA    │
│                                                      │
│   Browser → http://localhost:8501 → live dashboard   │
└──────────────────────────────────────────────────────┘
```

**Provisioning:** see the README "Quick start." Your laptop or any
Linux/macOS host with Docker works.

---

## Shape 2 — Edge boxes + central server (recommended for pilot)

One small "edge" box per room, one central server for the whole
clinic. This is how real telemetry systems are built.

```
ROOM 1 ─┐ ┌─Pi 5 (2 GB) (edge)─────────────────┐
        │ │ ESP32 USB → csi_capture.py        │
        │ │ Polar BLE → hr_logger.py          │  bus traffic
        │ │ Vernier BLE → rr_logger.py        │ ─────────►┐
        │ │ All publish to redis://central:6379│           │
        │ └────────────────────────────────────┘           ▼
        │                                          ┌─────────────────┐
ROOM 2 ─┤ ┌─Pi 5───────────────────────────────┐   │ Central server  │
        │ │ Same loggers, patient_id=room-2    │ ─►│ (Intel N100)    │
        │ └────────────────────────────────────┘   │                 │
        │                                          │ Redis +         │
ROOM N ─┤ (...)                                    │ inference +     │
        │                                          │ audit + API +   │
        │                                          │ dashboard SPA + │
        │                                          │ login           │
        │                                          └────┬────────────┘
        │  ┌────────────────────────────┐                │ HTTPS
        └──┤ Travel router              │                │
           │ (or clinic VLAN)           │                ▼
           │ WiFi ESP32 + edge boxes    │       ┌─────────────────┐
           │ + central + clinician      │       │ Clinician       │
           └────────────────────────────┘       │ laptop / phone  │
                                                │ Login → SPA     │
                                                └─────────────────┘
```

### Hardware shopping list — 2-room demo

| Item | Qty | ~Cost | Notes |
|---|---|---|---|
| Beelink S12 Pro N100 mini PC (8 GB / 256 GB SSD) | 1 | $170 | Central server. Runs your existing x86 Docker images unchanged |
| Raspberry Pi 5 (2 GB) starter kit (CanaKit or Adafruit) | 2 | $95 each | Edge boxes; 2 GB RAM is enough — CSI capture + BLE forwarders peak ~700 MB |
| ESP32-S3-DevKitC-1U-N8R8 + external dipole antenna | 4 (2 TX + 2 RX) | ~$30 | One TX/RX pair per room |
| Polar H10 chest strap | 1+ | $90 each | Reference HR; share across rooms during validation |
| Vernier GDX-RB respiration belt | 1+ | $200 each | Reference RR; share across rooms during validation |
| GL.iNet GL-AX1800 travel router (OR use clinic LAN) | 1 | $50 | Dedicated ViFi WiFi |
| Cat6 Ethernet cable (router → central) | 1 | $10 | Wired beats WiFi for the central uplink |
| **Total (2-room demo)** | | **~$540** | excludes Vernier belts |

### Hardware shopping list — 10-room clinical pilot

| Item | Qty | Cost | Notes |
|---|---|---|---|
| Beelink S12 Pro N100 (central) | 1 | $170 | One per clinic floor (≤15 rooms) |
| Raspberry Pi 5 (2 GB) edge starter kit | 10 | $950 | One per room; bundles PSU + microSD + case + fan |
| ESP32-S3 pairs | 20 | $300 | One pair per room |
| Polar H10 | 1-3 | $90-270 | Shared across rooms during validation; not deployed in production |
| Vernier GDX-RB | 1-3 | $200-600 | Shared across rooms during validation; not deployed in production |
| Cisco Meraki MR-series AP (BAA-eligible) | 1-2 | $500-1000 | Replaces consumer router for HIPAA |
| **Total (10-room pilot validation)** | | **~$3K-3.4K** | + monitoring agreements |
| **Per-room marginal (production, post-validation)** | | **~$65** | ESP32 pair + Pi Zero 2W; refs removed |

For pre-pilot, the consumer travel router is fine. For real patients,
the AP must be from a vendor that signs a BAA — see
`HIPAA_PILOT_CHECKLIST.md`.

### Provisioning a central server

```bash
# 1. Fresh Ubuntu 22.04 / Debian 12 install (Server edition).
# 2. Install Docker.
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 3. Clone the repo.
git clone https://github.com/Zpopowitz/vifi-ml.git
cd vifi-ml

# 4. Generate all secrets in one go.
./tools/setup_keys.sh

# 5. Start the stack (no simulator on a real central server).
docker compose up -d

# 6. The central server is now reachable at:
#      http://<central-ip>:8000      API
#      http://<central-ip>:8501      Dashboard
#    Find <central-ip> with: hostname -I | awk '{print $1}'
```

Set `VIFI_AUTH_MODE=api_key` in `.env` for any non-LAN deployment.

### Provisioning an edge box

```bash
# 1. Fresh Raspberry Pi OS Lite (64-bit) install.
# 2. Install Docker (same one-liner).
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 3. Clone (only the loggers; no need for the full stack).
git clone https://github.com/Zpopowitz/vifi-ml.git
cd vifi-ml

# 4. Configure — point at the central server.
cat > .env <<EOF
VIFI_BUS_URL=redis://<central-ip>:6379/0
VIFI_PATIENT_ID=room-3
VIFI_PSEUDO_SALT=<paste from central .env>
EOF

# 5. Run the loggers as host processes (BLE + USB need direct access).
# csi_capture forwards ESP32 USB → bus:
python tools/csi_capture.py --port /dev/ttyUSB0 --bus --patient-id room-3 \
    --duration 0   # 0 = run forever

# In another terminal:
python hr_logger.py --address AA:BB:CC:DD:EE:FF --bus --patient-id room-3 \
    --duration 0

# (Vernier RR logger when belt is connected; same pattern.)
```

These can be wrapped in systemd units for auto-start on boot. A
`scripts/setup-edge.sh` is on the M2 backlog (gated on actual
hardware).

### Network topology options

#### Option A: Dedicated travel router (simplest pre-pilot)

- Router = `vifi-net.local` SSID
- Central server: wired Ethernet to router, gets static IP `192.168.8.10`
- Edge boxes: WiFi to router
- Clinician laptop: WiFi to router
- ESP32 TX/RX pair: a different WiFi channel on the same router (or a separate AP) to avoid interfering with the data-plane traffic

Pros: zero clinic IT involvement, full control.
Cons: no BAA from router vendor → not OK for real patient data.

#### Option B: Clinic VLAN (HIPAA-compliant pilot)

- Clinic IT puts ViFi devices on a dedicated VLAN
- Devices reach each other but NOT the general clinic LAN
- BAA covered by clinic's existing IT infrastructure agreements
- Static DHCP reservations per device (MAC → IP)

Pros: HIPAA story is "the clinic's network is HIPAA-compliant; we sit on it."
Cons: needs clinic IT cooperation; lead time of weeks.

#### Option C: Hybrid (Tailscale / WireGuard mesh)

- Edge boxes + central server join a private VPN mesh
- No public IP exposure
- Works across clinics

Pros: scales to multi-site; no public attack surface.
Cons: needs Tailscale account + setup; more moving parts.

---

## Shape 3 — Edge + cloud central (post-pilot, multi-clinic)

The central server moves to the cloud. Edge boxes push to a managed
Redis (e.g., AWS ElastiCache, Redis Cloud). Dashboard becomes a
public URL with login.

```
ROOM 1 ──Pi──┐
ROOM 2 ──Pi──┤  ─────────────────►  Cloud Redis  ──►  Cloud API
ROOM N ──Pi──┘                                       Cloud SPA
                                                      Auth0 login
                                                      ▲
                                                      │
                                              Clinician anywhere
                                              with internet
```

This is the M3 architecture in `IMPLEMENTATION_PLAN.md`. Requires:

- BAA with cloud provider (AWS / GCP / Azure) — included with their
  HIPAA-eligible-services list
- Auth0 or equivalent for clinician login (BAA included)
- Datadog / similar for observability with BAA
- TLS everywhere (Caddy or AWS ALB + ACM)
- Multi-tenancy isolation if more than one clinic shares the central

Cost (rough): $50-200/month for the cloud infrastructure at low
volume; scales with patients monitored.

---

## Backup + recovery

Per-deployment-shape:

| Shape | Audit log backup | Disaster recovery |
|---|---|---|
| Single-host | Manual `cp data/audit/ /backup/` | Re-run last training; replay audit |
| Edge + central | Cron-archive central's `audit_data` to clinic NAS | Reinstall central from script in <2 hours |
| Edge + cloud | S3 Object Lock with 6-year retention | Cloud provider's redundancy + multi-AZ |

See `docs/DR.md` for the full recovery procedures.

---

## Deployment checklist (pre-pilot)

Before bringing the system to a clinical site even for a non-patient
demo:

- [ ] All hardware tested in your lab (catch DOA components)
- [ ] `setup_keys.sh` run on the central server; `.env` backed up
- [ ] Edge boxes provisioned with the right `VIFI_PATIENT_ID` per room
- [ ] Travel router (or clinic VLAN) configured with WPA2-PSK or stronger
- [ ] Central server reachable from each edge over the network (`ping`,
      `redis-cli ping`)
- [ ] First end-to-end smoke test: simulator on edge → central
      dashboard shows that room → reference HR via Polar shows
- [ ] Login form works (when implemented; M2)
- [ ] All disks encrypted (see HIPAA checklist)
- [ ] CHANGELOG entry for the deployment configuration

After demo, capture the deployment in `docs/sessions/<date>.md` so
the next site has a working template.

---

## Common deployment problems + fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard shows "Disconnected" | Edge can't reach central's Redis | `redis-cli -h <central-ip> ping` from the edge; check firewall |
| Predictions arrive but lag 30+ seconds | Central CPU saturated | Check `docker stats`; might need to scale inference workers |
| Polar drops out every few minutes | Pi BLE radio range / interference | Move edge box closer to the patient; consider USB BLE adapter |
| Audit log shows duplicate records | Worker restart between read + ack (expected with at-least-once) | Operators should dedupe by `msg_id` at query time; not a bug |
| `vifi.local` doesn't resolve | mDNS not installed or blocked by network | Use the central's IP address directly; or `apt install avahi-daemon` |
| Multiple rooms show same data | All edge boxes have `VIFI_PATIENT_ID=default` | Set per-room patient_id in each box's `.env` |

---

## What's next on the deployment roadmap

From `IMPLEMENTATION_PLAN.md`:

| Item | Status | When |
|---|---|---|
| `scripts/setup-edge.sh` | TBD | Once we have a Pi to test on |
| Multi-arch Docker (ARM64 for Pi) | TBD | When per-room boxes are Pis |
| Login form on SPA | TBD | M2 (gated on UX decisions) |
| Room dropdown in dashboard | TBD | M2 (gated on multi-room hardware) |
| `tools/setup-central.sh` | Available | Use today |
| `tools/setup_keys.sh` | Available | Use today |
| `tools/audit_query.py` | Available | Use today |

---

## Validation phase vs production deployment

The cost story has two distinct phases. Don't conflate them.

### Validation phase (you are here)

- **Goal**: collect paired CSI + reference HR + reference RR across
  4-5 sessions per subject, multiple subjects, to retrain models on
  real data and quantify cross-session MAE.
- **Hardware per room**: ESP32 pair + Pi 5 (2 GB) + temporary Polar
  H10 + temporary Vernier GDX-RB.
- **Per-room cost**: ~$140 (with shared refs) to ~$340 (dedicated refs).
- **Duration**: weeks to a few months per subject.
- **Why the Pi at edge**: BLE references can't run on the ESP32; you
  need a host with BLE radios for Polar + Vernier alongside USB serial
  for the ESP32 RX.

### Production deployment (post-pilot, what ships)

- **Goal**: monitor real patients with HR + RR predicted from CSI alone.
  No reference belts on the patient.
- **Hardware per room**: ESP32 pair + Pi Zero 2W (USB host).
- **Per-room cost**: ~$65.
- **Why the Pi Zero 2W is enough**: only doing USB serial → Redis
  forwarding. No BLE, no inference, no UI. 512 MB RAM and one core
  handle that with room to spare.
- **Why not ESP32 standalone**: the ESP32 *does* have WiFi, and
  `tools/esp32_csi_collector.py` is already a UDP listener variant,
  but using the same WiFi for both sensing and uplink is firmware-
  fragile in practice. A $15 USB host is the cleaner separation.

The headline "$50 of ESP32-S3 hardware" stays true — that's the
sensor BOM. $65/room is the realistic deployed cost including the
host. The validation phase Pi 5 is a one-time spend per pilot site,
not a per-room operational cost.

---

## Capture methodology — controlling room geometry

WiFi CSI is sensitive to room geometry, antenna orientation, and
subject position. The corpus is only useful if those are nailed down
across sessions.

### What matters most (in order)

1. **Antenna orientation + polarization match.** Co-polarize TX and
   RX antennas; lock them so they don't flop between sessions. This
   is the single biggest source of avoidable noise.
2. **Antenna height ≈ chest level.** You're sensing chest motion;
   antennas need to "see" it. Floor or ceiling-mounted is a different
   problem.
3. **Subject in the Fresnel zone.** Somewhere between TX and RX,
   chest in the propagation path. Within that zone, ±50 cm is
   forgiving; outside it, signal vanishes.
4. **Static multipath stability.** Fan running, person walking by, a
   door opening — all of those move the multipath baseline. Note
   anything that changes in the session `notes.txt`.

### What matters less than you'd think

- Exact board placement. ±20-30 cm is fine within the same room; the
  static multipath shifts but the modulation from breathing is still
  there. `calibration.py` per-session calibration absorbs the bias.

### Securing antenna orientation

The ESP32-S3-DevKitC-1U has a U.FL connector with a short pigtail to
an external dipole antenna. The pigtail joint is what flops around.

**Cheap, reversible (recommended for validation):**

- **Velcro hook-and-loop strips** ($5/roll). One half on the antenna
  body, one on a foam-board / plywood / 3D-print jig. Locks
  orientation but lets you pull off to reposition.
- **Cable ties on a wall bracket.** Locks orientation, ~$2.

**Semi-permanent (recommended once geometry is locked):**

- **3D-printed antenna cradle** screwed to a wall mount. STL files
  available for SMA/U.FL antennas; JLC3DP prints for ~$3/unit.
- **Heat-shrink tubing over the U.FL pigtail joint.** Locks the fold
  angle without committing to a wall mount.

**Production:**

- **Patch antenna with magnetic or screw mount.** Replaces the dipole
  entirely. Patch antennas have a directional pattern that's actually
  better for sensing a single subject in a Fresnel zone, and they
  don't need orientation jigs.

### Validation session protocol

#### Floor layout — controlled-geometry SOP

The single biggest avoidable source of cross-session MAE drift is the
subject sitting in a different position relative to the TX-RX line.
Empirical case from the founder corpus: session4 had a 7.2 bpm
held-out HR MAE while sessions 3 and 5 sat at 2.6 bpm — the only
visible difference was a ~10 Hz drop in CSI packet rate, consistent
with the subject having shifted off-axis between captures. Don't
guess at this; mark it.

```
       ◯ TX                 X (subject)                 ◯ RX
       │                    │                           │
       └────────d_TX────────┴──────d_RX─────────────────┘

       Total:  d_TX + d_RX = TX-RX distance
       Goal:   d_TX ≈ d_RX (subject at the midpoint)
       Goal:   subject ON the TX-RX line (subject_on_axis = True)
       Goal:   chest perpendicular to the TX-RX axis
```

Day-1 setup, once per room (never move it again):

1. Pick TX and RX positions. Mark each on the floor with painter's
   tape. Both at chest-height when seated (~110 cm).
2. Measure TX→RX distance. Record as `tx_rx_distance_m`.
3. Mark the **midpoint X** between TX and RX — that's where the
   subject sits.
4. Mark the chair's footprint at that X. Same chair, same height,
   same orientation, every session.
5. Lock the antenna orientation with velcro or a 3D-printed cradle
   (see `Antenna mounting` above). Once locked, the antenna
   should never wiggle again.

Per-session, do every time:

1. Strap on belts. Sit at the marked X position, on the TX-RX line.
2. Run the orchestrator with the geometry flags set:

   ```bash
   python tools/run_paired_session.py \
       --subject-id founder --room-id home_office \
       --posture seated --csi-port COM6 \
       --h10-address AA:BB:CC:DD:EE:FF \
       --duration 600 \
       --tx-rx-distance-m 2.0 \
       --subject-to-tx-distance-m 1.0 \
       --subject-on-axis true \
       --antenna-type external_dipole \
       --antenna-height-cm 110 \
       --notes "session1 baseline"
   ```

   Those geometry flags get persisted into `session.json` so
   `eval_harness` can later stratify MAE by distance / on-axis,
   and so future-you can debug a session4-style failure
   retrospectively. Use `python tools/validate_session_metadata.py
   --strict` to verify the geometry fields landed.

3. **Vary one thing per session:**

   | Session | Posture | Activity | Notes |
   |---|---|---|---|
   | 1 | Seated, upright | Still, screen work | Baseline |
   | 2 | Lying supine | Still | Chest-down profile |
   | 3 | Seated | Post-walk (5 min) | Elevated baseline |
   | 4 | Standing | Still | Vertical chest motion |
   | 5 | Seated | Reading + talking | Speech artifact |

4. **Annotate `notes.txt`** in each session directory if you want
   free-text observations beyond what the schema captures.

Cross-room generalization is its own experiment, in M3. Don't change
rooms during the M2 paired-capture corpus.

---

## Recommended Pi 5 SKU

Single SKU: **Raspberry Pi 5 (2 GB RAM)**. Reputable buy paths:

| Store | SKU | Price | Notes |
|---|---|---|---|
| **CanaKit** (US) | "Raspberry Pi 5 - 2GB" | $50 | Most reliable US stock |
| **Adafruit** | Product 5812 | $50 | Same board, ships fast |
| **PiShop.us** | "Raspberry Pi 5 (2 GB)" | $50 | Often has stock when others don't |
| **DigiKey** | SC1112 | $50 | Already a DigiKey customer? Easy |

Plus accessories (or buy a kit that bundles them):

| Item | Cost | Notes |
|---|---|---|
| Official Pi 5 PSU (USB-C, 27 W / 5V/5A) | $12 | Don't skimp — Pi 5 brown-outs USB devices on weaker PSUs |
| microSD (32 GB, A2 class) | $8 | SanDisk Extreme or Samsung Pro Endurance |
| Case with active cooling fan | $15 | Pi 5 thermal-throttles without one under sustained load |

**Easiest single buy**: CanaKit "Raspberry Pi 5 Starter Kit (2 GB)"
bundles all of the above for ~$95. Recommended.

### Why 2 GB is enough

The combined load on the edge box during validation:

| Process | Resident memory | What it does |
|---|---|---|
| `csi_capture.py` | ~80-120 MB | USB serial → Redis publish |
| `hr_logger.py` | ~60-80 MB | Polar BLE → Redis |
| `rr_logger.py` | ~80-120 MB | Vernier BLE → Redis (numpy for force FFT) |
| Raspberry Pi OS Lite | ~150-250 MB | base OS |
| Headroom (BLE stack + buffers) | ~200 MB | |
| **Total** | **~600-800 MB** | |

2 GB Pi 5 leaves ~1.2 GB free at peak. Bump to 4 GB only if you
later add edge inference (XGBoost predict + 30-sec CSI buffer) or
multi-patient per room.
