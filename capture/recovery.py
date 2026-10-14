"""Recovery decisions for the listener and its watchdog, kept free of audio and
database imports so they can be unit-tested with a fake clock."""
import time

# Exit code the listener uses when the mic stayed silent or broken through
# every reopen, so the watchdog can say so in its alert.
EXIT_MIC = 3


class MicGuard:
    """Tracks consecutive bad segments (silent, or recording failed).

    ``check(ok)`` returns what the capture loop should do next:
    "ok" (healthy), "wait" (bad, keep trying), "reopen" (restart PortAudio and
    find the mic again; every ``reopen_after`` bad segments), or "give_up"
    (bad for ``give_up_after`` seconds despite reopening; exit and let the
    watchdog start a fresh process).
    """

    def __init__(self, reopen_after, give_up_after, clock=time.monotonic):
        self.reopen_after = max(1, reopen_after)
        self.give_up_after = give_up_after
        self.clock = clock
        self.bad = 0
        self.since = None

    def check(self, ok):
        if ok:
            self.bad, self.since = 0, None
            return "ok"
        now = self.clock()
        if self.since is None:
            self.since = now
        self.bad += 1
        if now - self.since >= self.give_up_after:
            return "give_up"
        if self.bad % self.reopen_after == 0:
            return "reopen"
        return "wait"


class RestartPolicy:
    """The watchdog's backoff between restarts, and when to raise the alarm.

    Every listener exit (crash, give-up or forced kill) counts as a failure. A
    run that lasted ``healthy_run`` seconds clears the history first, so an
    exit after days of good running restarts quickly and quietly. Delays double
    from 5 s up to 5 minutes. ``on_exit`` returns (delay_seconds, alert): alert
    is True once, when failures within ``window`` seconds reach
    ``alert_after``; it re-arms after a healthy run.
    """

    def __init__(self, alert_after, window, healthy_run=1800,
                 clock=time.monotonic):
        self.alert_after = alert_after
        self.window = window
        self.healthy_run = healthy_run
        self.clock = clock
        self.failures = []
        self.alerted = False

    def on_exit(self, ran_for):
        now = self.clock()
        if ran_for >= self.healthy_run:
            self.failures, self.alerted = [], False
        self.failures = [t for t in self.failures if now - t < self.window]
        self.failures.append(now)
        delay = min(300, 5 * 2 ** (len(self.failures) - 1))
        alert = len(self.failures) >= self.alert_after and not self.alerted
        if alert:
            self.alerted = True
        return delay, alert
