"""Unit tests for the GSP payload decoders and the recording exporter.

The decoders were originally verified against physics on live hardware
(see the README). These tests pin that same reasoning down against
synthetic payloads so a future change that quietly breaks the byte layout
fails here instead of in a recording nobody checks until later: a
magnitude that stops reading like gravity, or a temperature that comes
back as absolute zero because the field order got swapped again.

Run with: python -m unittest discover backend
"""

import csv
import io
import json
import math
import struct
import time
import unittest
import unittest.mock
import zipfile

from movesense_ble import (
    ECG_LSB_TO_MV,
    _decode_ecg,
    _decode_imu9,
    _decode_temp,
)
import recording
from recording import Recorder


def build_ecg_payload(timestamp_ms, raw_samples):
    return struct.pack("<I", timestamp_ms) + struct.pack(f"<{len(raw_samples)}h", *raw_samples)


def build_imu9_payload(timestamp_ms, acc, gyro, magn):
    body = b""
    for block in (acc, gyro, magn):
        for x, y, z in block:
            body += struct.pack("<3f", x, y, z)
    return struct.pack("<I", timestamp_ms) + body


def build_temp_payload(kelvin, timestamp_ms):
    """Kelvin first, timestamp second - the reverse of ECG/IMU9."""
    return struct.pack("<f", kelvin) + struct.pack("<I", timestamp_ms)


class DecodeEcgTest(unittest.TestCase):
    def test_timestamp_and_scaling(self):
        raw = [0, 100, -100, 32767, -32768]
        ts, mv = _decode_ecg(build_ecg_payload(412340, raw))
        self.assertEqual(ts, 412340)
        self.assertEqual(len(mv), len(raw))
        for got, expected in zip(mv, raw):
            self.assertAlmostEqual(got, expected * ECG_LSB_TO_MV, places=9)

    def test_full_scale_stays_in_millivolt_range(self):
        # A saturated int16 is ~12.5 mV. If the conversion factor is ever
        # wrong by orders of magnitude this is where it shows.
        _, mv = _decode_ecg(build_ecg_payload(0, [32767]))
        self.assertLess(abs(mv[0]), 100.0)
        self.assertGreater(abs(mv[0]), 1.0)

    def test_sample_count_derived_from_payload_length(self):
        for count in (1, 8, 16, 64):
            _, mv = _decode_ecg(build_ecg_payload(0, [1] * count))
            self.assertEqual(len(mv), count)


class DecodeImu9Test(unittest.TestCase):
    def test_accelerometer_at_rest_reads_as_gravity(self):
        # The physics check the README describes: a stationary sensor must
        # measure ~9.8 m/s^2 total, however that is split across axes.
        acc = [(0.21, -9.78, 0.44), (0.19, -9.81, 0.40)]
        gyro = [(0.0, 0.0, 0.0)] * 2
        magn = [(21.0, -14.0, 41.2)] * 2
        _, got_acc, _, _ = _decode_imu9(build_imu9_payload(0, acc, gyro, magn))

        for x, y, z in got_acc:
            magnitude = math.sqrt(x * x + y * y + z * z)
            self.assertGreater(magnitude, 8.5)
            self.assertLess(magnitude, 11.0)

    def test_magnetometer_block_round_trips(self):
        # Deliberately NOT an Earth's-field magnitude check. Asserting that
        # synthetic values chosen to be in range come back in range proves
        # nothing, and on real hardware the magnitude reads ~11 uT anyway:
        # an uncalibrated magnetometer's hard-iron offset means a single
        # orientation cannot measure the field. What is worth pinning down
        # is that the third block is read at the right offset and scale.
        acc = [(0.0, 0.0, 9.81)] * 3
        gyro = [(0.0, 0.0, 0.0)] * 3
        magn = [(8.52, -4.92, -5.43), (7.59, -6.68, -6.42), (8.87, -0.76, -3.14)]
        _, _, _, got_magn = _decode_imu9(build_imu9_payload(0, acc, gyro, magn))

        self.assertEqual(len(got_magn), 3)
        for got, expected in zip(got_magn, magn):
            for axis_got, axis_expected in zip(got, expected):
                self.assertAlmostEqual(axis_got, axis_expected, places=5)

    def test_blocks_are_not_interleaved(self):
        # The three sensors arrive as three back-to-back blocks, not as
        # interleaved xyz triplets per instant. Distinct values per block
        # catch a decoder that mixes them up.
        acc = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]
        gyro = [(10.0, 20.0, 30.0), (40.0, 50.0, 60.0)]
        magn = [(100.0, 200.0, 300.0), (400.0, 500.0, 600.0)]
        ts, got_acc, got_gyro, got_magn = _decode_imu9(build_imu9_payload(7, acc, gyro, magn))

        self.assertEqual(ts, 7)
        self.assertEqual(got_acc, acc)
        self.assertEqual(got_gyro, gyro)
        self.assertEqual(got_magn, magn)

    def test_sample_count_derived_from_payload_length(self):
        for n in (1, 2, 8):
            acc = [(0.0, 0.0, 9.81)] * n
            payload = build_imu9_payload(0, acc, acc, acc)
            _, got_acc, got_gyro, got_magn = _decode_imu9(payload)
            self.assertEqual(len(got_acc), n)
            self.assertEqual(len(got_gyro), n)
            self.assertEqual(len(got_magn), n)


class DecodeTempTest(unittest.TestCase):
    def test_skin_temperature_is_plausible(self):
        ts, celsius = _decode_temp(build_temp_payload(273.15 + 33.8, 412340))
        self.assertEqual(ts, 412340)
        self.assertAlmostEqual(celsius, 33.8, places=3)
        self.assertGreater(celsius, 20.0)
        self.assertLess(celsius, 45.0)

    def test_field_order_is_kelvin_first(self):
        # Reading this payload timestamp-first is what produced 0 K on real
        # hardware. Decoding the reversed layout must NOT give a plausible
        # temperature, otherwise the test would pass either way.
        reversed_payload = struct.pack("<I", 412340) + struct.pack("<f", 273.15 + 33.8)
        _, celsius = _decode_temp(reversed_payload)
        self.assertLess(celsius, -200.0)


class RecorderTimebaseTest(unittest.TestCase):
    def _record(self, **kwargs):
        rec = Recorder()
        rec.start(streams=["ecg"], label="test", ecg_hz=125, imu_hz=52, **kwargs)
        return rec

    @staticmethod
    def _read_csv(rec, recording_id, name):
        data = rec.get_finished(recording_id)["zip"]
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return list(csv.DictReader(io.StringIO(z.read(name).decode())))

    def test_samples_within_a_packet_are_spread_not_stacked(self):
        rec = Recorder()
        rid = rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
        rec.record_ecg(1000, [0.1, 0.2, 0.3, 0.4], recv_unix=1_700_000_000.0)
        rec.stop()

        rows = self._read_csv(rec, rid, "ecg.csv")
        self.assertEqual(len(rows), 4)

        # 125 Hz means 8 ms between samples. If the decoder ever stamped a
        # whole packet with one time, every diff here would be zero.
        times = [float(r["t_unix"]) for r in rows]
        for earlier, later in zip(times, times[1:]):
            self.assertAlmostEqual(later - earlier, 0.008, places=6)

        # All four came off one BLE packet, so they share an arrival time.
        self.assertEqual(len({r["t_recv_unix"] for r in rows}), 1)

    def test_device_clock_wrap_does_not_jump_backwards(self):
        rec = Recorder()
        rid = rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
        near_wrap = 2 ** 32 - 8
        rec.record_ecg(near_wrap, [0.1], recv_unix=1_700_000_000.0)
        rec.record_ecg(0, [0.2], recv_unix=1_700_000_000.008)  # uint32 rolls over
        rec.stop()

        rows = self._read_csv(rec, rid, "ecg.csv")
        times = [float(r["t_unix"]) for r in rows]
        self.assertAlmostEqual(times[1] - times[0], 0.008, places=6)

    def test_heart_rate_rows_have_no_device_clock(self):
        rec = Recorder()
        rid = rec.start(streams=["hr"], label="", ecg_hz=125, imu_hz=52)
        rec.record_hr(108, 80.8, [560.5, 555.0], recv_unix=1_700_000_000.0)
        rec.stop()

        hr_rows = self._read_csv(rec, rid, "hr.csv")
        self.assertEqual(len(hr_rows), 1)
        # The standard BLE Heart Rate Service carries no device timestamp,
        # so this column is empty rather than guessed at.
        self.assertEqual(hr_rows[0]["t_device_ms"], "")
        self.assertEqual(hr_rows[0]["t_unix"], hr_rows[0]["t_recv_unix"])
        self.assertEqual(hr_rows[0]["bpm"], "108")

        rr_rows = self._read_csv(rec, rid, "rr.csv")
        self.assertEqual([r["rr_ms"] for r in rr_rows], ["560.500", "555.000"])

    def _meta(self, rec, recording_id):
        data = rec.get_finished(recording_id)["zip"]
        return json.loads(zipfile.ZipFile(io.BytesIO(data)).read("meta.json"))

    def test_dropped_packet_is_flagged_as_an_anomaly(self):
        rec = Recorder()
        rid = rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
        # Establish a cadence first - with only two packets the single gap
        # between them *is* the cadence, and nothing can deviate from it.
        for k in range(5):
            rec.record_ecg(1000 + k * 32, [0.1] * 4, recv_unix=1_700_000_000.0 + k * 0.032)
        # Contiguous would be 1160. A jump to 2000 means packets went missing.
        rec.record_ecg(2000, [0.2] * 4, recv_unix=1_700_000_001.0)
        for k in range(5):
            rec.record_ecg(2032 + k * 32, [0.1] * 4, recv_unix=1_700_000_001.1 + k * 0.032)
        summary = rec.stop()

        self.assertEqual(self._meta(rec, rid)["clock"]["timestamp_gap_anomalies"]["ecg"], 1)
        self.assertTrue(any("did not follow on" in w for w in summary["warnings"]))

    def test_steady_stream_reports_no_dropped_packets(self):
        rec = Recorder()
        rid = rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
        for k in range(10):
            rec.record_ecg(1000 + k * 32, [0.1] * 4, recv_unix=1_700_000_000.0 + k * 0.032)
        summary = rec.stop()

        self.assertEqual(self._meta(rec, rid)["clock"]["timestamp_gap_anomalies"]["ecg"], 0)
        self.assertEqual(summary["warnings"], [])

    def test_sensor_reboot_re_anchors_instead_of_dating_samples_in_the_past(self):
        # A restarted sensor's clock goes back to near zero. Read as a
        # uint32 wrap that would be fine; read as nothing at all it dates
        # every later sample before the recording began. Neither is right.
        rec = Recorder()
        rid = rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
        now = time.time()
        rec.record_ecg(2_622_452, [0.1], recv_unix=now)
        rec.record_ecg(12, [0.2], recv_unix=now + 30.0)  # sensor rebooted
        summary = rec.stop()

        rows = self._read_csv(rec, rid, "ecg.csv")
        times = [float(r["t_s"]) for r in rows]
        self.assertTrue(all(t >= 0 for t in times), f"negative t_s after reboot: {times}")
        self.assertLess(times[0], times[1])
        self.assertEqual(self._meta(rec, rid)["clock"]["clock_restarts"], 1)
        self.assertTrue(any("clock restarted" in w for w in summary["warnings"]))

    def test_recording_stops_growing_at_the_sample_ceiling(self):
        # The ceiling behaves the same at any size, and pushing two million
        # rows through the CSV writer turns this into a five-second test.
        with unittest.mock.patch.object(recording, "MAX_SAMPLES_PER_STREAM", 400):
            rec = Recorder()
            rid = rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
            for k in range(20):
                rec.record_ecg(1000 + k * 800, [0.1] * 100, recv_unix=time.time() + k * 0.8)
            summary = rec.stop()

            self.assertLessEqual(summary["counts"]["ecg"], 500)
            self.assertTrue(any("ceiling" in w for w in summary["warnings"]))
            self.assertEqual(self._meta(rec, rid)["truncated_streams"], ["ecg"])

    def test_spacing_follows_the_delivered_rate_not_the_requested_one(self):
        # Subscribing IMU9 at 52 Hz was observed delivering 4 samples every
        # 74 ms - about 54 Hz. Spacing on the nominal 19.231 ms would drift
        # each packet's samples ahead and snap them back at the next packet.
        rec = Recorder()
        rid = rec.start(streams=["imu"], label="", ecg_hz=125, imu_hz=52)
        acc = [(0.0, 0.0, 9.81)] * 4
        for k in range(4):
            rec.record_imu(1000 + k * 74, acc, acc, acc, recv_unix=1_700_000_000.0 + k * 0.074)
        summary = rec.stop()

        rows = self._read_csv(rec, rid, "imu.csv")
        times = [float(r["t_unix"]) for r in rows]
        steps = [round(b - a, 6) for a, b in zip(times, times[1:])]

        # 74 ms / 4 samples = 18.5 ms, evenly, across packet boundaries too.
        self.assertEqual(set(steps), {0.0185})
        self.assertTrue(any("delivered 54.054 Hz" in w for w in summary["warnings"]))

    def test_unselected_streams_are_not_written(self):
        rec = Recorder()
        rid = rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
        rec.record_ecg(1000, [0.1], recv_unix=1_700_000_000.0)
        rec.record_temp(1000, 33.8, recv_unix=1_700_000_000.0)  # not selected
        rec.stop()

        with zipfile.ZipFile(io.BytesIO(rec.get_finished(rid)["zip"])) as z:
            self.assertEqual(sorted(z.namelist()), ["README.md", "ecg.csv", "meta.json"])

    def test_readme_describes_this_particular_recording(self):
        # The point of generating it rather than shipping a static file is
        # that it carries this recording's own numbers and caveats.
        rec = Recorder()
        rid = rec.start(streams=["ecg", "hr"], label="resting", ecg_hz=125, imu_hz=52,
                        device_address="AA:BB:CC")
        for k in range(4):
            rec.record_ecg(1000 + k * 32, [0.1] * 4, recv_unix=time.time() + k * 0.032)
        rec.record_hr(70, 41.5, [857.0], recv_unix=time.time())
        summary = rec.stop()

        with zipfile.ZipFile(io.BytesIO(rec.get_finished(rid)["zip"])) as z:
            # Opening the archive should put this in front of the reader.
            self.assertEqual(z.namelist()[0], "README.md")
            readme = z.read("README.md").decode()

        self.assertIn("resting", readme)
        self.assertIn("AA:BB:CC", readme)
        self.assertIn("`ecg.csv`", readme)
        self.assertIn("`hr.csv`", readme)
        # Streams that were not recorded have no file and no row in the table.
        self.assertNotIn("`imu.csv`", readme)
        self.assertNotIn("`temp.csv`", readme)
        # The caveats a reader needs before trusting the numbers.
        self.assertIn("t_unix", readme)
        self.assertIn("uncalibrated", readme)
        for warning in summary["warnings"]:
            self.assertIn(warning, readme)

    def test_empty_stream_produces_a_warning_not_a_silent_file(self):
        rec = Recorder()
        rec.start(streams=["ecg"], label="", ecg_hz=125, imu_hz=52)
        summary = rec.stop()
        self.assertTrue(any("no packets arrived" in w for w in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
