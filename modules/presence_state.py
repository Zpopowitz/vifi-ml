"""Sensor-agnostic bed-exit / presence state machine.

Drives off a per-window boolean `present` signal and emits events on
state transitions. The presence detector is sensor-specific (the radar
worker uses chest-bin energy, the CSI worker uses amplitude variance),
but the state machine and event payloads are the same.

States and transitions:

    OUT  ----presence sustained for in_bed_stable_s---->  IN_BED
    IN_BED  ----absence sustained for bed_exit_after_s---->  BED_EXIT_ALERT
    BED_EXIT_ALERT  ----presence sustained for in_bed_stable_s---->  IN_BED

Brief blips in either direction (caregiver crossing the radar beam,
subject reaching for a glass of water) do not flip state -- only signal
flips sustained past the configured thresholds do. This is the
hospital-operations primitive that matters most for an early-warning
fall-prevention pilot.

Public API:
    PresenceStateMachine(in_bed_stable_s, bed_exit_after_s)
        .state                           -> str
        .step(ts_unix, present) -> PresenceEvent | None
    PresenceEvent                         dataclass
    OUT, IN_BED, BED_EXIT_ALERT           state constants
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

OUT = "out"
IN_BED = "in_bed"
BED_EXIT_ALERT = "bed_exit_alert"


@dataclass(frozen=True)
class PresenceEvent:
    """A state-machine transition. Emitted on every state change."""

    state: str
    """The state just entered."""
    prev_state: str
    """The state just left."""
    since_unix: float
    """When the new state was entered (Unix epoch seconds)."""
    ts_unix: float
    """When the event was emitted (Unix epoch seconds)."""


class PresenceStateMachine:
    """Sensor-agnostic presence + bed-exit state machine.

    Drives off `step(ts_unix, present)` calls -- one per inference window.
    `present` is a boolean derived per-sensor:
      - radar: chest-bin energy above a noise floor (in radar.pipeline)
      - csi:   amplitude variance above empty-room baseline (modules.presence)

    Configure:
      `in_bed_stable_s`  -- contiguous presence required to declare IN_BED
      `bed_exit_after_s` -- contiguous absence from IN_BED before BED_EXIT_ALERT
    """

    def __init__(
        self,
        in_bed_stable_s: float = 30.0,
        bed_exit_after_s: float = 60.0,
    ) -> None:
        if in_bed_stable_s < 0:
            raise ValueError("in_bed_stable_s must be >= 0")
        if bed_exit_after_s < 0:
            raise ValueError("bed_exit_after_s must be >= 0")
        self.in_bed_stable_s = float(in_bed_stable_s)
        self.bed_exit_after_s = float(bed_exit_after_s)

        self._state: str = OUT
        # Wall-clock when the current signal value started.
        # None until the first step() observation.
        self._signal_value: Optional[bool] = None
        self._signal_since: Optional[float] = None

    @property
    def state(self) -> str:
        return self._state

    def step(self, ts_unix: float, present: bool) -> Optional[PresenceEvent]:
        """Observe one (ts_unix, present) sample. Returns an event on a
        state transition, None otherwise.

        ts_unix must be monotonically non-decreasing across calls; the
        state machine does not reorder.
        """
        # Track when the current signal value started. A flip resets the
        # since-counter so transitions key off "this signal value has
        # been true for X seconds."
        if self._signal_value is None or self._signal_value != present:
            self._signal_value = present
            self._signal_since = ts_unix

        # signal_since is set by the block above whenever we observe a
        # value; guard for type checkers.
        assert self._signal_since is not None
        elapsed = ts_unix - self._signal_since

        prev_state = self._state
        new_state = prev_state

        if prev_state == OUT and present and elapsed >= self.in_bed_stable_s:
            new_state = IN_BED
        elif prev_state == IN_BED and not present and elapsed >= self.bed_exit_after_s:
            new_state = BED_EXIT_ALERT
        elif (
            prev_state == BED_EXIT_ALERT and present and elapsed >= self.in_bed_stable_s
        ):
            new_state = IN_BED

        if new_state == prev_state:
            return None

        self._state = new_state
        return PresenceEvent(
            state=new_state,
            prev_state=prev_state,
            since_unix=ts_unix,
            ts_unix=ts_unix,
        )
