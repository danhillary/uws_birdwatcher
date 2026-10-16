"""Upload detection audio clips to S3 so the cloud dashboard can play them.

A host that only runs the dashboard (Posit Connect Cloud) has no access to the
recording machine's disk. So the listener uploads each bird clip to the same
public S3 bucket the iPhone feed uses and stores the public URL on the detection
row; the dashboard plays straight from the URL. The listener saves each clip
locally first; its sync thread (uploader.py) uploads it with ``put`` and then
deletes the local copy, so bird clips end up in S3 only. They are never deleted
from the bucket.

The whole feature is a no-op unless ``BW_FEED_S3_BUCKET`` is set and
``BW_PUBLISH_CLIPS`` is on. Clips containing a human voice are never uploaded —
the listener keeps those on the recording machine instead (see capture.listen).
"""
import os

import config


def enabled():
    """True when clip uploads are configured (feed bucket set + not disabled)."""
    return bool(config.FEED_S3_BUCKET and config.PUBLISH_CLIPS)


def _key(filename):
    return f"{config.CLIPS_S3_PREFIX}/{os.path.basename(filename)}"


def _public_url(key):
    """Virtual-hosted–style public URL. us-east-1 omits the region segment."""
    bucket = config.FEED_S3_BUCKET
    region = config.FEED_S3_REGION
    if region and region != "us-east-1":
        return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def _client():
    import boto3  # imported lazily so the dashboard never needs the dependency
    from botocore.config import Config

    # boto3's defaults (60 s connect + 60 s read, several retries) can stall
    # the capture loop for minutes on a flaky network, long enough for the
    # watchdog to kill the listener as hung (Oct 14). Fail fast instead: a clip
    # whose upload fails falls back to local disk for the backfill tool.
    kwargs = {"config": Config(connect_timeout=5, read_timeout=15,
                               retries={"max_attempts": 1, "mode": "standard"})}
    if config.FEED_S3_REGION:
        kwargs["region_name"] = config.FEED_S3_REGION
    return boto3.client("s3", **kwargs)


def put(filename, body):
    """Put one clip body to S3 under its key and return the public URL.
    Raises on failure, so the sync thread can retry the clip later."""
    key = _key(filename)
    put_kwargs = {
        "Bucket": config.FEED_S3_BUCKET,
        "Key": key,
        "Body": body,
        "ContentType": "audio/wav",
        # Clips are immutable once written, so let the browser cache hard.
        "CacheControl": "max-age=31536000, immutable",
    }
    if config.FEED_S3_ACL:
        put_kwargs["ACL"] = config.FEED_S3_ACL
    _client().put_object(**put_kwargs)
    return _public_url(key)


def _put(filename, body):
    """Like put(), but best-effort: returns None when uploads are off or the
    upload fails (logged), for the backfill tool."""
    if not enabled():
        return None
    try:
        return put(filename, body)
    except Exception as e:
        print(f"   (clip upload failed: {e})")
        return None


def upload(filename, local_path):
    """Upload a clip from a local file and return its public URL, or None. Used
    by the backfill tool, which re-uploads clips already sitting on disk."""
    if not enabled():
        return None
    try:
        with open(local_path, "rb") as f:
            body = f.read()
    except OSError as e:
        print(f"   (clip read failed: {e})")
        return None
    return _put(filename, body)
