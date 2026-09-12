"""Loads a Movesense recording ZIP's IMU stream into a dataframe.

See docs/data-format.md for the ZIP layout. `t_unix` is the column that
doc recommends for analysis, so the returned frame is sorted on it -
packet delivery order is not guaranteed to be strictly chronological
under BLE jitter.
"""
from __future__ import annotations

import json
import zipfile

import pandas as pd


def load_recording(path) -> tuple[pd.DataFrame, dict]:
    """Return (imu dataframe, meta.json dict) for a recording ZIP."""
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("meta.json"))
        if "imu" not in meta.get("streams", {}):
            raise ValueError(f"{path} has no IMU stream")
        with z.open("imu.csv") as f:
            imu = pd.read_csv(f)

    return imu.sort_values("t_unix").reset_index(drop=True), meta
