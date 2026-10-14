"""Keep the listener running: restart it when it exits, kill it when it hangs,
and send an alert only when restarting isn't fixing things.

run_listener.vbs starts this rather than the listener itself. The old setup
relied on Task Scheduler's "restart if the task fails", which never fired: the
.vbs returns at once, so the task always looked successful. Here every listener
exit (crash, mic give-up) is followed by a restart, with backoff from 5 s to
5 minutes, and a listener whose heartbeat file goes stale for BW_HANG_SECONDS
is killed and restarted. Once BW_ALERT_AFTER_RESTARTS restarts pile up within
BW_ALERT_WINDOW seconds, a failure goes to BW_HEALTHCHECK_URL with the reason
and the end of the log.

The listener's output goes to listener.log (rolled over to listener.log.1 at
50 MB); this watchdog's own events (starts, exits, kills, alerts) are printed,
and run_listener.vbs appends them to watchdog.log, a short outage history.

Run with:   python -m capture.watchdog
Stop with:  python -m capture.watchdog --stop   (stops the listener too)
Only one watchdog runs at a time; extra starts exit at once, so launching it
repeatedly (e.g. from a repeating scheduled task) is safe.
"""
import datetime
import os
import subprocess
import sys
import time

import config
import healthcheck
from capture.recovery import EXIT_MIC, RestartPolicy

LOG_PATH = os.path.join(config.BASE_DIR, "listener.log")
LOG_MAX_BYTES = 50 * 1024 * 1024
STOP_PATH = os.path.join(config.BASE_DIR, "watchdog.stop")
LOCK_PATH = os.path.join(config.BASE_DIR, "watchdog.lock")
# Model loading and a slow database at startup can take a while before the
# first heartbeat.
STARTUP_GRACE = 120

_lock = None


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def take_lock():
    """One watchdog per machine. Returns False if another one holds the lock."""
    global _lock
    f = open(LOCK_PATH, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _lock = f
    return True


def rotate_log():
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass  # missing, or still held open by a listener from the old launcher


def kill_tree(proc):
    """Kill the listener and its children. On Windows a venv's python.exe is a
    launcher that runs the real interpreter as a child, so kill the whole tree."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def heartbeat_age(started):
    """Seconds since the listener last showed progress. Before its first
    heartbeat, the clock starts STARTUP_GRACE seconds after launch."""
    try:
        beat = os.path.getmtime(config.HEARTBEAT_PATH)
    except OSError:
        beat = 0
    return time.time() - max(beat, started + STARTUP_GRACE)


def log_tail(lines=25):
    """The end of listener.log, minus BirdNET's per-segment chatter."""
    noise = ("read_audio_data", "analyze_recording", "recording has lon/lat",
             "set_predicted_species_list", "return_predicted_species_list")
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(max(0, os.path.getsize(LOG_PATH) - 64 * 1024))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return "(listener.log unreadable)"
    keep = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith(noise)]
    return "\n".join(keep[-lines:])


def run_listener():
    """Run the listener once. Returns (exit_code, seconds_run, why)."""
    rotate_log()
    try:
        os.remove(config.HEARTBEAT_PATH)
    except OSError:
        pass
    started = time.time()
    with open(LOG_PATH, "ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "capture.listen"],
            cwd=config.BASE_DIR, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log(f"listener started (pid {proc.pid})")
        try:
            while proc.poll() is None:
                if os.path.exists(STOP_PATH):
                    kill_tree(proc)
                    return None, time.time() - started, "stop requested"
                if heartbeat_age(started) > config.HANG_SECONDS:
                    kill_tree(proc)
                    return (proc.returncode, time.time() - started,
                            f"no progress for {config.HANG_SECONDS}s (hung); killed")
                time.sleep(5)
        except KeyboardInterrupt:
            kill_tree(proc)
            raise
    code = proc.returncode
    why = ("mic stayed silent or broken despite reopening" if code == EXIT_MIC
           else f"exited with code {code}")
    return code, time.time() - started, why


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--stop" in argv:
        open(STOP_PATH, "w").close()
        print("Stop requested; the watchdog stops the listener within ~5 s.")
        return 0
    if not take_lock():
        return 0  # already supervised; quietly do nothing
    try:
        os.remove(STOP_PATH)
    except OSError:
        pass

    policy = RestartPolicy(config.ALERT_AFTER_RESTARTS, config.ALERT_WINDOW_SECONDS)
    log(f"watchdog up (pid {os.getpid()}); alerts "
        f"{'on' if config.HEALTHCHECK_URL else 'off (BW_HEALTHCHECK_URL not set)'}")
    try:
        while True:
            if os.path.exists(STOP_PATH):
                os.remove(STOP_PATH)
                log("stop requested; listener stopped, watchdog exiting")
                return 0
            code, ran, why = run_listener()
            if why == "stop requested":
                continue
            delay, alert = policy.on_exit(ran)
            log(f"listener {why} after {ran / 60:.1f} min; restarting in {delay}s")
            if alert:
                n = len(policy.failures)
                msg = (f"Birdwatcher listener on {os.environ.get('COMPUTERNAME', 'host')} "
                       f"is not recovering: {n} restarts in the last "
                       f"{config.ALERT_WINDOW_SECONDS // 60} min. Latest: {why}.\n\n"
                       f"End of listener.log:\n{log_tail()}")
                sent = healthcheck.fail(msg)
                log(f"ALERT {'sent' if sent else 'NOT sent'}: {n} restarts, latest: {why}")
            # Sleep in short steps so --stop still works during a long backoff.
            for _ in range(delay):
                if os.path.exists(STOP_PATH):
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        log("interrupted; watchdog exiting")
        return 0
    except Exception as e:  # never die silently: this process has no console
        log(f"watchdog crashed: {e!r}")
        raise


if __name__ == "__main__":
    sys.exit(main())
