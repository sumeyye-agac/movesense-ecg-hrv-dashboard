"""FastAPI server bridging a Movesense sensor (BLE) to the dashboard (WebSocket).

Setup instructions are in the project README. Without MOVESENSE_ADDRESS set,
the server still starts but stays disconnected - useful for frontend work.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from collections import deque
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from movesense_ble import (
    DEFAULT_ECG_SAMPLE_RATE_HZ,
    DEFAULT_IMU_SAMPLE_RATE_HZ,
    VALID_ECG_RATES_HZ,
    VALID_IMU_RATES_HZ,
    MovesenseBLE,
)
from recording import ALL_STREAMS, Recorder

load_dotenv()
MOVESENSE_ADDRESS = os.environ.get("MOVESENSE_ADDRESS")

connected_clients: set[WebSocket] = set()
ble_client: MovesenseBLE | None = None
main_loop: asyncio.AbstractEventLoop | None = None
reconnect_event: asyncio.Event | None = None

recorder = Recorder()

# Rates survive a reconnect: connection_loop rebuilds the client from
# these rather than dropping back to the defaults mid-session.
current_rates = {"ecg_hz": DEFAULT_ECG_SAMPLE_RATE_HZ, "imu_hz": DEFAULT_IMU_SAMPLE_RATE_HZ}

# Bumped on every packet, only ever read as "has this advanced?". Used to
# confirm a subscription is actually delivering, since GSP accepts an
# unsupported sample rate and then silently sends nothing.
packets_seen = {"ecg": 0, "imu": 0, "temp": 0, "hr": 0}

# Rolling window for a simple SDNN (HRV) estimate. This is standard
# deviation of raw RR intervals with no artifact/ectopic-beat filtering,
# so treat it as a rough live indicator, not a clinical HRV metric.
RR_WINDOW_SIZE = 60
SDNN_MIN_SAMPLES = 10
rr_window: deque[float] = deque(maxlen=RR_WINDOW_SIZE)


async def broadcast(message: dict):
    if not connected_clients:
        return
    data = json.dumps(message)
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_clients.discard(ws)


# Every on_* below runs on bleak's callback thread. Arrival time is taken
# first thing, before any work, so it stays a measure of when the packet
# reached this machine rather than of how long we then took over it.


def on_hr(bpm: int, rr_intervals_ms: list[float]):
    recv_unix = time.time()
    packets_seen["hr"] += 1
    rr_window.extend(rr_intervals_ms)
    sdnn = round(statistics.stdev(rr_window), 1) if len(rr_window) >= SDNN_MIN_SAMPLES else None
    recorder.record_hr(bpm, sdnn, rr_intervals_ms, recv_unix)

    if main_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "hr", "bpm": bpm, "sdnn_ms": sdnn}),
            main_loop,
        )


def on_ecg(timestamp_ms: int, mv_samples: list[float]):
    recv_unix = time.time()
    packets_seen["ecg"] += 1
    recorder.record_ecg(timestamp_ms, mv_samples, recv_unix)

    if main_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "ecg", "t": timestamp_ms, "mv": mv_samples}),
            main_loop,
        )


def on_imu(timestamp_ms: int, acc: list[tuple], gyro: list[tuple], magn: list[tuple]):
    recv_unix = time.time()
    packets_seen["imu"] += 1
    recorder.record_imu(timestamp_ms, acc, gyro, magn, recv_unix)

    if main_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({
                "type": "imu",
                "t": timestamp_ms,
                "acc": acc,
                "gyro": gyro,
                "magn": magn,
            }),
            main_loop,
        )


def on_temp(timestamp_ms: int, celsius: float):
    recv_unix = time.time()
    packets_seen["temp"] += 1
    recorder.record_temp(timestamp_ms, celsius, recv_unix)

    if main_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "temp", "t": timestamp_ms, "celsius": round(celsius, 1)}),
            main_loop,
        )


def on_sensor_disconnect():
    # Runs on bleak's callback thread/loop - hop back onto the main loop
    # to flag the reconnect loop rather than touching asyncio state directly.
    recorder.note_gap("sensor disconnected")
    if main_loop and reconnect_event:
        main_loop.call_soon_threadsafe(reconnect_event.set)


async def connection_loop():
    """Connects to the sensor and keeps reconnecting after unexpected drops.

    A dropped BLE link (out of range, sensor sleeping, radio interference)
    is normal, not exceptional - this loop just keeps retrying rather than
    leaving the backend silently dead until someone restarts uvicorn.
    """
    global ble_client
    while True:
        reconnect_event.clear()
        connected_ok = False
        try:
            ble_client = MovesenseBLE(
                MOVESENSE_ADDRESS,
                on_hr=on_hr,
                on_ecg=on_ecg,
                on_imu=on_imu,
                on_temp=on_temp,
                on_disconnect=on_sensor_disconnect,
                ecg_hz=current_rates["ecg_hz"],
                imu_hz=current_rates["imu_hz"],
            )
            await ble_client.connect()
            connected_ok = True
            print(f"Connected to Movesense sensor at {MOVESENSE_ADDRESS}")
            print(f"GSP (ECG) available: {ble_client.gsp_available}")
        except Exception as e:
            print(f"Could not connect to Movesense sensor: {e!r}")
            ble_client = None

        if connected_ok:
            await reconnect_event.wait()
            print("Sensor disconnected - reconnecting...")
        else:
            print("Retrying sensor connection in 5s...")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop, reconnect_event
    main_loop = asyncio.get_running_loop()
    reconnect_event = asyncio.Event()

    connection_task = None
    if MOVESENSE_ADDRESS:
        connection_task = asyncio.create_task(connection_loop())
    else:
        print("MOVESENSE_ADDRESS not set - run `python scan.py` to find your sensor,")
        print("then put it in a .env file. Server will run without a live sensor.")

    yield

    if connection_task:
        connection_task.cancel()
    if ble_client:
        await ble_client.disconnect()


app = FastAPI(lifespan=lifespan)

# Any page you have open can reach a server on your own loopback address.
# That was harmless when this only served readings, but the recording
# endpoints start and stop captures and re-subscribe the sensor at a
# different sample rate, so "*" would let an unrelated site you happen to
# be visiting drive the hardware. Restrict it to origins that are the
# dashboard: a local static server, or the page opened straight off disk,
# which sends the literal origin "null".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        while True:
            # We don't expect messages from the browser, this just keeps
            # the connection open and detects disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.discard(websocket)


@app.get("/status")
async def status():
    return {
        "connected": bool(ble_client and ble_client.is_connected),
        "address": MOVESENSE_ADDRESS,
        "gsp_available": bool(ble_client and ble_client.gsp_available),
    }


# --- Recording -------------------------------------------------------------
#
# stop() deliberately returns a summary rather than the ZIP: the browser
# shows the user what was captured, lets them name the file, and only then
# fetches the bytes from the export endpoint.

# How long to wait for a subscription to prove itself before giving up.
# set_rates() spends ~0.8s on its own paced subscribes, so this has to
# leave room beyond that.
ARM_TIMEOUT_S = 5.0


class StartRequest(BaseModel):
    streams: list[str] = Field(default_factory=lambda: list(ALL_STREAMS))
    ecg_hz: int = DEFAULT_ECG_SAMPLE_RATE_HZ
    imu_hz: int = DEFAULT_IMU_SAMPLE_RATE_HZ
    # The label reaches both meta.json and the suggested filename, so it is
    # bounded here rather than left to whatever gets posted.
    label: str = Field(default="", max_length=100)


async def _wait_for_packets(streams: list[str], timeout: float = ARM_TIMEOUT_S) -> list[str]:
    """Wait until the silent-failure-prone streams are actually delivering.

    Only ECG and IMU9 are watched. Their sample rate goes into the
    subscribe path, and GSP accepts an unsupported rate without complaint
    and then sends nothing - so the subscribe succeeding proves nothing.
    Temperature only notifies on change and heart rate is roughly 1 Hz;
    blocking on either would reject perfectly good recordings.

    Returns the streams that never produced a packet.
    """
    watched = [s for s in ("ecg", "imu") if s in streams]
    if not watched:
        return []

    baseline = {s: packets_seen[s] for s in watched}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stalled = [s for s in watched if packets_seen[s] == baseline[s]]
        if not stalled:
            return []
        await asyncio.sleep(0.1)
    return stalled


@app.get("/recording/options")
async def recording_options():
    return {
        "streams": list(ALL_STREAMS),
        "ecg_rates_hz": list(VALID_ECG_RATES_HZ),
        "imu_rates_hz": list(VALID_IMU_RATES_HZ),
        "current": dict(current_rates),
        "defaults": {
            "ecg_hz": DEFAULT_ECG_SAMPLE_RATE_HZ,
            "imu_hz": DEFAULT_IMU_SAMPLE_RATE_HZ,
        },
    }


@app.post("/recording/start")
async def recording_start(req: StartRequest):
    if recorder.is_recording:
        raise HTTPException(409, "A recording is already running.")

    streams = [s for s in req.streams if s in ALL_STREAMS]
    if not streams:
        raise HTTPException(400, "Select at least one stream to record.")
    if req.ecg_hz not in VALID_ECG_RATES_HZ:
        raise HTTPException(400, f"ECG rate must be one of {list(VALID_ECG_RATES_HZ)} Hz.")
    if req.imu_hz not in VALID_IMU_RATES_HZ:
        raise HTTPException(400, f"IMU9 rate must be one of {list(VALID_IMU_RATES_HZ)} Hz.")

    if not (ble_client and ble_client.is_connected):
        raise HTTPException(409, "Sensor is not connected.")
    if any(s in streams for s in ("ecg", "imu", "temp")) and not ble_client.gsp_available:
        raise HTTPException(
            409,
            "GSP is unavailable on this sensor, so ECG, IMU9 and temperature cannot be "
            "recorded. Heart rate still can.",
        )

    if any(s in streams for s in ("ecg", "imu")):
        previous = dict(current_rates)
        try:
            await ble_client.set_rates(req.ecg_hz, req.imu_hz)
        except Exception as e:
            raise HTTPException(502, f"Could not apply the sample rates: {e}")
        current_rates.update(ecg_hz=req.ecg_hz, imu_hz=req.imu_hz)

        stalled = await _wait_for_packets(streams)
        if stalled:
            # Nothing arrived, so the rate is probably unsupported. Put the
            # last known-good rates back rather than leaving the dashboard
            # dead, and refuse to start - a recording that silently captures
            # nothing is worse than no recording.
            try:
                await ble_client.set_rates(previous["ecg_hz"], previous["imu_hz"])
                current_rates.update(previous)
            except Exception:
                pass
            names = " and ".join(s.upper() for s in stalled)
            raise HTTPException(
                504,
                f"No {names} data arrived within {ARM_TIMEOUT_S:.0f}s of subscribing. That "
                f"sample rate is probably not supported by this sensor - the previous rate "
                f"has been restored. Try a different one.",
            )

    recording_id = recorder.start(
        streams=streams,
        label=req.label,
        ecg_hz=req.ecg_hz,
        imu_hz=req.imu_hz,
        device_address=MOVESENSE_ADDRESS,
    )
    return {"recording_id": recording_id, "streams": streams, **dict(current_rates)}


@app.get("/recording/status")
async def recording_status():
    return recorder.status()


@app.post("/recording/stop")
async def recording_stop():
    if not recorder.is_recording:
        raise HTTPException(409, "No recording is running.")
    return recorder.stop()


@app.get("/recording/{recording_id}/export.zip")
async def recording_export(recording_id: str):
    entry = recorder.get_finished(recording_id)
    if entry is None:
        raise HTTPException(404, "Unknown recording, or it has been superseded by newer ones.")
    filename = entry["summary"]["suggested_filename"]
    return Response(
        content=entry["zip"],
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
