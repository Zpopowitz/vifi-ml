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
| **Edge boxes + central server** ⭐ | Multi-room pilot | ~$320 | ~$470 | LAN |
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
ROOM 1 ─┐ ┌─Pi 4 / Pi Zero 2W (edge)──────────┐
        │ │ ESP32 USB → csi_capture.py        │
        │ │ Polar BLE → hr_logger.py          │  bus traffic
        │ │ Vernier BLE → rr_logger.py        │ ─────────►┐
        │ │ All publish to redis://central:6379│           │
        │ └────────────────────────────────────┘           ▼
        │                                          ┌─────────────────┐
ROOM 2 ─┤ ┌─Pi 4───────────────────────────────┐   │ Central server  │
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
| Raspberry Pi 4 (2 GB) starter kit | 2 | $80 each | Edge boxes; full-size USB for ESP32 |
| ESP32-S3-DevKitC-1U-N8R8 + antenna | 4 (2 TX + 2 RX) | ~$30 | One TX/RX pair per room |
| Polar H10 chest strap | 1+ | $90 each | Reference HR; can share between rooms during testing |
| Vernier GDX-RB respiration belt | 1+ | $200 each | Reference RR (when M1 RR captures begin) |
| GL.iNet GL-AX1800 travel router (OR use clinic LAN) | 1 | $50 | Dedicated ViFi WiFi |
| Cat6 Ethernet cable (router → central) | 1 | $10 | Wired beats WiFi for the central uplink |
| **Total (2-room demo)** | | **~$510** | excludes Vernier belts |

### Hardware shopping list — 10-room clinical pilot

| Item | Qty | Cost | Notes |
|---|---|---|---|
| Beelink S12 Pro N100 (central) | 1 | $170 | One per clinic floor |
| Raspberry Pi 4 (2 GB) edge | 10 | $800 | One per room |
| ESP32-S3 pairs | 20 | $300 | One pair per room |
| Polar H10 | 10 | $900 | One per active patient |
| Vernier GDX-RB (when ready) | 10 | $2000 | Same |
| Cisco Meraki MR-series AP (BAA-eligible) | 1-2 | $500-1000 | Replaces consumer router for HIPAA |
| **Total (10-room pilot)** | | **~$5K** | + monitoring agreements |

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
