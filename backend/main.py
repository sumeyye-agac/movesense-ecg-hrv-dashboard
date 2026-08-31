"""FastAPI server bridging a Movesense sensor (BLE) to the dashboard (WebSocket).

Setup instructions are in the project README. Without MOVESENSE_ADDRESS set,
the server still starts but stays disconnected - useful for frontend work.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
from collections import deque
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from movesense_ble import MovesenseBLE

load_dotenv()
MOVESENSE_ADDRESS = os.environ.get("MOVESENSE_ADDRESS")

connected_clients: set[WebSocket] = set()
ble_client: MovesenseBLE | None = None
main_loop: asyncio.AbstractEventLoop | None = None

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


def on_hr(bpm: int, rr_intervals_ms: list[float]):
    rr_window.extend(rr_intervals_ms)
    sdnn = round(statistics.stdev(rr_window), 1) if len(rr_window) >= SDNN_MIN_SAMPLES else None

    if main_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "hr", "bpm": bpm, "sdnn_ms": sdnn}),
            main_loop,
        )


def on_ecg_raw(ref: int, payload: bytes):
    if main_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "ecg_raw", "ref": ref, "num_bytes": len(payload)}),
            main_loop,
        )


def on_imu_raw(ref: int, payload: bytes):
    if main_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "imu_raw", "ref": ref, "num_bytes": len(payload)}),
            main_loop,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ble_client, main_loop
    main_loop = asyncio.get_running_loop()

    if MOVESENSE_ADDRESS:
        ble_client = MovesenseBLE(
            MOVESENSE_ADDRESS, on_hr=on_hr, on_ecg_raw=on_ecg_raw, on_imu_raw=on_imu_raw
        )
        try:
            await ble_client.connect()
            print(f"Connected to Movesense sensor at {MOVESENSE_ADDRESS}")
            print(f"GSP (ECG) available: {ble_client.gsp_available}")
        except Exception as e:
            print(f"Could not connect to Movesense sensor: {e}")
            ble_client = None
    else:
        print("MOVESENSE_ADDRESS not set - run `python scan.py` to find your sensor,")
        print("then put it in a .env file. Server will run without a live sensor.")

    yield

    if ble_client:
        await ble_client.disconnect()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
