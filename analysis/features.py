"""Windowed time-domain features on IMU accelerometer/gyroscope channels.

Matches the accelerometer feature set used by the Frontiers in Digital
Health (Imperial College, 2024) accelerometer cough-detection work: RMS,
peak absolute value, zero-crossing rate, signal energy and jerk magnitude,
each computed over a short sliding window.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Adjustable constants, not magic numbers scattered through the file.
WINDOW_S = 0.75
OVERLAP = 0.5

ACC_COLUMNS = ["acc_x", "acc_y", "acc_z"]
GYRO_COLUMNS = ["gyro_x", "gyro_y", "gyro_z"]


def accel_magnitude(imu: pd.DataFrame) -> np.ndarray:
    return np.sqrt((imu[ACC_COLUMNS] ** 2).sum(axis=1)).to_numpy()


def gyro_magnitude(imu: pd.DataFrame) -> np.ndarray | None:
    if not set(GYRO_COLUMNS).issubset(imu.columns):
        return None
    return np.sqrt((imu[GYRO_COLUMNS] ** 2).sum(axis=1)).to_numpy()


def _rate_hz(imu: pd.DataFrame) -> float:
    return 1.0 / imu["t_unix"].diff().median()


def _window_slices(n_samples: int, rate_hz: float, window_s: float, overlap: float):
    window_n = max(int(round(window_s * rate_hz)), 2)
    step_n = max(int(round(window_n * (1 - overlap))), 1)
    for start in range(0, n_samples - window_n + 1, step_n):
        yield start, start + window_n


def _window_features(values: np.ndarray, rate_hz: float, prefix: str) -> dict:
    jerk = np.diff(values) * rate_hz
    zero_crossings = np.sum(np.diff(np.sign(values)) != 0)
    return {
        f"{prefix}_rms": float(np.sqrt(np.mean(values ** 2))),
        f"{prefix}_peak_abs": float(np.max(np.abs(values))),
        f"{prefix}_zero_crossing_rate": float(zero_crossings / len(values)),
        f"{prefix}_energy": float(np.sum(values ** 2)),
        f"{prefix}_jerk_rms": float(np.sqrt(np.mean(jerk ** 2))) if len(jerk) else 0.0,
    }


def extract_features(imu: pd.DataFrame, window_s: float = WINDOW_S, overlap: float = OVERLAP) -> pd.DataFrame:
    """One row of features per window, with the window's t_unix span."""
    rate_hz = _rate_hz(imu)
    acc_mag = accel_magnitude(imu)
    gyro_mag = gyro_magnitude(imu)
    t_unix = imu["t_unix"].to_numpy()

    rows = []
    for start, end in _window_slices(len(imu), rate_hz, window_s, overlap):
        row = {"t_start": t_unix[start], "t_end": t_unix[end - 1]}
        row.update(_window_features(acc_mag[start:end], rate_hz, "acc"))
        if gyro_mag is not None:
            row.update(_window_features(gyro_mag[start:end], rate_hz, "gyro"))
        rows.append(row)
    return pd.DataFrame(rows)
