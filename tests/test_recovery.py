"""Tests for the listener's self-recovery: the outbox, the mic guard and the
watchdog's restart policy (no audio, database or network needed)."""
import datetime

import pytest

import outbox
from capture.recovery import MicGuard, RestartPolicy


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _row(i):
    return dict(detected_at=datetime.datetime(2026, 10, 13, 23, 53, i),
                common_name=f"Bird {i}", scientific_name="Avis sp.",
                confidence=0.8, lat=40.776, lon=-73.977,
                clip_path=None, clip_url=None)


# --- outbox -----------------------------------------------------------------

def test_outbox_round_trip(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    assert not outbox.pending(path)
    outbox.add(_row(1), path)
    outbox.add(_row(2), path)
    assert outbox.pending(path)

    got = []
    assert outbox.flush(lambda **r: got.append(r), path) == 2
    assert got == [_row(1), _row(2)]  # datetimes restored, order kept
    assert not outbox.pending(path)


def test_outbox_keeps_rows_after_a_failure(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    for i in range(3):
        outbox.add(_row(i), path)

    calls = []

    def insert(**r):
        if len(calls) == 1:
            raise ConnectionError("db down again")
        calls.append(r)

    with pytest.raises(ConnectionError):
        outbox.flush(insert, path)
    # Row 0 was written; rows 1 and 2 wait for the next flush.
    got = []
    assert outbox.flush(lambda **r: got.append(r), path) == 2
    assert [r["common_name"] for r in got] == ["Bird 1", "Bird 2"]


def test_outbox_skips_corrupt_lines(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    outbox.add(_row(1), path)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"truncated": \n')
    outbox.add(_row(2), path)
    got = []
    assert outbox.flush(lambda **r: got.append(r), path) == 3
    assert len(got) == 2
    assert not outbox.pending(path)


def test_outbox_flush_with_nothing_queued(tmp_path):
    assert outbox.flush(lambda **r: None, str(tmp_path / "none.jsonl")) == 0


def test_outbox_keeps_rows_added_during_a_flush(tmp_path):
    # The capture thread appends while the sync thread is mid-flush.
    path = str(tmp_path / "outbox.jsonl")
    outbox.add(_row(1), path)
    outbox.add(_row(2), path)

    got = []

    def insert(**r):
        got.append(r["common_name"])
        if len(got) == 1:
            outbox.add(_row(3), path)

    assert outbox.flush(insert, path) == 2
    assert got == ["Bird 1", "Bird 2"]
    left = []
    outbox.flush(lambda **r: left.append(r["common_name"]), path)
    assert left == ["Bird 3"]


def test_outbox_cap_drops_oldest_rows(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    for i in range(40):
        outbox.add(_row(i % 60), path, max_bytes=4000)
    import os
    assert os.path.getsize(path) <= 4000
    got = []
    outbox.flush(lambda **r: got.append(r["common_name"]), path)
    assert 0 < len(got) < 40
    assert got[-1] == "Bird 39"  # newest kept, oldest dropped


# --- mic guard ----------------------------------------------------------------

def test_mic_guard_reopens_every_n_bad_segments_then_gives_up():
    clock = Clock()
    g = MicGuard(reopen_after=4, give_up_after=600, clock=clock)
    actions = []
    for _ in range(8):
        actions.append(g.check(False))
        clock.t += 15
    assert actions == ["wait"] * 3 + ["reopen"] + ["wait"] * 3 + ["reopen"]
    clock.t = 600
    assert g.check(False) == "give_up"


def test_mic_guard_resets_on_sound():
    clock = Clock()
    g = MicGuard(reopen_after=2, give_up_after=60, clock=clock)
    g.check(False)
    clock.t = 59
    assert g.check(True) == "ok"
    clock.t = 120
    # A fresh fault starts its own clock: no give-up from the old one.
    assert g.check(False) == "wait"
    assert g.check(False) == "reopen"


def test_mic_guard_single_silent_blips_never_reopen():
    g = MicGuard(reopen_after=4, give_up_after=600, clock=Clock())
    for _ in range(20):
        assert g.check(False) == "wait"
        assert g.check(True) == "ok"


# --- restart policy -----------------------------------------------------------

def test_restart_policy_backs_off_and_alerts_once():
    clock = Clock()
    p = RestartPolicy(alert_after=3, window=3600, clock=clock)
    results = []
    for _ in range(5):
        results.append(p.on_exit(ran_for=10))
        clock.t += 60
    assert [d for d, _ in results] == [5, 10, 20, 40, 80]
    assert [a for _, a in results] == [False, False, True, False, False]


def test_restart_policy_delay_caps_at_five_minutes():
    p = RestartPolicy(alert_after=99, window=10**9, clock=Clock())
    delays = [p.on_exit(ran_for=1)[0] for _ in range(12)]
    assert max(delays) == 300


def test_restart_policy_healthy_run_clears_history_and_rearms():
    clock = Clock()
    p = RestartPolicy(alert_after=2, window=3600, healthy_run=1800, clock=clock)
    p.on_exit(10)
    clock.t += 30
    assert p.on_exit(10) == (10, True)
    clock.t += 7200
    # After two hours of good running, an exit restarts fast and stays quiet...
    assert p.on_exit(7200) == (5, False)
    clock.t += 30
    # ...and a new run of failures can alert again.
    assert p.on_exit(10) == (10, True)


def test_restart_policy_old_failures_age_out():
    clock = Clock()
    p = RestartPolicy(alert_after=3, window=3600, clock=clock)
    p.on_exit(10)
    p.on_exit(10)
    clock.t += 4000  # both outside the window now
    assert p.on_exit(10) == (5, False)
