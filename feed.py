"""Publish a small public JSON feed for the iPhone widget.

Queries the shared database for the latest detection, today's totals, and the
listener's health, then writes a compact ``latest.json`` to S3 — a public-read
object a Scriptable widget on the phone can fetch directly. No database
credentials ever touch the phone; the widget only ever sees this JSON.

Run standalone to publish once and print the payload:

    python -m feed

Or let the listener call ``publish_safe()`` every loop (throttled to
``config.FEED_INTERVAL``). The whole feature is a no-op unless
``BW_FEED_S3_BUCKET`` is set.
"""
import datetime
import json
import time
import urllib.parse
import urllib.request

import config
import db

# Bird photos rarely change, so cache the Wikipedia lookup per process. This
# keeps the once-a-minute publish from re-fetching the same image.
_PHOTO_CACHE = {}
_last_publish = 0.0


def _photo(common_name, scientific_name):
    """A Wikipedia image URL for the species, or None. Tries the common name
    first, then the scientific name. Cached for the life of the process."""
    key = (common_name, scientific_name)
    if key in _PHOTO_CACHE:
        return _PHOTO_CACHE[key]
    found = None
    for title in (common_name, scientific_name):
        if not title:
            continue
        try:
            url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
                   + urllib.parse.quote(title.replace(" ", "_")))
            req = urllib.request.Request(
                url, headers={"User-Agent": "uws_birdwatcher/1.0 (hobby project)"}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.load(resp)
            # Use the canonical thumbnail URL as-is. Wikimedia only serves a
            # fixed set of cached thumbnail widths now; rewriting the width to
            # an arbitrary value (e.g. 640px) returns HTTP 400. The widget's
            # dark gradient keeps this ~330px image perfectly legible.
            thumb = (data.get("thumbnail") or {}).get("source")
            if thumb:
                found = thumb
                break
        except Exception:
            continue
    _PHOTO_CACHE[key] = found
    return found


def _humanize_age(seconds):
    """Compact 'time ago': 8s, 5m, 2h, 3d."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _as_date(v):
    return v.date() if isinstance(v, datetime.datetime) else v


def _health(status, now):
    """Mirror the dashboard's traffic-light logic for the widget's dot."""
    stale_after = max(60, config.SEGMENT_SECONDS * 4)
    if not status or not status.get("updated_at"):
        return {"state": "unknown", "label": "No heartbeat yet"}
    age = (now - status["updated_at"]).total_seconds()
    peak = status.get("last_peak") or 0.0
    if age > stale_after:
        return {"state": "red", "label": f"Offline {_humanize_age(age)}",
                "age_seconds": int(age)}
    if peak < 1e-4:
        return {"state": "yellow", "label": "Mic silent", "mic_level": peak}
    return {"state": "green", "label": "Listening",
            "age_seconds": int(age), "mic_level": round(peak, 3)}


def build_feed():
    """Assemble the widget payload from a single DB snapshot."""
    now = config.now_local()
    today = config.today_local()

    with db.get_conn() as conn:
        recent = db.recent_detections(conn, limit=1)
        counts = db.counts_for_day(conn, today.isoformat())
        overview = db.overview(conn)
        life = db.life_list(conn)
        status = db.listener_status(conn)

    feed = {
        "generated_at": now.isoformat(timespec="seconds"),
        "location": "Upper West Side, NYC",
        "listener": _health(status, now),
        "today": {
            "species": len(counts),
            "calls": sum(c["n"] for c in counts),
            "new_species": [r["common_name"] for r in life
                            if _as_date(r["first_seen"]) == today],
        },
        "totals": {
            "species": overview.get("species", 0),
            "detections": overview.get("total", 0),
        },
        "latest": None,
    }

    if recent:
        r = recent[0]
        at = r["detected_at"]
        feed["latest"] = {
            "name": r["common_name"],
            "scientific": r["scientific_name"],
            "confidence": round((r["confidence"] or 0) * 100),
            "at": at.isoformat(timespec="seconds")
                  if isinstance(at, datetime.datetime) else str(at),
            "ago": _humanize_age((now - at).total_seconds())
                   if isinstance(at, datetime.datetime) else None,
            "photo": _photo(r["common_name"], r["scientific_name"]),
        }
    return feed


def publish(feed=None):
    """Upload the feed JSON to S3. Raises if BW_FEED_S3_BUCKET isn't set or the
    upload fails — callers in the listener loop should use publish_safe()."""
    if not config.FEED_S3_BUCKET:
        raise RuntimeError("BW_FEED_S3_BUCKET is not set; feed is disabled.")
    import boto3  # imported lazily so the dashboard never needs the dependency

    feed = feed if feed is not None else build_feed()
    body = json.dumps(feed, default=str).encode("utf-8")

    client_kwargs = {}
    if config.FEED_S3_REGION:
        client_kwargs["region_name"] = config.FEED_S3_REGION
    s3 = boto3.client("s3", **client_kwargs)

    put_kwargs = {
        "Bucket": config.FEED_S3_BUCKET,
        "Key": config.FEED_S3_KEY,
        "Body": body,
        "ContentType": "application/json",
        "CacheControl": "max-age=30",
    }
    if config.FEED_S3_ACL:
        put_kwargs["ACL"] = config.FEED_S3_ACL
    s3.put_object(**put_kwargs)
    return feed


def publish_safe():
    """For the listener loop: publish at most once per FEED_INTERVAL seconds,
    swallowing every error so a feed problem never disturbs the capture loop."""
    global _last_publish
    if not config.FEED_S3_BUCKET:
        return  # feed disabled
    now = time.monotonic()
    if now - _last_publish < config.FEED_INTERVAL:
        return
    _last_publish = now  # set first, so a failure doesn't retry every loop
    try:
        publish()
    except Exception as e:
        print(f"   (feed publish failed: {e})")


if __name__ == "__main__":
    print(json.dumps(publish(), indent=2, default=str))
