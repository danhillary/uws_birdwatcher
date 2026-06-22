"""Audio-clip encoding, local saving, and storage-cap pruning.

Bird clips live in S3 only — the listener encodes them in memory (``encode_clip``)
and uploads straight to the bucket, never writing local disk. ``save_clip`` is
the local-disk path, used only for the clips that *can't* go to the public
bucket: human-voice holds (privacy) and any clip whose S3 upload failed (kept on
disk so the backfill tool can retry it)."""
import io
import os
import re

import soundfile as sf

import config

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def ensure_dirs():
    os.makedirs(config.CLIPS_DIR, exist_ok=True)


def clip_filename(when, common_name):
    """The clip's filename (used as both the local name and the S3 key leaf).
    Deterministic, so encode_clip and save_clip agree on the same name."""
    safe_name = _SAFE.sub("_", common_name).strip("_") or "bird"
    return f"{when:%Y%m%dT%H%M%S}_{safe_name}.wav"


def _slice(audio, sample_rate, start_time, end_time):
    """The padded slice of `audio` around a detection."""
    pad = config.CLIP_PADDING_SECONDS
    start = max(0, int((start_time - pad) * sample_rate))
    end = min(len(audio), int((end_time + pad) * sample_rate))
    return audio[start:end] if end > start else audio


# 16-bit PCM (not the float32 default): half the size and playable in every
# browser, which matters now that clips are served to the web dashboard.
def encode_clip(audio, sample_rate, start_time, end_time):
    """Encode the detection's clip as in-memory WAV bytes, for direct upload to
    S3 with no local file written."""
    buf = io.BytesIO()
    sf.write(buf, _slice(audio, sample_rate, start_time, end_time), sample_rate,
             format="WAV", subtype="PCM_16")
    return buf.getvalue()


def save_clip(audio, sample_rate, when, common_name, start_time, end_time):
    """Write the detection's clip to a WAV in CLIPS_DIR. Returns the filename
    (not full path), suitable for the DB's clip_path."""
    filename = clip_filename(when, common_name)
    sf.write(os.path.join(config.CLIPS_DIR, filename),
             _slice(audio, sample_rate, start_time, end_time), sample_rate,
             subtype="PCM_16")
    return filename


def _clip_files():
    """(path, size, mtime) for every clip, newest last."""
    items = []
    for name in os.listdir(config.CLIPS_DIR):
        path = os.path.join(config.CLIPS_DIR, name)
        if os.path.isfile(path) and name.lower().endswith(".wav"):
            st = os.stat(path)
            items.append((name, path, st.st_size, st.st_mtime))
    items.sort(key=lambda t: t[3])  # oldest first
    return items


def prune_to_cap():
    """Delete oldest *local* clips until the directory is under MAX_CLIPS_MB.
    Returns the list of deleted filenames so the DB can be updated.

    Only human-voice holds and failed-upload fallbacks live on disk now (bird
    clips go straight to S3), so this just bounds that small local cache. The S3
    copies are permanent — they are never pruned."""
    if config.MAX_CLIPS_MB <= 0:
        return []
    cap_bytes = config.MAX_CLIPS_MB * 1024 * 1024
    files = _clip_files()
    total = sum(f[2] for f in files)
    removed = []
    i = 0
    while total > cap_bytes and i < len(files):
        name, path, size, _ = files[i]
        try:
            os.remove(path)
            total -= size
            removed.append(name)
        except OSError:
            pass
        i += 1
    return removed
