"""Buffers live sensor data during a recording and exports it as a ZIP of CSVs.

The output is meant to be fed to machine learning, which drives two
decisions that the live dashboard never had to care about:

1) **Per-sample timestamps, not per-packet ones.** BLE delivers ECG and
   IMU9 in batches - one packet carries a single device timestamp and
   ~16 samples. Stamping every sample in a packet with the packet's
   arrival time produces a staircase, which corrupts inter-beat
   intervals and anything else derived from sample spacing. Sample i in a
   packet is placed at `packet_timestamp + i * step` instead, where step
   is measured from the gap to the *next* packet rather than assumed from
   the rate we subscribed at - those two disagree in practice (IMU9 asked
   for at 52 Hz delivers about 54).

2) **Two clocks, kept separate.** The sensor's own monotonic clock has
   accurate spacing but is relative to device boot; the host's wall clock
   is absolute but carries BLE transmission and batching jitter. Rather
   than pick one, every row carries both:

     t_s          seconds since recording start (convenience axis)
     t_device_ms  the sensor's own clock, unwrapped
     t_unix       absolute time derived from the device clock, anchored
                  to the host clock once at the start of the recording
                  - this is the one to use for analysis
     t_recv_unix  when the packet actually reached this machine
                  - diagnostic: the gap against t_unix is BLE latency

   Heart rate is the exception. It arrives over the standard Bluetooth
   Heart Rate Service, which carries no device timestamp at all, so
   t_device_ms is empty there and t_unix can only equal t_recv_unix.

Buffers hold raw packets, not expanded rows - expansion happens once at
export. A ten-minute recording at 125 Hz is a few thousand packets.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone

# The device clock is a uint32 of milliseconds, so it wraps roughly every
# 49.7 days of sensor uptime. A backward jump larger than this threshold
# is a wrap; anything smaller is just two streams' packets interleaving.
_UINT32 = 2 ** 32
_WRAP_THRESHOLD = 2 ** 31

ALL_STREAMS = ("ecg", "imu", "hr", "temp")

# Buffers live in memory and export costs roughly another two copies of
# them (the CSV text, then the ZIP), so an unbounded recording is an
# out-of-memory crash that takes the whole capture with it. Two million
# samples is about four hours of 125 Hz ECG, or an hour at 500 Hz. On
# reaching it the recording stops accepting samples and says so, which is
# recoverable; silently dying is not.
MAX_SAMPLES_PER_STREAM = 2_000_000

# A backward jump in the device clock bigger than this is a uint32 wrap.
# Anything smaller but still substantial means the sensor restarted and
# its clock went back to zero - a different problem needing a different fix.
_CLOCK_RESTART_THRESHOLD_MS = 1_000

# Time columns are identical across every CSV so one loader can read them
# all. t_device_ms is left empty where the stream has no device clock.
_TIME_COLUMNS = ["t_s", "t_device_ms", "t_unix", "t_recv_unix"]


def _fmt(value, digits):
    return "" if value is None else f"{value:.{digits}f}"


class Recorder:
    """Collects sensor packets between start() and stop().

    record_* runs on bleak's callback thread while start/stop run on the
    event loop, so every transition takes the lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = False
        self._finished: dict[str, dict] = {}

        self._reset()

    def _reset(self):
        self.recording_id = None
        self.label = ""
        self.streams: tuple[str, ...] = ()
        self.ecg_hz = 0
        self.imu_hz = 0
        self.device_address = None

        self.started_unix = 0.0
        self.stopped_unix = 0.0

        # Device-clock anchor, set by the first packet that carries one.
        self._anchor_unix = None
        self._anchor_device_ms = None

        # uint32 unwrap state, shared across streams (one device clock).
        self._wrap_offset = 0
        self._last_raw_device_ms = None

        # Raw packets: expanded into rows at export time. t_unix is resolved
        # here at capture rather than at export, because a sensor reboot
        # re-anchors the clock and recomputing an old packet against a newer
        # anchor would silently redate everything recorded before it.
        self._ecg: list[tuple] = []   # (device_ms, t_unix, recv_unix, [mv, ...])
        self._imu: list[tuple] = []   # (device_ms, t_unix, recv_unix, acc, gyro, magn)
        self._temp: list[tuple] = []  # (device_ms, t_unix, recv_unix, celsius)
        self._hr: list[tuple] = []    # (recv_unix, bpm, sdnn_ms, [rr_ms, ...])

        # Running totals. status() is polled once a second during a
        # recording; re-summing every buffered packet each time turns a
        # status poll into an O(recording length) walk.
        self._ecg_samples = 0
        self._imu_samples = 0

        self._gaps: list[dict] = []
        self._summary_warnings: list[str] = []
        self._clock_restarts = 0
        self._truncated: set[str] = set()
        self._last_device_row = None  # (device_ms, recv_unix) for drift at stop

    # --- lifecycle -----------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._active

    def start(self, streams, label, ecg_hz, imu_hz, device_address=None):
        with self._lock:
            if self._active:
                raise RuntimeError("a recording is already running")
            self._reset()
            self.recording_id = uuid.uuid4().hex[:12]
            self.label = label or ""
            self.streams = tuple(s for s in ALL_STREAMS if s in streams)
            self.ecg_hz = ecg_hz
            self.imu_hz = imu_hz
            self.device_address = device_address
            self.started_unix = time.time()
            self._active = True
            return self.recording_id

    def stop(self):
        with self._lock:
            if not self._active:
                raise RuntimeError("no recording is running")
            self._active = False
            self.stopped_unix = time.time()

        # Clearing _active above freezes the buffers - record_* returns
        # immediately now - so the CSV and ZIP work happens outside the
        # lock. Holding it here would stall bleak's callback thread for
        # as long as the export takes.
        summary = self._summary()
        self._summary_warnings = summary["warnings"]
        self._finished[self.recording_id] = {
            "summary": summary,
            "zip": self._build_zip(),
        }
        # Bound memory: a finished ZIP is held only until the next few
        # recordings push it out.
        while len(self._finished) > 3:
            self._finished.pop(next(iter(self._finished)))
        return summary

    def note_gap(self, reason: str):
        """Record that the data stream was interrupted (e.g. BLE dropped)."""
        with self._lock:
            if self._active:
                self._gaps.append({"unix": time.time(), "reason": reason})

    def get_finished(self, recording_id: str):
        return self._finished.get(recording_id)

    # --- clock ---------------------------------------------------------

    def _device_time(self, raw_device_ms: int, recv_unix: float) -> int:
        """Turn a raw packet timestamp into a continuous device-clock value.

        Two discontinuities have to be told apart. The clock is a uint32 of
        milliseconds, so it wraps every ~49.7 days of uptime and jumps back
        by nearly 2^32; that is recoverable by adding the wrap. But if the
        sensor loses power and reconnects, its clock restarts near zero -
        a small backward jump. Treating that as a wrap would leave every
        later sample dated an hour before the recording began, quietly and
        with no error. So re-anchor instead, keeping the timeline monotonic
        and noting it happened.
        """
        previous = self._last_raw_device_ms
        if previous is not None:
            if raw_device_ms < previous - _WRAP_THRESHOLD:
                self._wrap_offset += _UINT32
            elif raw_device_ms < previous - _CLOCK_RESTART_THRESHOLD_MS:
                self._clock_restarts += 1
                self._gaps.append({
                    "unix": recv_unix,
                    "reason": "device clock restarted - the sensor appears to have "
                              "rebooted; timestamps were re-anchored here",
                })
                self._wrap_offset = 0
                self._anchor_unix = None  # forces a fresh anchor below

        self._last_raw_device_ms = raw_device_ms
        device_ms = raw_device_ms + self._wrap_offset

        if self._anchor_unix is None:
            self._anchor_unix = recv_unix
            self._anchor_device_ms = device_ms
        return device_ms

    def _to_unix(self, device_ms: float) -> float:
        return self._anchor_unix + (device_ms - self._anchor_device_ms) / 1000.0

    def _over_budget(self, stream: str, buffered: int) -> bool:
        if buffered < MAX_SAMPLES_PER_STREAM:
            return False
        if stream not in self._truncated:
            self._truncated.add(stream)
        return True

    # --- capture -------------------------------------------------------

    def record_ecg(self, packet_ts_ms: int, mv_samples: list, recv_unix: float):
        with self._lock:
            if not self._active or "ecg" not in self.streams:
                return
            if self._over_budget("ecg", self._ecg_samples):
                return
            device_ms = self._device_time(packet_ts_ms, recv_unix)
            self._ecg.append((device_ms, self._to_unix(device_ms), recv_unix, mv_samples))
            self._ecg_samples += len(mv_samples)
            self._last_device_row = (self._to_unix(device_ms), recv_unix)

    def record_imu(self, packet_ts_ms: int, acc, gyro, magn, recv_unix: float):
        with self._lock:
            if not self._active or "imu" not in self.streams:
                return
            if self._over_budget("imu", self._imu_samples):
                return
            device_ms = self._device_time(packet_ts_ms, recv_unix)
            self._imu.append((device_ms, self._to_unix(device_ms), recv_unix, acc, gyro, magn))
            self._imu_samples += len(acc)
            self._last_device_row = (self._to_unix(device_ms), recv_unix)

    def record_temp(self, packet_ts_ms: int, celsius: float, recv_unix: float):
        with self._lock:
            if not self._active or "temp" not in self.streams:
                return
            if self._over_budget("temp", len(self._temp)):
                return
            device_ms = self._device_time(packet_ts_ms, recv_unix)
            self._temp.append((device_ms, self._to_unix(device_ms), recv_unix, celsius))

    def record_hr(self, bpm: int, sdnn_ms, rr_intervals_ms: list, recv_unix: float):
        with self._lock:
            if not self._active or "hr" not in self.streams:
                return
            if self._over_budget("hr", len(self._hr)):
                return
            self._hr.append((recv_unix, bpm, sdnn_ms, list(rr_intervals_ms)))

    # --- reporting -----------------------------------------------------

    def status(self):
        if not self._active:
            return {"recording": False}
        return {
            "recording": True,
            "recording_id": self.recording_id,
            "label": self.label,
            "elapsed_s": round(time.time() - self.started_unix, 1),
            "counts": self._counts(),
        }

    def _counts(self):
        counts = {}
        if "ecg" in self.streams:
            counts["ecg"] = self._ecg_samples
        if "imu" in self.streams:
            counts["imu"] = self._imu_samples
        if "temp" in self.streams:
            counts["temp"] = len(self._temp)
        if "hr" in self.streams:
            counts["hr"] = len(self._hr)
            counts["rr"] = sum(len(h[3]) for h in self._hr)
        return counts

    def _dropped_packets(self, packets, nominal_hz):
        """Count packets that don't butt up against their predecessor.

        Deliberately measured against the median observed packet cadence
        rather than the nominal rate: IMU9 subscribed at 52 Hz delivers
        about 54, so a nominal yardstick would either flag every packet or
        need a tolerance so wide it flags nothing.

        A packet is contiguous when its timestamp equals the previous
        one's plus that packet's own duration. This doubles as a check on
        the decoder's premise that a timestamp belongs to a packet's
        *first* sample - if it did not, nothing here would line up.
        """
        steps = self._sample_steps(packets, nominal_hz)
        usable = [step for step in steps if step]
        if len(packets) < 3 or not usable:
            return 0
        step = statistics.median(usable)
        tolerance = step / 2

        dropped = 0
        for previous, current in zip(packets, packets[1:]):
            expected = previous[0] + len(previous[3]) * step
            if abs(current[0] - expected) > tolerance:
                dropped += 1
        return dropped

    def _anomalies(self):
        return {
            "ecg": self._dropped_packets(self._ecg, self.ecg_hz),
            "imu": self._dropped_packets(self._imu, self.imu_hz),
        }

    def _drift_ms(self):
        """How far the device clock has slipped from the wall clock.

        Compares the last device-derived timestamp against that same
        packet's actual arrival time. Includes a constant BLE latency
        offset, so read the trend, not the absolute value.
        """
        if self._last_device_row is None:
            return None
        t_unix, recv_unix = self._last_device_row
        return round((t_unix - recv_unix) * 1000.0, 2)

    def _summary(self):
        duration = self.stopped_unix - self.started_unix
        counts = self._counts()
        warnings = []

        if not any(counts.values()):
            warnings.append("No samples were captured at all.")
        for stream in ("ecg", "imu"):
            if stream in self.streams and not counts.get(stream):
                warnings.append(
                    f"{stream.upper()} was selected but no packets arrived - "
                    f"the sampling rate may not be supported by this sensor."
                )
        if self._truncated:
            warnings.append(
                f"Hit the {MAX_SAMPLES_PER_STREAM:,}-sample ceiling on "
                f"{', '.join(sorted(s.upper() for s in self._truncated))}; those streams "
                f"stop early. Record in shorter sessions to capture the whole thing."
            )
        if self._clock_restarts:
            warnings.append(
                f"The sensor's clock restarted {self._clock_restarts} time(s), so it "
                f"probably rebooted mid-recording. Timestamps were re-anchored at each "
                f"restart - t_unix stays monotonic but the gap across them is not exact."
            )
        if self._gaps:
            warnings.append(f"{len(self._gaps)} interruption(s) during recording.")
        for name, packets, asked in (("ECG", self._ecg, self.ecg_hz), ("IMU9", self._imu, self.imu_hz)):
            measured = self._measured_hz(packets, asked)
            if measured and asked and abs(measured - asked) / asked > 0.01:
                warnings.append(
                    f"{name} was subscribed at {asked} Hz but the sensor delivered "
                    f"{measured} Hz. Sample spacing follows the measured rate; "
                    f"meta.json records both."
                )
        for stream, n in self._anomalies().items():
            if n:
                warnings.append(
                    f"{n} {stream.upper()} packet(s) did not follow on from the previous "
                    f"one - likely dropped packets, leaving gaps in the timeline."
                )
        return {
            "recording_id": self.recording_id,
            "label": self.label,
            "duration_s": round(duration, 1),
            "counts": counts,
            "streams": list(self.streams),
            "ecg_hz": self.ecg_hz,
            "imu_hz": self.imu_hz,
            "warnings": warnings,
            "suggested_filename": self._suggested_filename(),
        }

    def _suggested_filename(self):
        stamp = datetime.fromtimestamp(self.started_unix).strftime("%Y-%m-%d_%H-%M-%S")
        label = "".join(c if c.isalnum() or c in "-_" else "-" for c in self.label).strip("-")
        return f"movesense_{label}_{stamp}.zip" if label else f"movesense_{stamp}.zip"

    # --- export --------------------------------------------------------

    def _row_times(self, device_ms, t_unix, recv_unix):
        return [
            _fmt(t_unix - self.started_unix, 6),
            _fmt(device_ms, 3),
            _fmt(t_unix, 6),
            _fmt(recv_unix, 6),
        ]

    def _sample_steps(self, packets, nominal_hz):
        """Per-packet spacing between samples, in milliseconds.

        The rate we subscribed at is not necessarily the rate the sensor
        delivers. Asking for IMU9 at 52 Hz was observed producing four
        samples every 74 ms - about 54 Hz. Spacing samples on the nominal
        rate then makes each packet's samples drift ahead of the truth and
        snap back at the next packet's timestamp: a sawtooth in a column
        whose entire purpose is even spacing.

        Consecutive packet timestamps measure the real interval directly,
        so use those. The last packet has no successor, so it falls back
        to the median of the measured steps.
        """
        fallback = 1000.0 / nominal_hz if nominal_hz else 0.0
        if not packets:
            return []

        steps = []
        for index, packet in enumerate(packets):
            count = len(packet[3])
            if index + 1 < len(packets) and count:
                steps.append((packets[index + 1][0] - packet[0]) / count)
            else:
                steps.append(None)

        measured = [s for s in steps if s is not None]
        tail = statistics.median(measured) if measured else fallback
        return [tail if s is None else s for s in steps]

    def _measured_hz(self, packets, nominal_hz):
        steps = [s for s in self._sample_steps(packets, nominal_hz) if s]
        return round(1000.0 / statistics.median(steps), 3) if steps else None

    def _ecg_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + ["ecg_mv"])
        steps = self._sample_steps(self._ecg, self.ecg_hz)
        for (device_ms, t_unix, recv_unix, samples), step in zip(self._ecg, steps):
            for i, mv in enumerate(samples):
                offset = i * step
                w.writerow(
                    self._row_times(device_ms + offset, t_unix + offset / 1000.0, recv_unix)
                    + [_fmt(mv, 6)]
                )
        return out.getvalue()

    def _imu_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + [
            "acc_x", "acc_y", "acc_z",
            "gyro_x", "gyro_y", "gyro_z",
            "magn_x", "magn_y", "magn_z",
        ])
        steps = self._sample_steps(self._imu, self.imu_hz)
        for (device_ms, t_unix, recv_unix, acc, gyro, magn), step in zip(self._imu, steps):
            for i in range(len(acc)):
                offset = i * step
                values = list(acc[i]) + list(gyro[i]) + list(magn[i])
                w.writerow(
                    self._row_times(device_ms + offset, t_unix + offset / 1000.0, recv_unix)
                    + [_fmt(v, 6) for v in values]
                )
        return out.getvalue()

    def _temp_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + ["temp_c"])
        for device_ms, t_unix, recv_unix, celsius in self._temp:
            w.writerow(self._row_times(device_ms, t_unix, recv_unix) + [_fmt(celsius, 3)])
        return out.getvalue()

    def _hr_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + ["bpm", "sdnn_ms"])
        for recv_unix, bpm, sdnn_ms, _rr in self._hr:
            # No device clock on this path, so t_unix is arrival time.
            w.writerow([
                _fmt(recv_unix - self.started_unix, 6), "",
                _fmt(recv_unix, 6), _fmt(recv_unix, 6),
                bpm, _fmt(sdnn_ms, 1),
            ])
        return out.getvalue()

    def _rr_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + ["rr_ms", "index_in_packet"])
        for recv_unix, _bpm, _sdnn, rr_list in self._hr:
            for i, rr in enumerate(rr_list):
                w.writerow([
                    _fmt(recv_unix - self.started_unix, 6), "",
                    _fmt(recv_unix, 6), _fmt(recv_unix, 6),
                    _fmt(rr, 3), i,
                ])
        return out.getvalue()

    def _meta(self):
        files = {}
        counts = self._counts()
        if "ecg" in self.streams:
            files["ecg"] = {
                "file": "ecg.csv",
                "rate_hz": self.ecg_hz,
                "measured_rate_hz": self._measured_hz(self._ecg, self.ecg_hz),
                "samples": counts.get("ecg", 0),
            }
        if "imu" in self.streams:
            files["imu"] = {
                "file": "imu.csv",
                "rate_hz": self.imu_hz,
                "measured_rate_hz": self._measured_hz(self._imu, self.imu_hz),
                "samples": counts.get("imu", 0),
            }
        if "temp" in self.streams:
            files["temp"] = {"file": "temp.csv", "rate_hz": None, "samples": counts.get("temp", 0)}
        if "hr" in self.streams:
            files["hr"] = {"file": "hr.csv", "rate_hz": None, "samples": counts.get("hr", 0)}
            files["rr"] = {"file": "rr.csv", "rate_hz": None, "samples": counts.get("rr", 0)}

        return {
            "schema_version": 1,
            "label": self.label,
            "device_address": self.device_address,
            "started_utc": datetime.fromtimestamp(self.started_unix, timezone.utc).isoformat(),
            "stopped_utc": datetime.fromtimestamp(self.stopped_unix, timezone.utc).isoformat(),
            "duration_s": round(self.stopped_unix - self.started_unix, 3),
            "streams": files,
            "clock": {
                "anchor_unix": self._anchor_unix,
                "anchor_device_ms": self._anchor_device_ms,
                "drift_ms_at_stop": self._drift_ms(),
                "timestamp_gap_anomalies": self._anomalies(),
                "clock_restarts": self._clock_restarts,
                "interruptions": self._gaps,
            },
            "truncated_streams": sorted(self._truncated),
            "ecg_lsb_to_mv": 0.000381469726563,
            "column_notes": {
                "rate_hz": "the rate we subscribed at",
                "measured_rate_hz": "the rate the sensor actually delivered, taken from "
                                    "packet timestamps - sample spacing follows this, not "
                                    "rate_hz, because the two do not always agree",
                "t_s": "seconds since the recording started. The first row can be "
                       "slightly negative: streams share one device clock, whichever "
                       "packet arrives first anchors it, and another stream's packet "
                       "may carry an earlier timestamp because it was sampled before "
                       "the button was pressed and only delivered afterwards",
                "t_device_ms": "the sensor's own monotonic clock, uint32 wraps unwrapped; "
                               "empty for hr/rr, which have no device clock",
                "t_unix": "absolute time derived from the device clock, anchored to the "
                          "host clock at the first packet - use this one for analysis",
                "t_recv_unix": "when the BLE packet reached the host; samples from the same "
                               "packet share it, so the gap against t_unix is BLE latency",
                "rr_ms": "inter-beat intervals in delivery order; absolute beat times are "
                         "NOT reconstructed here because a dropped notification would make "
                         "a cumulative sum silently wrong",
            },
        }

    def _readme_md(self) -> str:
        """A human-readable companion to meta.json, written per recording.

        meta.json is what a loader parses; this is what a person opening
        the ZIP months later reads. Both carry the same facts because the
        two audiences need the same information in different shapes -
        dropping either one costs somebody something.
        """
        started = datetime.fromtimestamp(self.started_unix).strftime("%Y-%m-%d %H:%M:%S")
        title = f'Movesense recording - "{self.label}"' if self.label else "Movesense recording"

        lines = [
            f"# {title}",
            "",
            f"Captured {started} (local time), {self._format_duration()} long, from Movesense "
            f"sensor `{self.device_address or 'unknown'}`.",
            "",
        ]

        if self._summary_warnings:
            lines += ["## Read this first", ""]
            lines += [f"- {w}" for w in self._summary_warnings]
            lines += [""]

        lines += [
            "## What is in here",
            "",
            "| file | rows | contents |",
            "|---|---|---|",
        ]
        descriptions = self._file_descriptions()
        for filename, rows, description in descriptions:
            lines.append(f"| `{filename}` | {rows} | {description} |")
        lines += [
            "| `meta.json` | - | the same facts as this file, machine-readable |",
            "",
            "## The time columns",
            "",
            "Every CSV starts with the same four columns, so one loader reads them all.",
            "",
            "| column | meaning |",
            "|---|---|",
            "| `t_s` | seconds since the recording started - the axis to plot against |",
            "| `t_device_ms` | the sensor's own clock, in ms, with uint32 wraps undone |",
            "| `t_unix` | absolute time, derived from the device clock - **use this for analysis** |",
            "| `t_recv_unix` | when the Bluetooth packet reached the computer - diagnostic only |",
            "",
            "Two clocks are involved and neither alone is sufficient. The sensor's clock",
            "spaces samples accurately but counts from its own power-on; the computer's",
            "clock is absolute but is only stamped when a packet arrives, and Bluetooth",
            "delivers ECG and IMU9 in batches of many samples at once. `t_unix` combines",
            "them: the sensor's spacing, anchored to the computer's clock once at the",
            "start. `t_recv_unix` is kept so the gap between the two - the Bluetooth",
            "delivery latency - stays visible instead of being baked invisibly into the",
            "timeline. Samples from one packet share a `t_recv_unix`; that is expected.",
            "",
            "## Loading it",
            "",
            "```python",
            "import pandas as pd",
            "",
            'ecg = pd.read_csv("ecg.csv")',
            'ecg["t_unix"].diff().describe()   # sample spacing, should be near-constant',
            "```",
            "",
            "## Things to know before trusting it",
            "",
            "- **Sample spacing follows what the sensor actually delivered**, not what was",
            "  asked for. The two can differ - IMU9 subscribed at 52 Hz tends to deliver",
            "  about 54 - so `meta.json` records `rate_hz` and `measured_rate_hz`",
            "  separately for each stream.",
            "- **The first row's `t_s` can be a few milliseconds negative.** The streams",
            "  share one device clock and whichever packet arrives first anchors it;",
            "  another stream's packet may carry an earlier timestamp because it was",
            "  sampled just before recording began and only delivered just after.",
            "- **Magnetometer values are raw and uncalibrated.** They carry a hard-iron",
            "  offset, so their magnitude is not Earth's field strength. Calibrate by",
            "  rotating the sensor through many orientations and fitting a sphere.",
            "- **`rr.csv` holds inter-beat intervals in delivery order, not beat times.**",
            "  Absolute beat times are deliberately not reconstructed here: one dropped",
            "  notification would make a cumulative sum silently wrong.",
            "- **`hr.csv` and `rr.csv` have an empty `t_device_ms`.** They come over the",
            "  standard Bluetooth Heart Rate Service, which carries no device timestamp",
            "  at all, so `t_unix` there can only be arrival time.",
            "- **`sdnn_ms` is unfiltered** - the standard deviation of raw RR intervals",
            "  over a rolling window, with no ectopic-beat correction. For real HRV work,",
            "  compute it yourself from `rr.csv`.",
            "",
            "Written by the Movesense Live Dashboard.",
            "",
        ]
        return "\n".join(lines)

    def _format_duration(self) -> str:
        seconds = self.stopped_unix - self.started_unix
        if seconds < 90:
            return f"{seconds:.1f} s"
        return f"{int(seconds // 60)} min {int(seconds % 60)} s"

    def _file_descriptions(self):
        """(filename, row count, prose) for each CSV actually written."""
        counts = self._counts()
        out = []
        if "ecg" in self.streams:
            measured = self._measured_hz(self._ecg, self.ecg_hz)
            out.append((
                "ecg.csv", f"{counts.get('ecg', 0):,}",
                f"single-lead ECG in millivolts, one row per sample, "
                f"{self.ecg_hz} Hz requested / {measured or '-'} Hz delivered",
            ))
        if "imu" in self.streams:
            measured = self._measured_hz(self._imu, self.imu_hz)
            out.append((
                "imu.csv", f"{counts.get('imu', 0):,}",
                f"accelerometer (m/s²), gyroscope (°/s) and magnetometer (µT), "
                f"three axes each, {self.imu_hz} Hz requested / {measured or '-'} Hz delivered",
            ))
        if "hr" in self.streams:
            out.append((
                "hr.csv", f"{counts.get('hr', 0):,}",
                "heart rate in bpm plus a rolling unfiltered SDNN, about one row per second",
            ))
            out.append((
                "rr.csv", f"{counts.get('rr', 0):,}",
                "inter-beat (RR) intervals in ms, one row per beat",
            ))
        if "temp" in self.streams:
            out.append((
                "temp.csv", f"{counts.get('temp', 0):,}",
                "skin-side temperature in °C - the sensor only reports it when it changes",
            ))
        return out

    def _build_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # First in the archive so it is the first thing seen on opening it.
            z.writestr("README.md", self._readme_md())
            if "ecg" in self.streams:
                z.writestr("ecg.csv", self._ecg_csv())
            if "imu" in self.streams:
                z.writestr("imu.csv", self._imu_csv())
            if "temp" in self.streams:
                z.writestr("temp.csv", self._temp_csv())
            if "hr" in self.streams:
                z.writestr("hr.csv", self._hr_csv())
                z.writestr("rr.csv", self._rr_csv())
            z.writestr("meta.json", json.dumps(self._meta(), indent=2))
        return buf.getvalue()
