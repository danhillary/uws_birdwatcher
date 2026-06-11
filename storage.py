"""Audio-clip saving and storage-cap pruning."""
import os
import re

import soundfile as sf

import config

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def ensure_dirs():
    os.makedirs(config.CLIPS_DIR, exist_ok=True)


def save_clip(audio, sample_rate, when, common_name, start_time, end_time):
    """Write the slice of `audio` around a detection to a WAV in CLIPS_DIR.
    Returns the filename (not full path), suitable for the DB's clip_path."""
    pad = config.CLIP_PADDING_SECONDS
    start = max(0, int((start_time - pad) * sample_rate))
    end = min(len(audio), int((end_time + pad) * sample_rate))
    slice_ = audio[start:end] if end > start else audio

    safe_name = _SAFE.sub("_", common_name).strip("_") or "bird"
    filename = f"{when:%Y%m%dT%H%M%S}_{safe_name}.wav"
    # 16-bit PCM (not the float32 default): half the size and playable in every
    # browser, which matters now that clips are served to the web dashboard.
    sf.write(os.path.join(config.CLIPS_DIR, filename), slice_, sample_rate,
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
    """Delete oldest clips until the directory is under MAX_CLIPS_MB.
    Returns the list of deleted filenames so the DB can be updated."""
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
