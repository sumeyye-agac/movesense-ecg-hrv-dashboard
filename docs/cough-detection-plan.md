# Cough detection: what's left once the sensor is back

The offline pipeline (`analysis/`) is done and tested against synthetic
data. What's left needs the physical Movesense sensor: recording real
coughs, confirming candidate events by eye, training on them, and flashing
the result. See `PROGRESS.md` for what else is blocked (the firmware
toolchain track needs the gated `suunto-movesense-medical-sw` source, which
isn't on this machine).

## 1. Record

Use the existing dashboard (`README.md` → "Recording"). Settings:

- **IMU9 at 104 Hz** — 208 Hz recordings showed frequent GSP fragment
  mis-merges (spurious `clock_restarts`); 104 Hz is the validated clean rate.
- **Multiple separate `cough` sessions**, not one long one. Spread across
  sitting, standing, and a little walking. Aim for roughly 30–50
  individual coughs in total across all sessions.
- **At least as much recorded time labelled with real non-cough activity**
  — walking, sitting down, arm movement, talking, laughing,
  throat-clearing *without* coughing. This class determines the
  false-positive rate, so it deserves equal attention, not an
  afterthought. Label these sessions clearly as non-cough (e.g.
  `baseline`), since `find_candidate_events.py` only looks for `cough`.

This yields a **single-subject proof-of-concept**, not a claim that
generalizes to other people — say so plainly if this ever reaches a README,
rather than letting it imply otherwise.

## 2. Find candidates

```bash
pip install -r analysis/requirements.txt
python analysis/find_candidate_events.py path/to/cough_session.zip
```

This prints a list of candidate `t_unix` timestamps and writes
`cough_session.candidates.png` — the accelerometer trace with each
candidate marked. Nothing here is confirmed yet; it's peak detection on
windowed energy, meant to save you from scrubbing through a raw trace by
hand.

## 3. Confirm candidates

Open the PNG next to the recording (or re-listen to/recall the session)
and decide which marked candidates are real coughs. Write the confirmed
ones' `t_unix` values to a plain text file, one per line, e.g.
`cough_session_events.txt`. Drop false positives; if a real cough was
missed, add its timestamp by hand.

## 4. Train

```bash
python analysis/train_cough_classifier.py \
  --cough session1.zip:session1_events.txt session2.zip:session2_events.txt \
  --baseline baseline1.zip baseline2.zip \
  --test-session session2.zip \
  --out cough_classifier.json
```

- `--cough` takes `recording.zip:events.txt` pairs.
- `--baseline` takes non-cough recordings (no events file — every window
  is a negative).
- `--test-session` holds out whole recordings for evaluation, so
  precision/recall reflect a session the model never trained on, not
  adjacent windows of the same cough leaking across train/test. Repeat
  with a different session held out to sanity-check the numbers aren't
  a fluke of one split.

The output JSON (`feature_order`, `weights`, `bias`, `threshold`) is a
plain logistic-regression fit — no framework needed to consume it.

## 5. Drop the coefficients into firmware

`samples/cough_counter_app`'s `onNotify` currently runs an explicit
placeholder threshold (see the comment marked `PLACEHOLDER` in its
source) pending exactly this file. Replace its body with the trained
linear rule: compute the same features (`analysis/features.py`) over each
incoming window, take the dot product with `weights`, add `bias`, and
compare against `threshold`. Keep the feature computation in the same
order as `feature_order` in the JSON — the model has no names at
inference time, only positions.

## 6. Rebuild and flash

Same `cmake`/`ninja` invocation as the original toolchain smoke test
(`docker run` against `movesense/sensor-build-env`, pointed at
`cough_counter_app`), producing a fresh DFU `.zip`. Flashing needs the
physical sensor present and is not something to run unattended — confirm
before doing it, same as any other DFU update to this sensor.
