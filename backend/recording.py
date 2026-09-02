"""Buffers live sensor data during a recording and exports it as a ZIP of CSVs.

The output is meant to be fed to machine learning, which drives two
decisions that the live dashboard never had to care about:

1) **Per-sample timestamps, not per-packet ones.** BLE delivers ECG and
   IMU9 in batches - one packet carries a single device timestamp and
   ~16 samples. Stamping every sample in a packet with the packet's
   arrival time produces a staircase, which corrupts inter-beat
   intervals and anything else derived from sample spacing. Sample i in a
   packet is therefore placed at `packet_timestamp + i / rate`.

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

        # Raw packets: expanded into rows at export time.
        self._ecg: list[tuple] = []   # (device_ms, recv_unix, [mv, ...])
        self._imu: list[tuple] = []   # (device_ms, recv_unix, acc, gyro, magn)
        self._temp: list[tuple] = []  # (device_ms, recv_unix, celsius)
        self._hr: list[tuple] = []    # (recv_unix, bpm, sdnn_ms, [rr_ms, ...])

        self._gaps: list[dict] = []
        self._anomalies = {"ecg": 0, "imu": 0}
        self._last_packet = {}  # stream -> (device_ms, sample_count)
        self._last_device_row = None  # (t_unix, recv_unix) for drift at stop

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
        if self._active:
            self._gaps.append({"unix": time.time(), "reason": reason})

    def get_finished(self, recording_id: str):
        return self._finished.get(recording_id)

    # --- clock ---------------------------------------------------------

    def _unwrap(self, raw_device_ms: int) -> int:
        if self._last_raw_device_ms is not None:
            if raw_device_ms < self._last_raw_device_ms - _WRAP_THRESHOLD:
                self._wrap_offset += _UINT32
        self._last_raw_device_ms = raw_device_ms
        return raw_device_ms + self._wrap_offset

    def _anchor(self, device_ms: int, recv_unix: float):
        if self._anchor_unix is None:
            self._anchor_unix = recv_unix
            self._anchor_device_ms = device_ms

    def _to_unix(self, device_ms: float) -> float:
        return self._anchor_unix + (device_ms - self._anchor_device_ms) / 1000.0

    def _check_contiguous(self, stream: str, device_ms: int, count: int, rate_hz: int):
        """Flag packets that don't butt up against the previous one.

        This doubles as a check on the assumption that a packet's
        timestamp belongs to its *first* sample: if it does, and no
        packets were dropped, consecutive timestamps differ by exactly
        count / rate.
        """
        prev = self._last_packet.get(stream)
        if prev is not None and rate_hz:
            prev_ms, prev_count = prev
            expected = prev_ms + prev_count * 1000.0 / rate_hz
            if abs(device_ms - expected) > 500.0 / rate_hz:  # half a sample
                self._anomalies[stream] += 1
        self._last_packet[stream] = (device_ms, count)

    # --- capture -------------------------------------------------------

    def record_ecg(self, packet_ts_ms: int, mv_samples: list, recv_unix: float):
        with self._lock:
            if not self._active or "ecg" not in self.streams:
                return
            device_ms = self._unwrap(packet_ts_ms)
            self._anchor(device_ms, recv_unix)
            self._check_contiguous("ecg", device_ms, len(mv_samples), self.ecg_hz)
            self._ecg.append((device_ms, recv_unix, mv_samples))
            self._last_device_row = (device_ms, recv_unix)

    def record_imu(self, packet_ts_ms: int, acc, gyro, magn, recv_unix: float):
        with self._lock:
            if not self._active or "imu" not in self.streams:
                return
            device_ms = self._unwrap(packet_ts_ms)
            self._anchor(device_ms, recv_unix)
            self._check_contiguous("imu", device_ms, len(acc), self.imu_hz)
            self._imu.append((device_ms, recv_unix, acc, gyro, magn))
            self._last_device_row = (device_ms, recv_unix)

    def record_temp(self, packet_ts_ms: int, celsius: float, recv_unix: float):
        with self._lock:
            if not self._active or "temp" not in self.streams:
                return
            device_ms = self._unwrap(packet_ts_ms)
            self._anchor(device_ms, recv_unix)
            self._temp.append((device_ms, recv_unix, celsius))

    def record_hr(self, bpm: int, sdnn_ms, rr_intervals_ms: list, recv_unix: float):
        with self._lock:
            if not self._active or "hr" not in self.streams:
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
            counts["ecg"] = sum(len(p[2]) for p in self._ecg)
        if "imu" in self.streams:
            counts["imu"] = sum(len(p[2]) for p in self._imu)
        if "temp" in self.streams:
            counts["temp"] = len(self._temp)
        if "hr" in self.streams:
            counts["hr"] = len(self._hr)
            counts["rr"] = sum(len(h[3]) for h in self._hr)
        return counts

    def _drift_ms(self):
        """How far the device clock has slipped from the wall clock.

        Compares the last device-derived timestamp against that same
        packet's actual arrival time. Includes a constant BLE latency
        offset, so read the trend, not the absolute value.
        """
        if self._last_device_row is None or self._anchor_unix is None:
            return None
        device_ms, recv_unix = self._last_device_row
        return round((self._to_unix(device_ms) - recv_unix) * 1000.0, 2)

    def _summary(self):
        duration = self.stopped_unix - self.started_unix
        warnings = []
        if not self._counts():
            warnings.append("No samples were captured at all.")
        for stream in ("ecg", "imu"):
            if stream in self.streams and not self._counts().get(stream):
                warnings.append(
                    f"{stream.upper()} was selected but no packets arrived - "
                    f"the sample rate may not be supported by this sensor."
                )
        if self._gaps:
            warnings.append(f"{len(self._gaps)} connection interruption(s) during recording.")
        for stream, n in self._anomalies.items():
            if n:
                warnings.append(
                    f"{n} {stream.upper()} packet(s) did not follow on from the previous "
                    f"one - likely dropped packets, leaving gaps in the timeline."
                )
        return {
            "recording_id": self.recording_id,
            "label": self.label,
            "duration_s": round(duration, 1),
            "counts": self._counts(),
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

    def _row_times(self, device_ms, recv_unix):
        t_unix = self._to_unix(device_ms)
        return [
            _fmt(t_unix - self.started_unix, 6),
            _fmt(device_ms, 3),
            _fmt(t_unix, 6),
            _fmt(recv_unix, 6),
        ]

    def _ecg_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + ["ecg_mv"])
        step = 1000.0 / self.ecg_hz if self.ecg_hz else 0.0
        for device_ms, recv_unix, samples in self._ecg:
            for i, mv in enumerate(samples):
                w.writerow(self._row_times(device_ms + i * step, recv_unix) + [_fmt(mv, 6)])
        return out.getvalue()

    def _imu_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + [
            "acc_x", "acc_y", "acc_z",
            "gyro_x", "gyro_y", "gyro_z",
            "magn_x", "magn_y", "magn_z",
        ])
        step = 1000.0 / self.imu_hz if self.imu_hz else 0.0
        for device_ms, recv_unix, acc, gyro, magn in self._imu:
            for i in range(len(acc)):
                values = list(acc[i]) + list(gyro[i]) + list(magn[i])
                w.writerow(
                    self._row_times(device_ms + i * step, recv_unix)
                    + [_fmt(v, 6) for v in values]
                )
        return out.getvalue()

    def _temp_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(_TIME_COLUMNS + ["temp_c"])
        for device_ms, recv_unix, celsius in self._temp:
            w.writerow(self._row_times(device_ms, recv_unix) + [_fmt(celsius, 3)])
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
            files["ecg"] = {"file": "ecg.csv", "rate_hz": self.ecg_hz, "samples": counts.get("ecg", 0)}
        if "imu" in self.streams:
            files["imu"] = {"file": "imu.csv", "rate_hz": self.imu_hz, "samples": counts.get("imu", 0)}
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
                "timestamp_gap_anomalies": dict(self._anomalies),
                "interruptions": self._gaps,
            },
            "ecg_lsb_to_mv": 0.000381469726563,
            "column_notes": {
                "t_s": "seconds since the recording started",
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

    def _build_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
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
