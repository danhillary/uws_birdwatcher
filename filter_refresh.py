"""Recompute and store the false-positive filter flags.

Every refresh is a full recompute: load all detections, the stored thresholds
and the manual species overrides, run ``filters.compute`` with every test on,
and replace ``detection_flags`` and ``species_quality`` in one transaction.
The compute takes a fraction of a second, so a full pass is simpler and safer
than tracking which bouts, species and days a new detection could change.

The listener runs this on a background thread every ``BW_FILTER_REFRESH``
seconds (default 120). The dashboard runs it after an admin saves thresholds
or overrides. Filtered SQL views (the phone widget) leave out detections newer
than the last refresh until it has judged them.

Command line:

    python -m filter_refresh              # refresh once and print a summary
    python -m filter_refresh --dry-run    # compute and print, write nothing
    python -m filter_refresh --validate   # replay the Oct 12, 2026 trial run
"""
import argparse
import sys
import threading
import time

import pandas as pd

import config
import db
import filters


def load_inputs(conn):
    """(detections DataFrame, stored config dict, overrides dict)."""
    df = db.all_detections(conn)
    cfg = db.filter_config(conn)
    overrides = {r["common_name"]: r["status"] for r in db.species_overrides(conn)}
    return df, cfg, overrides


def refresh(write=True):
    """Recompute every flag from the database and (by default) store them.
    Returns (filters.Result, seconds taken)."""
    t0 = time.monotonic()
    with db.get_conn() as conn:
        df, cfg, overrides = load_inputs(conn)
    result = filters.compute(df, cfg, filters.TESTS, overrides)
    if write:
        db.write_filter_results(result)
    return result, time.monotonic() - t0


def summary(result):
    det = result.detections
    if det.empty:
        return "no detections"
    kept = int(det["keep"].sum())
    species_raw = det["common_name"].nunique()
    species_kept = det.loc[det["keep"], "common_name"].nunique()
    return (f"kept {kept:,} of {len(det):,} detections, "
            f"{species_kept} of {species_raw} species")


def start_background(interval=None):
    """Refresh now and then every ``interval`` seconds on a daemon thread.
    Errors are printed and never propagate, so filtering can't disturb the
    capture loop. Returns the thread, or None when disabled (interval <= 0)."""
    interval = config.FILTER_REFRESH_SECONDS if interval is None else interval
    if interval <= 0:
        return None

    def loop():
        while True:
            try:
                result, secs = refresh()
                print(f"   (filters refreshed: {summary(result)} in {secs:.1f}s)")
            except Exception as e:
                print(f"   (filter refresh failed: {e})")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="filter-refresh", daemon=True)
    thread.start()
    return thread


# --- Validation against the trial run ------------------------------------
# The handover's trial ran on Oct 12, 2026 against the 17,619 detections up to
# this timestamp. It predates the mixed-flock exemption, so validation turns
# the exemption off. Targets per species: raw, after test 1, after tests 1-2,
# after tests 1-4. "About" values in the handover get a 2% tolerance.

TRIAL_CUTOFF = pd.Timestamp("2026-10-08 12:28:55")
TRIAL_TOTALS = {"raw": 17619, "kept": 8988, "species_raw": 128,
                "species_kept": 46}
TRIAL_TARGETS = {
    "Northern Cardinal": (8079, 5653, 5611, 5600),
    "Blue Jay": (1367, 1199, 1195, 1195),
    "American Robin": (828, 427, 416, 416),
    "Black Vulture": (624, 47, 47, 21),
    "Merlin": (248, 27, 27, 8),
    "Pileated Woodpecker": (281, 82, 82, 82),
    "Chimney Swift": (25, 19, 19, 19),
    "Red-tailed Hawk": (28, 5, 3, 0),
    "Veery": (47, 1, 1, 0),
}
APPROX = {("Northern Cardinal", 3), ("Blue Jay", 3), ("American Robin", 3)}
CADENCE_TARGETS = {"Northern Cardinal": (17, 1.75), "Blue Jay": (17, 1.74),
                   "Black Vulture": (96.5, 0.92)}


def validate(df):
    """Replay the trial on ``df`` and print a comparison. Returns True if every
    figure matches."""
    trial = df[pd.to_datetime(df["detected_at"]) <= TRIAL_CUTOFF]
    cfg = {"shadow_exempt_days": 0}
    stages = [
        filters.compute(trial, cfg, ()),
        filters.compute(trial, cfg, ("anchor",)),
        filters.compute(trial, cfg, ("anchor", "shadow")),
        filters.compute(trial, cfg),
    ]
    ok = True

    def check(label, got, want, approx=False):
        nonlocal ok
        tol = max(1, round(0.02 * want)) if approx else 0
        good = abs(got - want) <= tol
        ok &= good
        return f"{got:>6,}{'' if good else ' !=' + format(want, ',')}"

    final = stages[-1].detections
    print(f"Trial window: detections up to {TRIAL_CUTOFF} (mixed-flock exemption off)\n")
    print(f"  detections kept {check('kept', int(final['keep'].sum()), TRIAL_TOTALS['kept'])}"
          f" of {check('raw', len(final), TRIAL_TOTALS['raw'])}")
    print(f"  species kept    "
          f"{check('sk', final.loc[final['keep'], 'common_name'].nunique(), TRIAL_TOTALS['species_kept'])}"
          f" of {check('sr', final['common_name'].nunique(), TRIAL_TOTALS['species_raw'])}\n")

    print(f"  {'Species':22} {'Raw':>7} {'Test 1':>7} {'1-2':>7} {'1-4':>7}")
    for name, targets in TRIAL_TARGETS.items():
        cells = []
        for i, stage in enumerate(stages):
            d = stage.detections
            got = int((d["keep"] & (d["common_name"] == name)).sum())
            cells.append(check(name, got, targets[i], (name, i) in APPROX))
        print(f"  {name:22} " + " ".join(f"{c:>7}" for c in cells))

    print(f"\n  {'Cadence':22} {'Median gap':>11} {'CV':>6}")
    sp = stages[-1].species.set_index("common_name")
    for name, (gap, cv) in CADENCE_TARGETS.items():
        g, c = sp.at[name, "median_gap_s"], sp.at[name, "gap_cv"]
        good = abs(g - gap) <= 0.5 and abs(c - cv) <= 0.01
        ok &= good
        print(f"  {name:22} {g:>11.1f} {c:>6.2f}{'' if good else '  != target'}")

    default = filters.compute(trial)
    dk = default.detections
    print(f"\n  With default settings (exemption on): kept "
          f"{int(dk['keep'].sum()):,} detections, "
          f"{dk.loc[dk['keep'], 'common_name'].nunique()} species.")
    print("\nPASS" if ok else "\nFAIL: a rule differs from the trial run")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, but don't write the tables")
    ap.add_argument("--validate", action="store_true",
                    help="replay the trial run and compare to its figures")
    args = ap.parse_args(argv)

    db.init_db()
    if args.validate:
        with db.get_conn() as conn:
            df = db.all_detections(conn)
        return 0 if validate(df) else 1

    result, secs = refresh(write=not args.dry_run)
    print(f"{'Computed' if args.dry_run else 'Refreshed'}: {summary(result)} "
          f"in {secs:.1f}s")
    sp = result.species
    cols = ["common_name", "raw", "kept", "pct_kept", "top_removed_by",
            "median_gap_s", "gap_cv", "cadence_flag"]
    with pd.option_context("display.width", 140, "display.max_rows", 200):
        print(sp[cols].head(25).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
