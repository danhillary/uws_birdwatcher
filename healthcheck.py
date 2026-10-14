"""Dead man's switch: ping an external monitor while the listener is healthy.

Set BW_HEALTHCHECK_URL to a healthchecks.io check (or any service with the same
URL scheme). The listener pings it every BW_HEALTHCHECK_INTERVAL seconds, but
only while the mic is hearing sound, so the monitor alerts you when the pings
stop: the machine is off, asleep or waiting at the sign-in screen, the network
is down, the listener is dead, or the mic stays silent. The watchdog also sends
an explicit failure when its own recovery gives up, so that alert comes at once
instead of after the grace period. Outages that recover within the grace
period never alert. The monitor sends a recovery notice when pings resume.

Run ``python -m healthcheck`` to send a test ping.
"""
import threading
import time
import urllib.request

import config

_last_ok = float("-inf")
_confirmed = False


def _send(url, body=None):
    data = body.encode("utf-8")[:10_000] if body else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"User-Agent": "uws-birdwatcher"},
    )
    urllib.request.urlopen(req, timeout=10).close()


def ok_safe():
    """Throttled 'alive' ping for the capture loop. Sent on a daemon thread, so
    a slow or dead network never holds up recording, and never raises."""
    global _last_ok
    if not config.HEALTHCHECK_URL:
        return
    now = time.monotonic()
    if now - _last_ok < config.HEALTHCHECK_INTERVAL:
        return
    _last_ok = now

    def run():
        global _confirmed
        try:
            _send(config.HEALTHCHECK_URL)
        except Exception as e:
            print(f"   (health ping failed: {e})")
            return
        if not _confirmed:  # say so once per run, so the log shows alerts are armed
            _confirmed = True
            print("   (health ping delivered; outage alerts are armed)")

    threading.Thread(target=run, name="health-ping", daemon=True).start()


def fail(message):
    """Report a failure right away, with ``message`` as the alert's details.
    Returns True if it was delivered."""
    if not config.HEALTHCHECK_URL:
        return False
    try:
        _send(config.HEALTHCHECK_URL.rstrip("/") + "/fail", message)
        return True
    except Exception as e:
        print(f"   (failure alert not delivered: {e})")
        return False


if __name__ == "__main__":
    if not config.HEALTHCHECK_URL:
        raise SystemExit("BW_HEALTHCHECK_URL is not set.")
    _send(config.HEALTHCHECK_URL, "test ping from python -m healthcheck")
    print("Ping sent; the check should now show as up.")
