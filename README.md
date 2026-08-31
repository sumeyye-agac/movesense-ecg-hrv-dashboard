# Movesense Live Dashboard

Real-time heart-rate, ECG, and IMU streaming from a Movesense MD sensor
to a browser dashboard, over Bluetooth Low Energy — no phone app in the
middle.

## Demo

A pipeline that reads live ECG, IMU9, and heart-rate data off a
Movesense medical sensor over Bluetooth Low Energy, decodes it against
the vendor's own GATT protocol spec, computes a live HRV estimate, and
streams the result to a browser dashboard in real time.

## Requirements

- A Movesense MD sensor. Heart rate and HRV work on any firmware, since
  they come over the standard BLE Heart Rate Service. The ECG and IMU9
  paths need **firmware 2.3.0 or later** (2.3.1 for the MD/medical
  variant), which is where GSP became part of the default firmware — on
  older firmware the backend logs that GSP is unavailable and keeps
  streaming heart rate. Check/update via the Movesense Showcase app.
- Python 3.10+, and a machine with BLE (macOS, Linux, or Windows).

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
- **ECG and IMU9** (accelerometer + gyroscope + magnetometer) — via
  Movesense's own GSP ("GATT SensorData Protocol"), built
  for exactly this case: a laptop or Pi that can't run their mobile SDK.
  The handshake and subscription here are implemented against Movesense's
  published spec. The payload itself comes back as **SBEM**, Suunto's
  binary measurement format. SBEM isn't described in Movesense's written
  docs, but it isn't undocumented either — Movesense ships an official
  Python client that decodes it (see References). Decoding isn't wired
  up in this repo yet; the dashboard currently just shows that packets
  are arriving on each path (count, byte rate).

## Setup

```bash
cd backend
pip install -r requirements.txt

python scan.py                                    # find your sensor's BLE address
cp .env.example .env                              # then edit .env with your address
uvicorn main:app --reload
```

Then open `frontend/index.html` in a browser (or serve it with any static
file server). It connects to `ws://localhost:8000/ws`. Chart.js is vendored
at `frontend/vendor/chart.umd.js`, so the dashboard needs no network access
of its own.

## Configuration

The only per-machine value is `MOVESENSE_ADDRESS`, which lives in
`backend/.env` and is gitignored as local config. `backend/.env.example`
is the checked-in template. There are no API keys or cloud credentials
here — the sensor connection is local BLE and FastAPI runs without auth,
so don't expose the backend beyond localhost as-is.

## Adding ECG decoding

1. Log a batch of raw payloads (already flowing into `on_ecg_raw` in
   `backend/movesense_ble.py`) to a file.
2. Movesense publishes an official Python client specifically for GSP
   live streaming, alongside the firmware sample it talks to
   (`gatt_sensordata_app`) — it already parses IMU9 and ECG out of SBEM
   into CSV. Read how it does that and adapt the relevant part instead
   of guessing at the byte layout, citing it rather than copying it
   wholesale. (Their separate Python Datalogger Tool decodes SBEM too,
   but that one targets flash-stored recordings rather than live GSP
   streaming — the sample above is the closer match to what this repo
   does.)
3. Check the decoded output against something you can independently
   verify, e.g. rough heartbeat timing against the HRS bpm reading at
   the same moment.
4. From there: a real scrolling ECG trace, R-peak detection, and an
   ECG-derived (rather than RR-derived) HRV metric like RMSSD all build
   on top of the decoded waveform.

## References

- Movesense MD Developer Kit spec — https://www.movesense.com/product/movesense-md-developer-kit-mdr/
- GATT SensorData Protocol (GSP) spec — https://www.movesense.com/docs/esw/gatt_sensordata_protocol/
- Movesense sample apps incl. `gatt_sensordata_app` + its Python client (parses ECG/IMU9 from SBEM) — https://www.movesense.com/docs/esw/sample_applications/
- Movesense system/mobile overview (Whiteboard vs GSP) — https://www.movesense.com/docs/system/system_overview/ , https://www.movesense.com/docs/mobile/mobile_sw_overview/
- Firmware 2.3.0 / 2.3.1 release notes (GSP becomes default firmware) — https://www.movesense.com/news/2024/12/movesense-firmware-update-2-3-0-a-perfect-holiday-gift-for-developers-and-researchers/ , https://www.movesense.com/news/2025/06/movesense-sensor-firmware-2-3-1-released-a-major-upgrade-for-medical-use/
- Movesense's official Python Datalogger Tool (also decodes SBEM, for flash recordings) — https://bitbucket.org/movesense/python-datalogger-tool
- sensein/movesense-py, a fork/extension of the above — https://github.com/sensein/movesense-py
- Movesense news post cataloguing these official tools — https://www.movesense.com/news/2025/11/recording-data-with-movesense-sensors-five-free-tools-to-get-you-started/
- bleak (BLE library) docs — https://bleak.readthedocs.io/ , https://github.com/hbldh/bleak
- Bluetooth Heart Rate Service measurement format, corroborating source — https://blefyi.com/guide/python-bleak/
- Bluetooth SIG Heart Rate Service spec (RR-Interval field, used for the HRV/SDNN feature) — https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html
- FastAPI lifespan events (pattern used in `main.py`) — https://fastapi.tiangolo.com/advanced/events/

## Notes

- `bleak` needs macOS, Linux, or Windows with BLE support. On Linux,
  stale GATT caching can cause `connect()` to hang — see bleak's
  troubleshooting docs.
- **macOS (Apple Silicon included, e.g. M1–M3):** fully supported, no
  Rosetta or extra setup needed — bleak's CoreBluetooth backend is
  native arm64. Two Mac-specific things to know:
  - The first time you run `scan.py` or `main.py`, macOS will prompt
    for Bluetooth access for whichever app is running the process
    (Terminal, iTerm, your editor's integrated terminal, etc). If you
    miss the prompt or deny it, you won't get a clear permission error
    — instead bleak raises something like "Bluetooth device is turned
    off" even though it's on. Fix via System Settings → Privacy &
    Security → Bluetooth, and re-run.
  - CoreBluetooth hides real BLE MAC addresses for privacy, so
    `scan.py` will print a macOS-generated UUID instead of an
    `XX:XX:XX:XX:XX:XX` address. That's expected — use it in
    `MOVESENSE_ADDRESS` exactly the same way; it only resolves back to
    your sensor on the same Mac that scanned it.
  - The backend has to run on the machine that's physically near the
    sensor — BLE needs proximity, so a remote/cloud dev environment
    can't reach it.
