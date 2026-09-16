"""Verifies a recording ZIP's IMU9 samples directly, independent of meta.json.

meta.json's clock_restarts/timestamp_gap_anomalies come from the same
recording pipeline that can be affected by the GSP fragmentation bug this
checks for, so they're not trustworthy as the only signal. This instead
loads imu.csv and checks accelerometer magnitude sqrt(x^2+y^2+z^2) against
the same [3, 25] m/s^2 plausibility bound used live in
backend/movesense_ble.py::_decode_and_dispatch.

Usage:
    python analysis/verify_recording.py <path/to/recording.zip> [...]
    python analysis/verify_recording.py analysis/data/raw/*.zip
"""
from __future__ import annotations

import sys

import numpy as np

from load_recording import load_recording

ACCEL_MAGNITUDE_MIN_MS2 = 3.0
ACCEL_MAGNITUDE_MAX_MS2 = 25.0


def verify(path) -> dict:
    imu, meta = load_recording(path)
    magnitude = np.sqrt(imu.acc_x**2 + imu.acc_y**2 + imu.acc_z**2)
    implausible = (magnitude < ACCEL_MAGNITUDE_MIN_MS2) | (magnitude > ACCEL_MAGNITUDE_MAX_MS2)
    return {
        "path": path,
        "samples": len(imu),
        "implausible_samples": int(implausible.sum()),
        "implausible_pct": 100 * implausible.mean() if len(imu) else 0.0,
        "magnitude_min": float(magnitude.min()) if len(imu) else float("nan"),
        "magnitude_max": float(magnitude.max()) if len(imu) else float("nan"),
        "meta_clock_restarts": meta.get("clock", {}).get("clock_restarts"),
    }


def main(paths: list[str]):
    results = [verify(p) for p in paths]

    header = f'{"file":40} {"samples":9} {"implausible":12} {"pct":7} {"mag_min":10} {"mag_max":12} {"meta_restarts":13}'
    print(header)
    print("-" * len(header))
    for r in results:
        name = r["path"].split("/")[-1]
        print(
            f'{name:40} {r["samples"]:9} {r["implausible_samples"]:12} '
            f'{r["implausible_pct"]:6.2f}% {r["magnitude_min"]:10.2f} {r["magnitude_max"]:12.2f} '
            f'{str(r["meta_clock_restarts"]):13}'
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
