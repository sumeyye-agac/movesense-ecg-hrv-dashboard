"""Train a logistic-regression cough classifier from confirmed events.

Takes one or more "cough" recordings plus their confirmed event timestamps
(the human-reviewed output of find_candidate_events.py) and one or more
non-cough recordings, builds a labelled windowed feature dataset, and
trains a small sklearn LogisticRegression -- matching the validated,
embeddable-sized approach from the literature rather than a deep model,
deliberately, for both statistical and on-device RAM-budget reasons.

The held-out split is done *by session* (recording), not by random row,
so adjacent windows of the same cough can't leak across train/test.

Usage:
    python train_cough_classifier.py \
        --cough session1.zip:session1_events.txt session2.zip:session2_events.txt \
        --baseline baseline1.zip baseline2.zip \
        --test-session session2.zip \
        --out cough_classifier.json

Each events file holds one confirmed cough t_unix timestamp per line.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, precision_score, recall_score

from features import extract_features
from load_recording import load_recording

# A window counts as a positive if it overlaps a confirmed cough timestamp
# within this margin -- coughs are brief but the window (features.WINDOW_S)
# is not point-sized, so an exact-timestamp match would miss real overlaps.
LABEL_MATCH_TOLERANCE_S = 0.5

NON_FEATURE_COLUMNS = {"t_start", "t_end", "label", "session"}


def _label_windows(features: pd.DataFrame, confirmed_t_unix: np.ndarray) -> np.ndarray:
    labels = np.zeros(len(features), dtype=int)
    for t in confirmed_t_unix:
        hit = (features["t_start"] - LABEL_MATCH_TOLERANCE_S <= t) & (
            t <= features["t_end"] + LABEL_MATCH_TOLERANCE_S
        )
        labels[hit.to_numpy()] = 1
    return labels


def _session_features(recording_zip: Path, confirmed_t_unix) -> pd.DataFrame:
    imu, _meta = load_recording(recording_zip)
    features = extract_features(imu)
    features["label"] = (
        _label_windows(features, confirmed_t_unix) if confirmed_t_unix is not None else 0
    )
    features["session"] = str(recording_zip)
    return features


def build_dataset(cough_specs, baseline_paths) -> pd.DataFrame:
    frames = []
    for zip_path, events_path in cough_specs:
        confirmed = np.loadtxt(events_path, ndmin=1) if events_path else np.array([])
        frames.append(_session_features(zip_path, confirmed))
    for zip_path in baseline_paths:
        frames.append(_session_features(zip_path, None))
    if not frames:
        raise ValueError("no recordings given -- need at least one --cough or --baseline")
    return pd.concat(frames, ignore_index=True)


def train(dataset: pd.DataFrame, test_sessions: set[str]):
    feature_cols = [c for c in dataset.columns if c not in NON_FEATURE_COLUMNS]
    train_df = dataset[~dataset["session"].isin(test_sessions)]
    test_df = dataset[dataset["session"].isin(test_sessions)]
    if train_df.empty:
        raise ValueError("no training rows left after holding out --test-session")

    model = LogisticRegression(max_iter=1000)
    model.fit(train_df[feature_cols], train_df["label"])

    metrics = {}
    if len(test_df):
        pred = model.predict(test_df[feature_cols])
        metrics = {
            "accuracy": accuracy_score(test_df["label"], pred),
            "precision": precision_score(test_df["label"], pred, zero_division=0),
            "recall": recall_score(test_df["label"], pred, zero_division=0),
        }
    return model, feature_cols, metrics


def serialize(model, feature_cols, threshold=0.5) -> dict:
    return {
        "feature_order": feature_cols,
        "weights": model.coef_[0].tolist(),
        "bias": float(model.intercept_[0]),
        "threshold": threshold,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cough", nargs="+", default=[], metavar="ZIP[:EVENTS]",
                         help="cough recording, optionally with its confirmed-events file")
    parser.add_argument("--baseline", nargs="+", default=[], type=Path,
                         help="non-cough recordings")
    parser.add_argument("--test-session", nargs="+", default=[],
                         help="recording path(s) to hold out for evaluation")
    parser.add_argument("--out", type=Path, default=Path("cough_classifier.json"))
    args = parser.parse_args(argv)

    cough_specs = []
    for spec in args.cough:
        zip_part, _, events_part = spec.partition(":")
        cough_specs.append((Path(zip_part), Path(events_part) if events_part else None))

    dataset = build_dataset(cough_specs, args.baseline)
    model, feature_cols, metrics = train(dataset, set(args.test_session))

    print(f"{len(dataset)} windows, {int(dataset['label'].sum())} positive")
    if metrics:
        print(f"held-out ({', '.join(args.test_session)}): {metrics}")
    else:
        print("no --test-session given -- trained on everything, no held-out evaluation")

    args.out.write_text(json.dumps(serialize(model, feature_cols), indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
