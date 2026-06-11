"""Upload detection audio clips to S3 so the cloud dashboard can play them.

Clips are written to ``config.CLIPS_DIR`` on the recording machine; a host that
only runs the dashboard (Posit Connect Cloud) has no access to those files. So
the listener mirrors each clip to the same public S3 bucket the iPhone feed
uses, and stores the public URL on the detection row. The dashboard then plays
straight from the URL.

The whole feature is a no-op unless ``BW_FEED_S3_BUCKET`` is set and
``BW_PUBLISH_CLIPS`` is on. Clips containing a human voice are never uploaded —
the listener simply doesn't call ``upload()`` for them (see capture.listen).
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


def upload(filename, local_path):
    """Upload one clip and return its public URL, or None on failure/disabled.

    Best-effort: any error is swallowed (and logged) so an S3 hiccup never
    disturbs the capture loop — the detection is still recorded, just without a
    cloud-playable URL."""
    if not enabled():
        return None
    key = _key(filename)
    try:
        with open(local_path, "rb") as f:
            body = f.read()
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


def delete_many(filenames):
    """Best-effort delete of clips pruned from local disk, so the bucket tracks
    the same retention as CLIPS_DIR and the dashboard never shows a dead player.
    Swallows all errors."""
    if not enabled() or not filenames:
        return
    try:
        objects = [{"Key": _key(f)} for f in filenames]
        _client().delete_objects(
            Bucket=config.FEED_S3_BUCKET, Delete={"Objects": objects}
        )
    except Exception as e:
        print(f"   (clip S3 prune failed: {e})")
