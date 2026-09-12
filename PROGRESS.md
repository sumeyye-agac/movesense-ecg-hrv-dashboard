# Cough-detection progress log

Tracking progress against `docs/cough-detection-plan.md`. Newest entries on
top.

## 2026-09-12

**Track A — blocked at A2.** Docker is installed (`docker --version` →
29.7.2). The gated `suunto-movesense-medical-sw` source tree was not found
anywhere on disk — checked `~/Downloads`, `~/Desktop`, and a broader
`~/`-wide search (depth 4) for `*suunto*` / `*medical-sw*`, no matches, and
no `~/movesense-firmware-workspace/` exists yet. Per the plan's own
ambiguity rule, stopping this track here rather than guessing at a path.
**Needs from her:** the zip (or its extracted location) before A2–A7 can
proceed — smoke-testing the toolchain, copying the jumpmeter template into
`cough_counter_app`, wiring the placeholder detector, and compiling a DFU
zip all depend on it.

**Track B — done.** Added `analysis/` to the repo:
- `load_recording.py` — loads a recording ZIP's `imu.csv` into a
  dataframe, `t_unix` as the time axis, per `docs/data-format.md`.
- `features.py` — windowed time-domain features (RMS, peak abs, zero-
  crossing rate, energy, jerk magnitude) on configurable window/overlap.
- `find_candidate_events.py` — peak-detects candidate cough events from
  windowed energy on a `cough`-labelled recording, plots the trace with
  candidates marked to a PNG for human review. Output is candidates only,
  not confirmed coughs.
- `train_cough_classifier.py` — builds a labelled windowed dataset from
  confirmed events + non-cough recordings, trains a
  `LogisticRegression`, evaluates with a by-session train/test split,
  serializes weights/bias/threshold to JSON.
- `tests/test_synthetic.py` — synthetic sine-baseline + Gaussian-pulse CSV
  proving the pipeline runs end-to-end. Explicitly not a model-quality
  result (see the module docstring).
- `python -m unittest discover analysis` runs alongside the existing
  `python -m unittest discover backend`.

Dependencies added: `analysis/requirements.txt` (pandas, numpy, scipy,
scikit-learn, matplotlib), installed into the project's existing `.venv`.

**Track C — done.** `docs/cough-detection-plan.md` written: recording
protocol, how to run B3/B4, how to feed coefficients into A5's placeholder,
rebuild/flash steps.

**Gated source confirmed absent from the public repo.** `git status` /
`git ls-files` show no `medical-sw`, `MovesenseCoreLib`, or sample-app
material — nothing to check in, because nothing was ever placed inside
this repo's directory.
