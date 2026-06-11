"""Backfill S3 URLs for detection clips recorded before cloud uploads existed.

Early detections saved a local WAV (``clip_path``) but never uploaded it, so the
cloud dashboard has nothing to play (``clip_url`` is NULL). This one-off tool
finds those rows, uploads each clip to the same public S3 bucket the live
listener uses, and records the URL — making old clips playable in the dashboard.

Run it ON THE RECORDING MACHINE, where the clip files, birdnetlib, and boto3 all
live (the cloud dashboard host has none of them):

    python -m backfill_clips                 # voice-check + upload everything
    python -m backfill_clips --dry-run       # list what would happen, touch nothing
    python -m backfill_clips --limit 20      # only the oldest 20 candidates
    python -m backfill_clips --no-voice-check # skip the privacy re-analysis (faster)

Privacy: these clips predate the human-voice filter, so by default every clip is
re-analysed with BirdNET before upload and any clip with an audible human voice
is kept local only (never uploaded), matching how the live listener now behaves.
Pass --no-voice-check to skip that and upload everything (use only if you're sure
none contain private audio).

Safe to re-run: it only touches rows that still have no clip_url.
"""
import argparse
import os
import sys
import tempfile

import numpy as np
import soundfile as sf

import clips_s3
import config
import db


def _load_mono(path):
    """Read a clip as a mono float32 array + sample rate, for re-analysis."""
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1 and audio.shape[1] > 1:
        audio = audio.mean(axis=1)
    return np.asarray(audio).reshape(-1), sr


def _reencode_pcm16(path):
    """Rewrite a clip as 16-bit PCM to a temp file and return its path.

    Old clips may be float32 (the pre-PCM_16 default), which some browsers won't
    play. Re-encoding on upload makes backfilled clips match new ones."""
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    tmp = os.path.join(tempfile.gettempdir(),
                       f"bw_backfill_{os.path.basename(path)}")
    sf.write(tmp, audio, sr, subtype="PCM_16")
    return tmp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-voice-check", action="store_true",
                        help="skip the per-clip human-voice re-analysis")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N (oldest-first) clips")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without uploading or writing")
    args = parser.parse_args(argv)

    if not clips_s3.enabled():
        print("Clip uploads are not configured (set BW_FEED_S3_BUCKET and leave "
              "BW_PUBLISH_CLIPS on). Nothing to do.")
        return 1

    voice_check = not args.no_voice_check

    with db.get_conn() as conn:
        rows = db.detections_needing_clip_url(conn)
    if args.limit is not None:
        rows = rows[:args.limit]

    if not rows:
        print("No clips need backfilling — every clip with a local file already "
              "has a cloud URL.")
        return 0

    print(f"Found {len(rows)} clip(s) needing a cloud URL. "
          f"Voice-check: {'on' if voice_check else 'off'} | "
          f"{'DRY RUN — no changes' if args.dry_run else 'uploading'}\n")

    analyzer = None
    if voice_check and not args.dry_run:
        print("Loading BirdNET model for voice-checking (~10-20s)...")
        from analysis import BirdAnalyzer, partition_detections
        analyzer = BirdAnalyzer()
        print()

    uploaded = skipped_voice = missing_file = failed = 0

    for r in rows:
        name = r["clip_path"]
        local_path = os.path.join(config.CLIPS_DIR, name)
        label = f"{r['detected_at']:%Y-%m-%d %H:%M:%S} {r['common_name']}"

        if not os.path.isfile(local_path):
            print(f"  [missing] {label} -> {name} (file not on disk)")
            missing_file += 1
            continue

        if args.dry_run:
            note = ""
            if voice_check:
                note = " (would voice-check first)"
            print(f"  [would upload] {label} -> {name}{note}")
            uploaded += 1
            continue

        if voice_check:
            try:
                audio, sr = _load_mono(local_path)
                _, has_voice = partition_detections(
                    analyzer.analyze(audio, sr, when=r["detected_at"])
                )
            except Exception as e:
                print(f"  [failed] {label} -> {name} (voice-check error: {e})")
                failed += 1
                continue
            if has_voice:
                print(f"  [kept local] {label} -> {name} (human voice heard)")
                skipped_voice += 1
                continue

        tmp = None
        try:
            tmp = _reencode_pcm16(local_path)
            url = clips_s3.upload(name, tmp)
        except Exception as e:
            print(f"  [failed] {label} -> {name} (upload error: {e})")
            failed += 1
            continue
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        if not url:
            print(f"  [failed] {label} -> {name} (upload returned no URL)")
            failed += 1
            continue

        db.set_clip_url(r["id"], url)
        print(f"  [uploaded] {label} -> {url}")
        uploaded += 1

    print(
        f"\nDone. uploaded={uploaded} skipped_voice={skipped_voice} "
        f"missing_file={missing_file} failed={failed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
