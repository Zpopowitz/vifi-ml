"""ViFi message bus -- package facade.

The bus has four pieces, split into sibling modules for blast-radius
isolation per the 2026-05-23 codebase audit. The package surface
preserves the pre-split `from modules.bus import X` import paths so no
caller needs to change.

  contract       topic helpers, Message + IDs, MessageBus Protocol,
                 route_to_dlq + subscribe helpers, DEFAULT_BUS_MAXLEN
  memory         InMemoryBus -- single-process dev + tests
  redis_driver   RedisStreamBus -- production
  factory        bus_from_env -- env-driven backend selection
"""

from modules.bus.contract import (
    DEFAULT_BUS_MAXLEN,
    EARLIEST,
    LATEST,
    Message,
    MessageBus,
    _id_gt,
    _parse_id,
    all_topics,
    apnea_events,
    audit_topics,
    csi_raw,
    dlq,
    hr_predicted,
    hr_reference,
    presence_events,
    radar_raw,
    route_to_dlq,
    rr_predicted,
    rr_reference,
    subscribe,
)
from modules.bus.factory import bus_from_env
from modules.bus.memory import InMemoryBus
from modules.bus.redis_driver import RedisStreamBus

__all__ = [
    "DEFAULT_BUS_MAXLEN",
    "EARLIEST",
    "InMemoryBus",
    "LATEST",
    "Message",
    "MessageBus",
    "RedisStreamBus",
    "_id_gt",
    "_parse_id",
    "all_topics",
    "apnea_events",
    "audit_topics",
    "bus_from_env",
    "csi_raw",
    "dlq",
    "hr_predicted",
    "hr_reference",
    "presence_events",
    "radar_raw",
    "route_to_dlq",
    "rr_predicted",
    "rr_reference",
    "subscribe",
]
