"""
BLE client for a Movesense sensor.

Two data paths, handled independently:

- Heart rate over the standard Bluetooth Heart Rate Service (0x180D /
  0x2A37). Arrives pre-decoded, no vendor protocol involved.
- ECG and IMU9 (accelerometer + gyroscope + magnetometer) over
  Movesense's own GSP protocol (spec:
  movesense.com/docs/esw/gatt_sensordata_protocol). Both ride the same
  GSP notify characteristic, distinguished by reference code.

Decoding: GSP hands back each measurement as a small binary payload.
Movesense doesn't publish its exact byte layout as a written spec, but
for these two paths it's been reverse-engineered here and verified
against physics, not just "it parses without crashing":
  - IMU9 accelerometer samples decode to ~9.8 m/s^2 magnitude at rest
    (Earth's gravity) and magnetometer samples land in the ~25-65 uT
    range (Earth's field) - see _decode_imu9.
  - ECG samples (raw int16 LSBs) scaled by Movesense's own published
    conversion factor (1 LSB = 0.000381469726563 mV, from their API
    reference) produce a smooth, physiologically plausible signal -
    see _decode_ecg.
Both are payload structures of [uint32 LE timestamp][data], which
matches the Timestamp field Movesense's own Whiteboard API schemas
document for these resources.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner

# --- Standard BLE Heart Rate Service (Bluetooth SIG standard) ---
HRS_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# --- Movesense GATT SensorData Protocol (GSP) ---
GSP_WRITE_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"   # client -> sensor
GSP_NOTIFY_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"  # sensor -> client

CMD_HELLO = 0
CMD_SUBSCRIBE = 1
CMD_UNSUBSCRIBE = 2

# Arbitrary 8-bit "reference code" per subscription, must stay unique
# until unsubscribed - this is how responses on the shared GSP notify
# characteristic get routed back to the right callback.
ECG_SUBSCRIBE_REF = 42
IMU_SUBSCRIBE_REF = 43
IMU_SAMPLE_RATE_HZ = 52   # one of Movesense's fixed rates: 13/26/52/104/208/416/833/1666
ECG_SAMPLE_RATE_HZ = 125  # confirmed valid for this sensor via /Meas/ECG/Info's AvailableSampleRates

# Raw ECG integer -> millivolts, from Movesense's own API reference docs.
ECG_LSB_TO_MV = 0.000381469726563


def _decode_ecg(payload: bytes):
    """[uint32 LE timestamp_ms][int16 LE samples...] -> (timestamp_ms, [mV, ...])."""
    timestamp = struct.unpack_from("<I", payload, 0)[0]
    sample_count = (len(payload) - 4) // 2
    raw = struct.unpack_from(f"<{sample_count}h", payload, 4)
    return timestamp, [v * ECG_LSB_TO_MV for v in raw]


def _decode_imu9(payload: bytes):
    """[uint32 LE timestamp_ms][N accel xyz floats][N gyro xyz floats][N magn xyz floats]
    -> (timestamp_ms, acc[(x,y,z),...], gyro[(x,y,z),...], magn[(x,y,z),...]).
    N is derived from payload length, not hardcoded - it depends on sample rate/BLE MTU.
    """
    timestamp = struct.unpack_from("<I", payload, 0)[0]
    body = payload[4:]
    total_floats = len(body) // 4
    n = total_floats // 9  # 3 sensors x 3 axes per sample instant
    floats = struct.unpack_from(f"<{n * 9}f", body, 0)

    def take(start_vec: int):
        return [tuple(floats[(start_vec + i) * 3:(start_vec + i) * 3 + 3]) for i in range(n)]

    acc = take(0)
    gyro = take(n)
    magn = take(2 * n)
    return timestamp, acc, gyro, magn


class MovesenseBLE:
    def __init__(
        self,
        address: str,
        on_hr: Optional[Callable[[int, list], None]] = None,
        on_ecg: Optional[Callable[[int, list], None]] = None,
        on_imu: Optional[Callable[[int, list, list, list], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        self.address = address
        self.client: Optional[BleakClient] = None
        self.on_hr = on_hr
        self.on_ecg = on_ecg
        self.on_imu = on_imu
        self.on_disconnect = on_disconnect
        self.gsp_available = False

    @staticmethod
    async def discover(name_hint: str = "Movesense", timeout: float = 8.0):
        """Scan for nearby BLE devices whose advertised name contains name_hint."""
        devices = await BleakScanner.discover(timeout=timeout)
        return [d for d in devices if d.name and name_hint.lower() in d.name.lower()]

    async def connect(self):
        # disconnected_callback fires on an *unexpected* drop (out of range,
        # radio interference, sensor powering off) - not on our own
        # disconnect() calls below.
        self.client = BleakClient(self.address, disconnected_callback=self._handle_unexpected_disconnect)
        await self.client.connect()

        # 1) Standard Heart Rate Service - works immediately, no handshake needed
        await self.client.start_notify(HRS_MEASUREMENT_UUID, self._handle_hr)

        # 2) Movesense GSP - optional: older firmware (<2.3.0) doesn't expose
        # this characteristic at all, so a missing GSP shouldn't take HR down
        # with it.
        try:
            await self.client.start_notify(GSP_NOTIFY_UUID, self._handle_gsp)
            await self._gsp_send(CMD_HELLO, ref=1, data=b"")
            await asyncio.sleep(0.3)
            await self._gsp_subscribe(f"/Meas/ECG/{ECG_SAMPLE_RATE_HZ}/mV", ref=ECG_SUBSCRIBE_REF)
            await asyncio.sleep(0.3)
            await self._gsp_subscribe(f"/Meas/IMU9/{IMU_SAMPLE_RATE_HZ}", ref=IMU_SUBSCRIBE_REF)
            self.gsp_available = True
        except Exception as e:
            print(f"GSP not available on this sensor (likely firmware < 2.3.0): {e}")

    def _handle_unexpected_disconnect(self, _client):
        print("Sensor connection dropped unexpectedly")
        if self.on_disconnect:
            self.on_disconnect()

    async def disconnect(self):
        if self.client and self.client.is_connected:
            if self.gsp_available:
                for ref in (ECG_SUBSCRIBE_REF, IMU_SUBSCRIBE_REF):
                    try:
                        await self._gsp_send(CMD_UNSUBSCRIBE, ref=ref, data=b"")
                    except Exception:
                        pass
            await self.client.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    # --- Heart Rate Service ---
    def _handle_hr(self, _sender, data: bytearray):
        # HRS Measurement format (Bluetooth SIG spec, org.bluetooth.characteristic.heart_rate_measurement):
        # byte 0 = flags
        #   bit0: 0 => HR value is uint8, 1 => uint16 LE
        #   bit3: energy expended field present (uint16, 2 bytes)
        #   bit4: one or more RR-Interval values present, each uint16 LE in 1/1024 s
        flags = data[0]
        offset = 1

        if flags & 0x01:
            bpm = int.from_bytes(data[offset:offset + 2], "little")
            offset += 2
        else:
            bpm = data[offset]
            offset += 1

        if flags & 0x08:
            offset += 2  # energy expended, not used here

        rr_intervals_ms = []
        if flags & 0x10:
            while offset + 1 < len(data):
                raw = int.from_bytes(data[offset:offset + 2], "little")
                rr_intervals_ms.append(raw / 1024 * 1000)
                offset += 2

        if self.on_hr:
            self.on_hr(bpm, rr_intervals_ms)

    # --- GSP (Movesense custom protocol) ---
    async def _gsp_send(self, command_id: int, ref: int, data: bytes):
        packet = bytes([command_id, ref]) + data
        await self.client.write_gatt_char(GSP_WRITE_UUID, packet, response=True)

    async def _gsp_subscribe(self, resource_path: str, ref: int):
        await self._gsp_send(CMD_SUBSCRIBE, ref, resource_path.encode("utf-8"))

    def _handle_gsp(self, _sender, data: bytearray):
        response_code = data[0]
        ref = data[1]

        if response_code == 0x01:
            # Command response: for SUBSCRIBE/UNSUBSCRIBE this is a
            # uint16 status. HELLO's response is a different, richer
            # payload (device info strings), not decoded here.
            if len(data) >= 4:
                status = struct.unpack_from("<H", data, 2)[0]
                print(f"[GSP] command response ref={ref} status={status}")
            return

        if response_code in (0x02, 0x03):
            payload = bytes(data[2:])
            try:
                if ref == ECG_SUBSCRIBE_REF:
                    timestamp, mv_samples = _decode_ecg(payload)
                    if self.on_ecg:
                        self.on_ecg(timestamp, mv_samples)
                elif ref == IMU_SUBSCRIBE_REF:
                    timestamp, acc, gyro, magn = _decode_imu9(payload)
                    if self.on_imu:
                        self.on_imu(timestamp, acc, gyro, magn)
            except struct.error as e:
                print(f"[GSP] failed to decode payload for ref={ref}: {e}")

