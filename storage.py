"""Audio-clip saving and storage-cap pruning.

Recording is local-first: the listener saves every clip to CLIPS_DIR with
``save_clip``. Bird clips wait there only until the sync thread (uploader.py)
has uploaded them to S3, then are deleted; human-voice holds (privacy) stay,
as do clips whose upload kept failing (for the backfill tool). ``prune_to_cap``
keeps the folder under BW_MAX_CLIPS_MB whatever happens."""
import os
import re

import soundfile as sf

import config

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def ensure_dirs():
    os.makedirs(config.CLIPS_DIR, exist_ok=True)


def clip_filename(when, common_name):
    """The clip's filename (used as both the local name and the S3 key leaf)."""
    safe_name = _SAFE.sub("_", common_name).strip("_") or "bird"
    return f"{when:%Y%m%dT%H%M%S}_{safe_name}.wav"


def _slice(audio, sample_rate, start_time, end_time):
    """The padded slice of `audio` around a detection."""
    pad = config.CLIP_PADDING_SECONDS
    start = max(0, int((start_time - pad) * sample_rate))
    end = min(len(audio), int((end_time + pad) * sample_rate))
    return audio[start:end] if end > start else audio


def save_clip(audio, sample_rate, when, common_name, start_time, end_time):
    """Write the detection's clip to a WAV in CLIPS_DIR. Returns the filename
    (not full path), suitable for the DB's clip_path.

    16-bit PCM (not the float32 default): half the size and playable in every
    browser, which matters because clips are served to the web dashboard."""
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

    Normally the folder holds only human-voice holds, failed-upload fallbacks
    and a few clips waiting for upload. During a long outage the upload backlog
    grows too; past the cap its oldest clips go (their detections are still
    written, without audio). The S3 copies are permanent and never pruned."""
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
