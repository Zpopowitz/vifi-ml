"""Tests for modules.presence_state -- sensor-agnostic bed-exit /
presence state machine.

The machine drives off a per-window boolean `present` signal and emits
events on state transitions. Sensor-agnostic: the same code handles
radar-derived presence (chest-bin energy) and CSI-derived presence
(amplitude variance) without modification.
"""

from __future__ import annotations

import pytest

from modules.presence_state import (
    BED_EXIT_ALERT,
    IN_BED,
    OUT,
    PresenceEvent,
    PresenceStateMachine,
)


def test_initial_state_is_out():
    sm = PresenceStateMachine()
    assert sm.state == OUT


def test_step_returns_none_when_no_transition():
    """A single step that doesn't trigger a transition returns None."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0, bed_exit_after_s=60.0)
    ev = sm.step(ts_unix=100.0, present=True)
    # 0 s of presence at t=100 isn't enough for IN_BED yet.
    assert ev is None
    assert sm.state == OUT


def test_sustained_presence_transitions_to_in_bed():
    """30 s of continuous presence triggers OUT -> IN_BED."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0)
    sm.step(ts_unix=100.0, present=True)  # presence starts
    sm.step(ts_unix=110.0, present=True)  # 10 s elapsed, still OUT
    sm.step(ts_unix=125.0, present=True)  # 25 s elapsed, still OUT
    ev = sm.step(ts_unix=131.0, present=True)  # 31 s elapsed -> IN_BED
    assert ev is not None
    assert isinstance(ev, PresenceEvent)
    assert ev.state == IN_BED
    assert ev.prev_state == OUT
    assert sm.state == IN_BED


def test_brief_absence_does_not_leave_in_bed():
    """A short blip of absence while IN_BED stays IN_BED (caregiver
    briefly stepping in front of the radar, etc.)."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0, bed_exit_after_s=60.0)
    # Get to IN_BED.
    sm.step(100.0, True)
    ev_in_bed = sm.step(140.0, True)
    assert ev_in_bed is not None and ev_in_bed.state == IN_BED

    # 10 s absence -- not enough for bed exit (threshold 60 s).
    ev = sm.step(150.0, False)
    assert ev is None
    assert sm.state == IN_BED

    # Presence returns.
    ev = sm.step(155.0, True)
    assert ev is None
    assert sm.state == IN_BED


def test_sustained_absence_triggers_bed_exit_alert():
    """60 s of no presence while IN_BED triggers IN_BED -> BED_EXIT_ALERT."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0, bed_exit_after_s=60.0)
    # Get to IN_BED.
    sm.step(100.0, True)
    sm.step(140.0, True)
    assert sm.state == IN_BED

    # Absence starts at t=200.
    sm.step(200.0, False)
    sm.step(230.0, False)  # 30 s absent, no alert yet
    ev = sm.step(265.0, False)  # 65 s absent -> alert
    assert ev is not None
    assert ev.state == BED_EXIT_ALERT
    assert ev.prev_state == IN_BED
    assert sm.state == BED_EXIT_ALERT


def test_return_to_in_bed_from_alert():
    """After a bed-exit alert, sustained presence returns to IN_BED."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0, bed_exit_after_s=60.0)
    # IN_BED.
    sm.step(100.0, True)
    sm.step(140.0, True)
    # BED_EXIT_ALERT.
    sm.step(200.0, False)
    sm.step(265.0, False)
    assert sm.state == BED_EXIT_ALERT

    # Subject returns at t=400; 30 s later should re-enter IN_BED.
    sm.step(400.0, True)
    ev = sm.step(431.0, True)
    assert ev is not None
    assert ev.state == IN_BED
    assert ev.prev_state == BED_EXIT_ALERT


def test_short_blip_of_presence_in_out_does_not_promote():
    """OUT + brief presence (e.g., caregiver walking through) stays OUT."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0)
    sm.step(100.0, True)
    sm.step(105.0, False)  # presence gone after 5 s, well under the 30 s threshold
    sm.step(120.0, True)
    sm.step(122.0, False)
    assert sm.state == OUT


def test_event_carries_state_since_when_state_was_entered():
    """The event records when the NEW state was entered, not when it
    was emitted (those differ by a step-call granularity)."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0)
    sm.step(100.0, True)
    ev = sm.step(131.0, True)
    assert ev is not None
    # The "new state was entered" timestamp is the emission ts (the
    # first observation that met the threshold).
    assert ev.since_unix == pytest.approx(131.0)
    assert ev.ts_unix == pytest.approx(131.0)


def test_configurable_thresholds():
    """Custom in_bed_stable_s and bed_exit_after_s are honored."""
    sm = PresenceStateMachine(in_bed_stable_s=5.0, bed_exit_after_s=10.0)
    sm.step(100.0, True)
    ev = sm.step(106.0, True)  # 6 s presence with threshold 5 s
    assert ev is not None and ev.state == IN_BED

    sm.step(200.0, False)
    ev = sm.step(211.0, False)  # 11 s absence with threshold 10 s
    assert ev is not None and ev.state == BED_EXIT_ALERT


def test_no_event_on_repeated_same_state():
    """Once IN_BED is reached, repeated present=True calls don't re-fire."""
    sm = PresenceStateMachine(in_bed_stable_s=30.0)
    sm.step(100.0, True)
    ev1 = sm.step(131.0, True)
    assert ev1 is not None and ev1.state == IN_BED
    ev2 = sm.step(140.0, True)
    ev3 = sm.step(200.0, True)
    assert ev2 is None
    assert ev3 is None
