# How the sensor is read

## Architecture


```
Movesense MD sensor  --BLE-->  Python backend (bleak)  --WebSocket-->  Browser dashboard
                                     |
                                     +-- FastAPI serves /ws and /status
```

Two data paths run side by side:

- **Heart rate + HRV** — via the standard Bluetooth Heart Rate Service
  (BLE SIG spec, service 0x180D / characteristic 0x2A37). Bpm arrives
  pre-decoded; when the sensor also includes RR-intervals (flags bit 4),
  the backend keeps a rolling window of the last 60 and reports a live
  SDNN (standard deviation of RR intervals) as a rough HRV indicator.
  This is a simple, unfiltered SDNN — no ectopic-beat correction — so
  treat it as a live signal, not a clinical HRV number. Fully working.
- **ECG, IMU9, and temperature** — via Movesense's own GSP ("GATT
  SensorData Protocol"), built for exactly this case: a laptop or Pi
  that can't run their mobile SDK. All three ride the same GSP notify
  characteristic, distinguished by an 8-bit reference code chosen at
  subscribe time. ECG and IMU9 packets are `[uint32 LE timestamp_ms][payload]`
  (ECG: raw int16 LSBs scaled to mV; IMU9: three back-to-back blocks of
  N×{x,y,z} float32 samples for accelerometer/gyroscope/magnetometer, N
  derived from payload size). Temperature is the odd one out —
  `[float32 Kelvin][uint32 timestamp]`, timestamp *last*, unlike the
  other two. All of it is decoded and live now — the dashboard plots the
  ECG waveform plus all three IMU9 sensors (accelerometer, gyroscope,
  magnetometer) as separate three-axis traces. See the verification
  method below. The light paths (temperature) are subscribed before the heavy,
  continuous ones (ECG, IMU9) at connect time — subscribing them after
  the high-rate streams were already running sometimes got no
  acknowledgment at all, presumably the notification channel being busy.

## How the byte format was verified


Movesense's GSP spec documents the command/response envelope (HELLO,
SUBSCRIBE, response codes) but not the byte layout of the measurement
payload itself — that's proprietary ("SBEM"). Guessing at a binary
format for a biosignal and trusting it blind is worse than not decoding
it at all, so instead of reverse-engineering from vague hints, this
project:

1. Logged real raw payloads from the sensor for each measurement path.
2. Formed a hypothesis for the layout from the official Whiteboard API
   schemas (`movesense-api.md`, see References) — e.g. that a
   `Timestamp: uint32` field is part of the payload, matching what the
   ECG, IMU9, and Temperature schemas all document.
3. Decoded against that hypothesis and checked the result against
   something independently known to be true:
   - Accelerometer: `sqrt(x²+y²+z²)` should read ~9.8 m/s² when the
     sensor is roughly stationary (Earth's gravity). It did, across
     multiple samples.
   - Magnetometer: **not verified.** The obvious check — magnitude
     should sit in Earth's field range, ~25–65 µT — does not work on a
     stationary sensor. An uncalibrated MEMS magnetometer carries a
     hard-iron offset, so one orientation's magnitude measures that
     offset as much as the field; establishing the true field strength
     means rotating through many orientations and fitting a sphere,
     whose radius is the answer. Measured over a real recording the
     magnitude sits around 11 µT, tightly clustered (σ ≈ 1.2) — stable
     and structured rather than garbage, so the block boundaries are
     almost certainly right, but well under Earth's field and not
     something a single resting posture can confirm either way. Treat
     the magnetometer columns as raw uncalibrated output.
   - ECG: raw int16 samples × Movesense's published conversion factor
     (1 LSB = 0.000381469726563 mV) produced a smooth, continuous
     signal — not noise, not NaN, not saturated.
   - Temperature: the schema lists `Timestamp` before `Measurement`,
     so timestamp-first was the natural first guess — but that gave
     0.00 Kelvin (absolute zero, physically impossible for a device
     sitting against skin). Swapping the field order gave ~33.8 °C, a
     plausible skin-adjacent reading, confirming the wire order is the
     *reverse* of the schema's listing order. This is exactly the kind
     of error a plausibility check catches and a "did it crash?" check
     wouldn't — both readings parse cleanly as valid floats.
4. Only after those checks passed did each decoder go into
   `backend/movesense_ble.py` (`_decode_ecg`, `_decode_imu9`,
   `_decode_temp`) as the default path, along with unit tests that
   assert the same physics bounds (e.g. accelerometer magnitude within
   8.5–11.0 m/s²).

This is the difference between "it parses without crashing" and "it's
verified" — worth being explicit about for a biosignal, and a
reasonable template for decoding any undocumented binary sensor format.

## References

- Movesense MD Developer Kit spec — https://www.movesense.com/product/movesense-md-developer-kit-mdr/
- GATT SensorData Protocol (GSP) spec — https://www.movesense.com/docs/esw/gatt_sensordata_protocol/
- Movesense sample apps incl. `gatt_sensordata_app` + its Python client (parses ECG/IMU9 from SBEM) — https://www.movesense.com/docs/esw/sample_applications/
- Movesense system/mobile overview (Whiteboard vs GSP) — https://www.movesense.com/docs/system/system_overview/ , https://www.movesense.com/docs/mobile/mobile_sw_overview/
- Firmware 2.3.0 / 2.3.1 release notes (GSP becomes default firmware) — https://www.movesense.com/news/2024/12/movesense-firmware-update-2-3-0-a-perfect-holiday-gift-for-developers-and-researchers/ , https://www.movesense.com/news/2025/06/movesense-sensor-firmware-2-3-1-released-a-major-upgrade-for-medical-use/
- Movesense's official Python Datalogger Tool (also decodes SBEM, for flash recordings) — https://bitbucket.org/movesense/python-datalogger-tool
- sensein/movesense-py, a fork/extension of the above, including `docs/movesense-api.md` (Whiteboard schemas + the ECG LSB→mV conversion factor used here) — https://github.com/sensein/movesense-py
- Movesense news post cataloguing these official tools — https://www.movesense.com/news/2025/11/recording-data-with-movesense-sensors-five-free-tools-to-get-you-started/
- bleak (BLE library) docs — https://bleak.readthedocs.io/ , https://github.com/hbldh/bleak
- Bluetooth Heart Rate Service measurement format, corroborating source — https://blefyi.com/guide/python-bleak/
- Bluetooth SIG Heart Rate Service spec (RR-Interval field, used for the HRV/SDNN feature) — https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html
- FastAPI lifespan events (pattern used in `main.py`) — https://fastapi.tiangolo.com/advanced/events/
