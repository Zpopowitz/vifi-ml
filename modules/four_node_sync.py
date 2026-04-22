"""4-receiver ESP32-S3 array for multi-patient rooms + room-wide coverage.

Hardware plan:
    4x ESP32-S3-DevKitC-1U-N8R8 nodes, one per corner of the room,
    each with an external dual-band 2.4/5 GHz antenna via IPEX1 -> RP-SMA
    pigtail. Single transmitter (one extra node or an existing WiFi
    access point). Total node cost: ~$80 per room.

Roles:
    - Multi-patient separation via Fast Independent Component Analysis
      (FastICA) across the 4 receiver streams.
    - Spatial zone assignment: each reflection is localized to a room
      zone (bed A / bed B / chair / bathroom doorway) via time-of-flight
      differences between the 4 receivers.
    - Redundancy: no single line-of-sight blockage defeats the system.

Synchronization:
    ESP32-S3 nodes are NOT natively phase-locked. Options under
    consideration:
      1. Software timestamping + re-interpolation onto a common grid
         (what we already do in esp32_csi_collector for a single node).
      2. GPIO-triggered capture from a common pulse-per-second source.
      3. Common WiFi beacon alignment (use the same AP's beacons as
         implicit time reference).

Status: NOT IMPLEMENTED. This module is an architectural placeholder;
the first real-hardware milestone is 2-node (TX + RX) and the 4-node
array is post-pilot.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NodeStream:
    node_id: int
    udp_port: int
    last_seen_s: float


@dataclass
class RoomZone:
    zone_id: str
    polygon_m: list[tuple[float, float]]  # floor-plan coords
    assigned_subject_id: str | None = None


class FourNodeArray:
    """Aggregates 4 ESP32 CSI streams into a single room-scale sensor."""

    def __init__(self, nodes: list[NodeStream], zones: list[RoomZone]):
        self.nodes = nodes
        self.zones = zones

    def unmix(self) -> dict[str, "SubjectSignal"]:  # noqa: F821
        raise NotImplementedError("FastICA unmixing across 4 nodes pending")

    def localize(self, subject_signal) -> RoomZone:
        raise NotImplementedError("time-of-flight zone assignment pending")
