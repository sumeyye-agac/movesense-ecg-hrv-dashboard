# Cough detection: status and roadmap

_Last updated: 2026-09-21. Written after the first full pass on real hardware
data — 12 recording sessions, a cross-validated model — with no firmware work
started yet. This is meant to replace guessing with what's actually verified
against the sensor, the code, and GitHub itself._

## The two-track split (still the plan)

- **Track A — on-device (firmware).** The real goal: the sensor itself counts
  coughs, no phone or laptop needed. Blocked, unstarted (see below).
- **Track B — offline pipeline.** A stand-in that runs on recorded data on a
  laptop, to prove the approach before touching firmware. This is what has
  real results now.

## Where the code actually is right now (important)

As of this commit, everything below is on `origin/main`: the class-weight
fix (`d7c98fa`), the cross-validation script (`87bb71a`), and this status
doc itself (`analysis/train_cough_classifier.py` on GitHub now has
`class_weight="balanced"`, and `analysis/cross_validate.py` is there too).
For a while after they were first written, both sat as local-only commits
while `origin/main` was still at `bc2427f` — worth remembering, since
that's exactly the kind of gap this document exists to catch, but it's
closed now.

## What got fixed along the way

Three real, separate bugs, each found and fixed with an independent check (a
debug print, a replay, or a purpose-built verification script) rather than by
trusting the first theory:

| bug | root cause | fix | commit |
|---|---|---|---|
| ECG/IMU9 not streaming | Wrong GATT path case (`/Meas/Ecg/` vs `/Meas/ECG/`) and wrong ECG rate (200 Hz asked, only 125 Hz supported by this sensor) | Corrected path + rate | pre-dates this log |
| Garbage IMU9 values at 104 Hz+ (e.g. `2e32`) | GSP fragments packets above 104 Hz; each half was decoded on its own instead of reassembled | Buffer `DATA` (0x02) until `DATA_PART2` (0x03) arrives | `0e4d779` |
| Recordings logging 600–967 false "clock restarts" | `recording.py` compared timestamps across ECG/IMU9/temp using **one shared** last-seen clock, so interleaved streams looked like the clock jumped backward | Track the last-seen device clock **per stream** | `227cde9` |
| Rate dropdown silently reverting (e.g. 208→52 Hz) while not recording | A 5-second background status poll reset the dropdown on any mismatch, even outside a recording | Only reset it while a recording is actually running | `69d696d` |

The clock-restart bug is worth calling out on its own: it was first
misdiagnosed as "the fragmentation fix doesn't fully work at 208 Hz," which led
to dropping IMU9 to 104 Hz and re-recording all 12 sessions for nothing.
`analysis/verify_recording.py` (`20a1410`) — which checks accelerometer
magnitude directly against physics (`[3, 25] m/s²`) instead of trusting the
recorder's own counters — is what showed the "corrupted-looking" recordings
actually had zero implausible samples, pointing at the counter, not the data.
Once the real bug was found, IMU9 went back to 208 Hz (`bc2427f`) and all 12
sessions were re-recorded a second time.

## Data collected

One subject, 12 sessions, real Movesense hardware, IMU9 at 208 Hz. Six
sessions labelled `cough`, six labelled `baseline`:

| session | role | raw candidates | confirmed events | used in training? |
|---|---|---|---|---|
| `cough_metronomic_01` | forced/paced coughing | 9 | 8 | yes |
| `cough_metronomic_02` | forced/paced coughing | 9 | 8 | yes |
| `cough_natural_01_sitting` | natural coughing, sitting | 9 | 9 | yes |
| `cough_natural_02_sitting` | natural coughing, sitting | 12 | 8 | yes |
| `cough_natural_03_standing` | natural coughing, standing | 10 | 10 | yes |
| `cough_natural_04_walking` | natural coughing, walking | — | — | **no — excluded, see below** |
| `baseline_calm_01_sitting` | quiet, no coughing | – | 0 (all negative) | yes |
| `baseline_calm_02_standing` | quiet, no coughing | – | 0 | yes |
| `baseline_movement_01_walking` | walking, no coughing | – | 0 | yes |
| `baseline_movement_02_reaching` | arm movement, no coughing | – | 0 | yes |
| `baseline_confound_01_talking` | talking/laughing, no coughing | – | 0 | yes (held out in every CV fold, see below) |
| `baseline_confound_02_throatclear` | throat-clearing, no coughing | – | 0 | yes |

**43 confirmed coughs** across the 5 training sessions. `cough_natural_04_walking`
was recorded but dropped entirely from training: the candidate finder returned
5 more candidates than expected in a session done walking in circles indoors,
and walking's own motion signature isn't separable from a cough's using the
current feature set — labelling it would have injected unreliable ground truth
into the positive class rather than fixed anything. This is real, open work
(see "What's actually left"), not swept under the rug.

Also worth being upfront about: this is a **single-subject proof of concept**,
recorded entirely indoors (including the walking sessions, done in a room, not
outdoors), by design at this stage. Nothing here claims to generalize to
another person or a different environment.

## How events were labelled — not by eye

1. `find_candidate_events.py` peak-detects on windowed accelerometer energy
   (0.75 s windows, 50% overlap) and plots a PNG of the trace with every
   candidate marked. Its output is candidates, never "confirmed coughs."
2. Every candidate was checked against the recording session's own written
   note (cough count and rough timing, taken during recording, not
   reconstructed afterward).
3. Where two candidates landed close together (metronomic pacing, or a
   natural double-cough), the decision to merge them into one event was
   **quantified, not eyeballed**: measuring how far the signal between the two
   peaks deviates from the quiet-baseline noise level, in multiples of that
   baseline's own standard deviation. Every close pair checked came back at
   36–88× the baseline std with the signal never settling in between — a
   single sustained event, not two. One three-candidate cluster
   (`cough_natural_02_sitting`, candidates near t=528.9/530.4/531.5s) split
   the difference correctly: the first two merge (36.6× std, no settling),
   but the third stays separate because the signal genuinely drops back under
   3× baseline std for multiple stretches before it — a real second event,
   not an artifact of the peak detector's window.
4. Confirmed timestamps go into `analysis/data/candidates/*_events.txt`
   (plain text, one `t_unix` per line). These and the source recordings never
   leave this machine — `analysis/data/` is gitignored; it's personal
   sensor/health data.

## The model

10 features per 0.75 s window (50% overlap): RMS, peak absolute value,
zero-crossing rate, energy, and jerk RMS — each computed once on accelerometer
magnitude and once on gyroscope magnitude. This set matches the
accelerometer-based cough-detection approach in Frontiers in Digital Health
(Imperial College, 2024) rather than something invented from scratch.

`sklearn.LogisticRegression`, not a deep model — deliberately, both because a
linear model's coefficients are trivial to drop into the nRF52832's firmware
later (a dot product and a threshold, no framework needed) and because 43
positive examples isn't enough data to justify anything larger.

Held-out (by session, so no window from the same cough leaks across
train/test) evaluation of the unweighted model came back **worse than doing
nothing**: 91.5% accuracy against a 94.6% always-predict-negative baseline on
the same test split, because positives are only ~5% of windows. Adding
`class_weight="balanced"` (commit `d7c98fa`) trades that:
recall goes from 24% to 81% (it actually catches most real coughs now), at the
cost of accuracy dropping further below the trivial baseline (82.4%) and
precision staying low (21%). Given the actual goal is catching coughs, not
maximizing accuracy on an imbalanced set, that's the right direction — but
neither version is usable yet, and accuracy is never reported below without
the baseline next to it, because on this dataset it's misleading on its own.

## The real numbers — leave-one-session-out, not one lucky split

A single train/test split gives one precision/recall reading off as few as 37
positive windows — not enough to trust as *the* number.
`analysis/cross_validate.py` (commit `87bb71a`) holds out
each of the 5 cough sessions in turn as the test session, keeping the other 4
plus all baselines in training, and reports every fold:

| fold | held-out session | positive windows | precision | recall | accuracy | confound false positives (of 350 windows) |
|---|---|---|---|---|---|---|
| 1 | `cough_metronomic_01` | 39 | 0.191 | 0.949 | 0.765 | 95 |
| 2 | `cough_metronomic_02` | 40 | 0.154 | 0.650 | 0.768 | 91 |
| 3 | `cough_natural_01_sitting` | 44 | 0.219 | 0.727 | 0.815 | 83 |
| 4 | `cough_natural_02_sitting` | 37 | 0.210 | 0.811 | 0.824 | 94 |
| 5 | `cough_natural_03_standing` | 47 | 0.211 | 0.787 | 0.780 | 91 |
| **mean** | | | **0.197** | **0.785** | **0.790** | **90.8** |
| **range** | | | [0.154, 0.219] | [0.650, 0.949] | [0.765, 0.824] | [83, 95] |

`baseline_confound_01_talking` (talking/laughing, no coughs) is added to every
fold's test set and never trained on. Its false-positive rate — roughly **26%
of its 350 windows, in every single fold** — is the honest headline finding
here: the low precision isn't one bad session or an unlucky split, it's
systemic. The model currently can't separate a cough's motion signature from
talking or laughing. Recall is more encouraging and reasonably consistent
(65–95% across folds, mean 78.5%) — it does catch most real coughs — but at
this precision, a device using this model as-is would be right about 1 time in
5 when it flags a cough, and a large share of the wrong flags come
specifically from ordinary talking.

**What this does and doesn't show:** it shows the pipeline — record, label,
extract features, train, evaluate — works end to end on real hardware data,
and it shows recall is workable while precision has a specific, identified
bottleneck (speech/laughter motion) rather than being uniformly bad. It does
not show a cough detector ready for someone to wear: one subject, 43 events,
and a precision this low are not that.

## What's actually left

**Track A (firmware) — not started at all.** Per `PROGRESS.md` (dated
2026-09-12, itself now stale and due an update): Docker is installed, but the
gated `suunto-movesense-medical-sw` source tree — needed for the
`movesense/sensor-build-env` toolchain and the `jumpmeter_app` template to
copy into `cough_counter_app` — isn't on this machine. Nothing beyond that has
been attempted, and nothing should be guessed at without it.

**Track B — next honest experiment:** record more `baseline_confound`-style
sessions specifically varying speech and laughter (different volumes,
sentence lengths, laugh types), to test whether the false-positive rate on
talking comes down with more negative examples of exactly that kind, rather
than more data of any kind.

**Not urgent, explicitly deferred:** `cough_natural_04_walking` stays
excluded. Separating a cough from walking's own rhythmic motion likely needs a
different feature (e.g. isolating a cough's characteristic frequency band from
stride-rate motion) rather than more data with the current feature set —
flagged, not solved.

## Where things live

```
analysis/
  load_recording.py          # loads a recording ZIP's imu.csv (pushed)
  features.py                 # 10-feature extraction, 0.75s/50% windows (pushed)
  find_candidate_events.py    # peak-detection candidate finder (pushed)
  train_cough_classifier.py   # LogisticRegression trainer (pushed, with
                               #   class_weight="balanced", commit d7c98fa)
  cross_validate.py           # leave-one-session-out CV (87bb71a)
  verify_recording.py         # independent accel-magnitude sanity check (pushed, 20a1410)
  test_synthetic.py           # code-correctness tests on fabricated data (pushed)
  data/                        # gitignored -- raw recordings + confirmed_events.json +
                               #   cough_classifier.json. Personal health data. Never committed.
docs/
  cough-detection-plan.md     # the how-to (pushed, but written before real data existed)
  cough-detection-status.md   # this file
PROGRESS.md                    # dated log against the plan (pushed, last entry 2026-09-12,
                               #   predates all of the above -- needs a new entry)
```
