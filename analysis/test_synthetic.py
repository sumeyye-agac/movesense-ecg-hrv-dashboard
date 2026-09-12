"""Synthetic-data tests for the analysis/ pipeline.

These fabricate a fake IMU recording matching the real ZIP schema (a sine-
wave "baseline" segment and sharp Gaussian-pulse "cough-like" events)
purely to prove the pipeline runs end-to-end without crashing and produces
sane shapes and types. Any accuracy-like number that shows up in an
assertion here is a code-correctness check on synthetic data, not a claim
about the real classifier's performance -- that can only come from real
recordings.

Run with: python -m unittest discover analysis
"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from features import extract_features
from find_candidate_events import find_candidates
from load_recording import load_recording
from train_cough_classifier import build_dataset, train

RATE_HZ = 208
DURATION_S = 20
PULSE_TIMES_S = [5.0, 12.0, 17.0]


def _synthetic_imu(pulse_times_s) -> pd.DataFrame:
    n = int(DURATION_S * RATE_HZ)
    t = np.arange(n) / RATE_HZ

    # Baseline: gravity on one axis plus a small sinusoidal wobble on the
    # others -- not a real recording, just something with non-zero,
    # non-random-looking motion to detect a pulse against.
    acc_x = 0.3 * np.sin(2 * np.pi * 1.5 * t)
    acc_y = 0.3 * np.cos(2 * np.pi * 1.2 * t)
    acc_z = 9.81 + 0.1 * np.sin(2 * np.pi * 0.8 * t)

    # A cough-like event: a short, sharp Gaussian pulse -- the impulsive
    # signature the literature's feature set is meant to catch.
    for pulse_t in pulse_times_s:
        acc_x = acc_x + 20.0 * np.exp(-0.5 * ((t - pulse_t) / 0.05) ** 2)

    zeros = np.zeros(n)
    t_unix = t + 1_700_000_000.0
    return pd.DataFrame({
        "t_s": t,
        "t_device_ms": t * 1000.0,
        "t_unix": t_unix,
        "t_recv_unix": t_unix,
        "acc_x": acc_x, "acc_y": acc_y, "acc_z": acc_z,
        "gyro_x": zeros, "gyro_y": zeros, "gyro_z": zeros,
        "magn_x": zeros, "magn_y": zeros, "magn_z": zeros,
    })


def _write_synthetic_zip(path: Path, label: str, pulse_times_s) -> None:
    imu = _synthetic_imu(pulse_times_s)
    meta = {
        "schema_version": 1,
        "label": label,
        "streams": {"imu": {"file": "imu.csv", "rate_hz": RATE_HZ, "samples": len(imu)}},
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("imu.csv", imu.to_csv(index=False))
        z.writestr("meta.json", json.dumps(meta))


class LoadRecordingTest(unittest.TestCase):
    def test_reads_imu_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "cough.zip"
            _write_synthetic_zip(zip_path, "cough", PULSE_TIMES_S)

            imu, meta = load_recording(zip_path)

            self.assertEqual(meta["label"], "cough")
            self.assertIn("acc_x", imu.columns)
            self.assertEqual(len(imu), DURATION_S * RATE_HZ)
            self.assertTrue(imu["t_unix"].is_monotonic_increasing)

    def test_missing_imu_stream_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "no_imu.zip"
            with zipfile.ZipFile(zip_path, "w") as z:
                z.writestr("meta.json", json.dumps({"streams": {}}))

            with self.assertRaises(ValueError):
                load_recording(zip_path)


class ExtractFeaturesTest(unittest.TestCase):
    def test_shapes_and_types(self):
        imu = _synthetic_imu(PULSE_TIMES_S)

        features = extract_features(imu)

        self.assertGreater(len(features), 0)
        for column in ("acc_rms", "acc_peak_abs", "acc_zero_crossing_rate", "acc_energy", "acc_jerk_rms"):
            self.assertIn(column, features.columns)
            self.assertTrue(np.isfinite(features[column]).all())

    def test_pulse_windows_have_higher_jerk_than_baseline(self):
        # Code-correctness check on fabricated data: the injected pulses
        # should visibly raise windowed jerk relative to the quiet
        # sinusoidal baseline -- jerk, not raw energy, is what should catch
        # a sharp impulsive event against a constant ~9.8 m/s^2 gravity
        # offset. This is not a real-world accuracy claim.
        baseline_jerk = extract_features(_synthetic_imu([]))["acc_jerk_rms"]
        pulse_jerk = extract_features(_synthetic_imu(PULSE_TIMES_S))["acc_jerk_rms"]

        self.assertGreater(pulse_jerk.max(), baseline_jerk.max() * 10)


class FindCandidatesTest(unittest.TestCase):
    def test_flags_windows_near_injected_pulses(self):
        imu = _synthetic_imu(PULSE_TIMES_S)
        features = extract_features(imu)

        candidates = find_candidates(features)

        self.assertGreater(len(candidates), 0)
        t0 = imu["t_unix"].iloc[0]
        candidate_offsets = sorted(t - t0 for t in candidates)
        for pulse_t in PULSE_TIMES_S:
            self.assertTrue(
                any(abs(c - pulse_t) < 1.0 for c in candidate_offsets),
                f"no candidate found near injected pulse at t={pulse_t}s",
            )


class TrainCoughClassifierTest(unittest.TestCase):
    def test_runs_end_to_end_on_synthetic_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cough_zip = tmp / "cough.zip"
            baseline_zip = tmp / "baseline.zip"
            events_path = tmp / "events.txt"

            _write_synthetic_zip(cough_zip, "cough", PULSE_TIMES_S)
            _write_synthetic_zip(baseline_zip, "baseline", pulse_times_s=[])
            t0 = _synthetic_imu(PULSE_TIMES_S)["t_unix"].iloc[0]
            events_path.write_text("\n".join(str(t0 + t) for t in PULSE_TIMES_S) + "\n")

            dataset = build_dataset([(cough_zip, events_path)], [baseline_zip])
            self.assertGreater(dataset["label"].sum(), 0)
            self.assertGreater((dataset["label"] == 0).sum(), 0)

            model, feature_cols, metrics = train(dataset, test_sessions=set())

            self.assertEqual(len(model.coef_[0]), len(feature_cols))
            self.assertEqual(metrics, {})  # no --test-session given


if __name__ == "__main__":
    unittest.main()
