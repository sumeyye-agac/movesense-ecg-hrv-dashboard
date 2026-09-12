"""Propose candidate cough timestamps from a recording labelled "cough".

Peak-detects on windowed accelerometer energy and plots the accelerometer
trace with candidates marked, saved as a PNG for a human to visually
confirm or reject afterwards (see docs/cough-detection-plan.md). This
script's output is a list of *candidates* -- nothing here claims they are
confirmed coughs.

Usage: python find_candidate_events.py recording.zip
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import find_peaks

from features import ACC_COLUMNS, accel_magnitude, extract_features
from load_recording import load_recording

# How far above the mean a window's energy must rise to count as a
# candidate, and the minimum gap between two candidates -- coughs in the
# recording protocol (docs/cough-detection-plan.md) are spaced out, so
# anything closer than this is almost certainly the same event.
PROMINENCE_FACTOR = 2.0
MIN_GAP_S = 0.3


def find_candidates(features, prominence_factor=PROMINENCE_FACTOR, min_gap_s=MIN_GAP_S):
    energy = features["acc_energy"].to_numpy()
    threshold = energy.mean() + prominence_factor * energy.std()
    window_step_s = features["t_start"].diff().median() or 1.0
    distance = max(int(round(min_gap_s / window_step_s)), 1)
    peaks, _ = find_peaks(energy, height=threshold, distance=distance)
    return features.iloc[peaks]["t_start"].to_numpy()


def plot(imu, candidates, out_path):
    t0 = imu["t_unix"].iloc[0]
    acc_mag = accel_magnitude(imu)
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(imu["t_unix"] - t0, acc_mag, linewidth=0.5)
    for t in candidates:
        ax.axvline(t - t0, color="red", alpha=0.4)
    ax.set_xlabel("seconds since recording start")
    ax.set_ylabel("accelerometer magnitude (m/s^2)")
    ax.set_title(f"{len(candidates)} candidate event(s) -- UNCONFIRMED")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording_zip", type=Path)
    parser.add_argument("--out", type=Path, default=None,
                         help="PNG path (default: alongside the recording)")
    args = parser.parse_args(argv)

    imu, meta = load_recording(args.recording_zip)
    if meta.get("label") != "cough":
        print(f"warning: recording label is {meta.get('label')!r}, not 'cough'", file=sys.stderr)

    features = extract_features(imu)
    candidates = find_candidates(features)
    out_path = args.out or args.recording_zip.with_suffix(".candidates.png")
    plot(imu, candidates, out_path)

    print(f"{len(candidates)} candidate event(s) -- UNCONFIRMED, review {out_path} by eye")
    t0 = imu["t_unix"].iloc[0]
    for t in candidates:
        print(f"  t_unix={t:.3f}  t_s={t - t0:.3f}")


if __name__ == "__main__":
    main()
