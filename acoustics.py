"""Spectrograms and pitch signatures from detection clips.

The listener stores a 5-second, 48 kHz mono 16-bit WAV for almost every
detection on S3. This module turns one into:

* a small **spectrogram** (frequency x time, in dB relative to the clip's
  loudest point) for drawing, and
* a **pitch signature**: how much sound in each frequency band rises well
  above that band's usual level, normalised to sum to 1, so clips and
  species can be compared. Calls are short bursts while traffic and building
  hum are steady, so counting only the bursts keeps the background out.

Only numpy and the standard library, so the hosted dashboard needs no new
packages. Everything is kept small: 48 frequency bands of 250 Hz up to 12 kHz
(where nearly all birdsong sits) and 100 time steps per clip.
"""
import io
import urllib.parse
import urllib.request
import wave

import numpy as np

FREQ_MAX_HZ = 12000
BAND_HZ = 250
N_BANDS = FREQ_MAX_HZ // BAND_HZ          # 48
TIME_STEPS = 100
FLOOR_DB = -60.0
# Below this, clips are mostly traffic and building hum, not birds; the pitch
# signature ignores it (the spectrogram still shows it).
SIGNATURE_MIN_HZ = 500
# A moment counts toward the pitch signature when its power in a band is at
# least this many times the band's median over the clip (about 7 dB).
BURST_FACTOR = 5.0
MAX_CLIP_BYTES = 5 * 1024 * 1024


def band_centres_hz():
    return (np.arange(N_BANDS) + 0.5) * BAND_HZ


def fetch(url, timeout=10):
    """Download a clip (http/https only, at most MAX_CLIP_BYTES)."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"Unsupported clip URL: {url!r}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "uws_birdwatcher/1.0 (hobby project)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_CLIP_BYTES + 1)
    if len(data) > MAX_CLIP_BYTES:
        raise ValueError("Clip is larger than expected")
    return data


def read_wav(data):
    """WAV bytes -> (mono float samples in [-1, 1], sample rate)."""
    with wave.open(io.BytesIO(data)) as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"Unsupported sample width: {width} bytes")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def _band_power(x, sr, n_fft=1024, hop=512):
    """Power per 250 Hz band (rows) per STFT frame (columns)."""
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    n_frames = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(n_fft)[None, :]
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2          # frames x freqs
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    band = np.floor(freqs / BAND_HZ).astype(int)
    keep = band < N_BANDS
    out = np.zeros((N_BANDS, n_frames))
    np.add.at(out, band[keep], power[:, keep].T)
    return out


def spectrogram(x, sr):
    """(N_BANDS x TIME_STEPS array of dB relative to the loudest cell, clipped
    at FLOOR_DB; seconds per time step)."""
    bp = _band_power(x, sr)
    n_frames = bp.shape[1]
    steps = min(TIME_STEPS, n_frames)
    groups = np.array_split(np.arange(n_frames), steps)
    pooled = np.stack([bp[:, g].mean(axis=1) for g in groups], axis=1)
    db = 10 * np.log10(pooled + 1e-12)
    db = np.clip(db - db.max(), FLOOR_DB, 0)
    return db, (len(x) / sr) / steps


def pitch_signature(x, sr):
    """Share of burst energy per 250 Hz band (sums to 1; all zeros if the
    clip has no bursts). Burst energy is power above BURST_FACTOR times the
    band's median, summed over the clip, so steady background drops out.
    Bands below SIGNATURE_MIN_HZ are ignored."""
    bp = _band_power(x, sr)
    median = np.median(bp, axis=1, keepdims=True)
    total = np.clip(bp - BURST_FACTOR * median, 0, None).sum(axis=1)
    total[band_centres_hz() < SIGNATURE_MIN_HZ] = 0
    s = total.sum()
    return total / s if s > 0 else total


def signature_stats(sig):
    """(peak frequency, lower quartile, upper quartile) in Hz for a pitch
    signature: where it is loudest and the band holding its middle half of
    energy."""
    centres = band_centres_hz()
    if not np.any(sig > 0):
        return None, None, None
    cum = np.cumsum(sig) / sig.sum()
    lo = centres[np.searchsorted(cum, 0.25)]
    hi = centres[np.searchsorted(cum, 0.75)]
    return float(centres[int(np.argmax(sig))]), float(lo), float(hi)


def analyze(data):
    """Everything the dashboard needs from one clip, small enough to cache:
    spectrogram (dB array), seconds per step, pitch signature and
    duration."""
    x, sr = read_wav(data)
    db, step_s = spectrogram(x, sr)
    return {"spectrogram": db.astype(np.float32),
            "step_s": float(step_s),
            "signature": pitch_signature(x, sr).astype(np.float32),
            "duration_s": len(x) / sr}


def mean_signature(signatures):
    """Average of several pitch signatures (each weighted equally)."""
    sigs = [np.asarray(s, dtype=float) for s in signatures if s is not None]
    if not sigs:
        return None
    m = np.mean(sigs, axis=0)
    return m / m.sum() if m.sum() > 0 else m
