"""Leave-one-cough-session-out cross-validation for the cough classifier.

A single train/test split (as train_cough_classifier.py does by default)
gives one precision/recall reading from a small held-out sample - with as
few as ~37 positive windows in a fold, that single number can be luck as
much as signal. This holds out each cough recording in turn as the sole
test session (keeping baseline recordings in training throughout) and
reports the resulting metrics per fold plus their mean and range, so the
spread across folds is visible instead of a single point estimate.

baseline_confound_01_talking (speech/laughter, no coughs) is added to
every fold's test set as a fixed extra piece, never trained on, so its
false-positive count - the largest source of false positives seen so far
(see prior single-split run: 94/113 FP came from this one file) - stays
visible fold by fold rather than being averaged away.

Usage:
    python cross_validate.py
"""
from __future__ import annotations

from pathlib import Path

from train_cough_classifier import build_dataset, train

DATA = Path("data/raw")
EVENTS = Path("data/candidates")

COUGH_SESSIONS = [
    ("cough_metronomic_01", DATA / "cough_metronomic_01.zip", EVENTS / "cough_metronomic_01_events.txt"),
    ("cough_metronomic_02", DATA / "cough_metronomic_02.zip", EVENTS / "cough_metronomic_02_events.txt"),
    ("cough_natural_01_sitting", DATA / "cough_natural_01_sitting.zip", EVENTS / "cough_natural_01_sitting_events.txt"),
    ("cough_natural_02_sitting", DATA / "cough_natural_02_sitting.zip", EVENTS / "cough_natural_02_sitting_events.txt"),
    ("cough_natural_03_standing", DATA / "cough_natural_03_standing.zip", EVENTS / "cough_natural_03_standing_events.txt"),
]

BASELINE_PATHS = [
    DATA / "baseline_calm_01_sitting.zip",
    DATA / "baseline_calm_02_standing.zip",
    DATA / "baseline_movement_01_walking.zip",
    DATA / "baseline_movement_02_reaching.zip",
    DATA / "baseline_confound_01_talking.zip",
    DATA / "baseline_confound_02_throatclear.zip",
]

# Kept out of training in every fold, added to every fold's test set - see
# module docstring.
FIXED_TEST_EXTRA = str(DATA / "baseline_confound_01_talking.zip")


def _session_breakdown(test_df, model, feature_cols, session):
    g = test_df[test_df["session"] == session].copy()
    g["pred"] = model.predict(g[feature_cols])
    tp = int(((g["label"] == 1) & (g["pred"] == 1)).sum())
    fn = int(((g["label"] == 1) & (g["pred"] == 0)).sum())
    fp = int(((g["label"] == 0) & (g["pred"] == 1)).sum())
    tn = int(((g["label"] == 0) & (g["pred"] == 0)).sum())
    return {"windows": len(g), "positive": int(g["label"].sum()), "tp": tp, "fn": fn, "fp": fp, "tn": tn}


def main():
    cough_specs = [(zip_path, events_path) for _, zip_path, events_path in COUGH_SESSIONS]
    dataset = build_dataset(cough_specs, BASELINE_PATHS)

    rows = []
    print(f"{'fold':5} {'test_session':28} {'pos_win':8} {'precision':10} {'recall':8} {'accuracy':9} {'confound_fp':11}")
    print("-" * 90)
    for i, (name, zip_path, _events_path) in enumerate(COUGH_SESSIONS, start=1):
        test_sessions = {str(zip_path), FIXED_TEST_EXTRA}
        model, feature_cols, metrics = train(dataset, test_sessions)

        test_df = dataset[dataset["session"].isin(test_sessions)]
        cough_breakdown = _session_breakdown(test_df, model, feature_cols, str(zip_path))
        confound_breakdown = _session_breakdown(test_df, model, feature_cols, FIXED_TEST_EXTRA)

        rows.append({
            "fold": i,
            "test_session": name,
            "positive_windows": cough_breakdown["positive"],
            "precision": metrics["precision"],
            "recall": metrics["recall"],
            "accuracy": metrics["accuracy"],
            "confound_fp": confound_breakdown["fp"],
        })
        print(f"{i:<5} {name:28} {cough_breakdown['positive']:<8} {metrics['precision']:<10.3f} "
              f"{metrics['recall']:<8.3f} {metrics['accuracy']:<9.3f} {confound_breakdown['fp']:<11}")

    precisions = [r["precision"] for r in rows]
    recalls = [r["recall"] for r in rows]
    accuracies = [r["accuracy"] for r in rows]
    confound_fps = [r["confound_fp"] for r in rows]

    print("-" * 90)
    print(f"{'mean':5} {'':28} {'':8} {sum(precisions)/len(precisions):<10.3f} "
          f"{sum(recalls)/len(recalls):<8.3f} {sum(accuracies)/len(accuracies):<9.3f} "
          f"{sum(confound_fps)/len(confound_fps):<11.1f}")
    print(f"{'range':5} {'':28} {'':8} [{min(precisions):.3f}, {max(precisions):.3f}]  "
          f"[{min(recalls):.3f}, {max(recalls):.3f}]  [{min(accuracies):.3f}, {max(accuracies):.3f}]  "
          f"[{min(confound_fps)}, {max(confound_fps)}]")

    return rows


if __name__ == "__main__":
    main()
