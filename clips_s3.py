"""Upload detection audio clips to S3 so the cloud dashboard can play them.

A host that only runs the dashboard (Posit Connect Cloud) has no access to the
recording machine's disk. So the listener uploads each bird clip to the same
public S3 bucket the iPhone feed uses and stores the public URL on the detection
row; the dashboard plays straight from the URL. Bird clips live in S3 *only* —
the listener uploads them from memory (``upload_bytes``) without ever writing
local disk — and they are never deleted from the bucket.

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

    kwargs = {}
    if config.FEED_S3_REGION:
        kwargs["region_name"] = config.FEED_S3_REGION
    return boto3.client("s3", **kwargs)


def _put(filename, body):
    """Put one clip body to S3 under its key and return the public URL, or None.

    Best-effort: any error is swallowed (and logged) so an S3 hiccup never
    disturbs the capture loop — the detection is still recorded, just without a
    cloud-playable URL."""
    if not enabled():
        return None
    key = _key(filename)
    try:
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
    except Exception as e:
        print(f"   (clip upload failed: {e})")
        return None
    return _public_url(key)


def upload_bytes(filename, data):
    """Upload an in-memory clip and return its public URL, or None. Lets the
    listener store bird clips in S3 only, never touching local disk."""
    return _put(filename, data)


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
