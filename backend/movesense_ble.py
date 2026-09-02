"""
BLE client for a Movesense sensor.

Two data paths, handled independently:

- Heart rate over the standard Bluetooth Heart Rate Service (0x180D /
  0x2A37). Arrives pre-decoded, no vendor protocol involved.
- ECG, IMU9 (accelerometer + gyroscope + magnetometer) and temperature
  over Movesense's own GSP protocol (spec:
  movesense.com/docs/esw/gatt_sensordata_protocol). All three ride the
  same GSP notify characteristic, distinguished by reference code.

Decoding: GSP hands back each measurement as a small binary payload.
Movesense doesn't publish its exact byte layout as a written spec, but
for these two paths it's been reverse-engineered here and verified
against physics, not just "it parses without crashing":
  - IMU9 accelerometer samples decode to ~9.8 m/s^2 magnitude at rest
    (Earth's gravity) - see _decode_imu9. The magnetometer block is
    structurally sound (stable, correctly bounded values) but its
    absolute scale is NOT verified: an uncalibrated magnetometer's
    hard-iron offset means a stationary reading cannot be checked
    against Earth's field. Treat those columns as raw output.
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
TEMP_SUBSCRIBE_REF = 44
DEFAULT_IMU_SAMPLE_RATE_HZ = 52
DEFAULT_ECG_SAMPLE_RATE_HZ = 125  # confirmed valid for this sensor via /Meas/ECG/Info's AvailableSampleRates

# Rates Movesense documents for these resources. GSP has no GET verb -
# only HELLO/SUBSCRIBE/UNSUBSCRIBE - so there is no way to ask the sensor
# what it actually supports; these lists come from the API reference, not
# from the device. Subscribing at an unsupported rate fails *silently*:
# the subscribe simply never produces data. Callers must therefore verify
# that packets actually arrive rather than trusting the subscribe.
VALID_ECG_RATES_HZ = (125, 128, 200, 250, 256, 500)
VALID_IMU_RATES_HZ = (13, 26, 52, 104, 208, 416)

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


def _decode_temp(payload: bytes):
    """[float32 Kelvin][uint32 timestamp_ms] -> (timestamp_ms, celsius).

    Wire order is the *reverse* of the Whiteboard schema's field listing
    order (Timestamp, Measurement), and also reversed from ECG/IMU's
    timestamp-first layout. Confirmed on real hardware: this ordering
    gives ~33.8 C for a chest-worn sensor (plausible skin-adjacent temp);
    the timestamp-first reading gives 0 K (absolute zero, impossible),
    which is how the swap was caught.
    """
    kelvin = struct.unpack_from("<f", payload, 0)[0]
    timestamp = struct.unpack_from("<I", payload, 4)[0]
    return timestamp, kelvin - 273.15


class MovesenseBLE:
    def __init__(
        self,
        address: str,
        on_hr: Optional[Callable[[int, list], None]] = None,
        on_ecg: Optional[Callable[[int, list], None]] = None,
        on_imu: Optional[Callable[[int, list, list, list], None]] = None,
        on_temp: Optional[Callable[[int, float], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        ecg_hz: int = DEFAULT_ECG_SAMPLE_RATE_HZ,
        imu_hz: int = DEFAULT_IMU_SAMPLE_RATE_HZ,
    ):
        self.address = address
        self.client: Optional[BleakClient] = None
        self.ecg_hz = ecg_hz
        self.imu_hz = imu_hz
        self.on_hr = on_hr
        self.on_ecg = on_ecg
        self.on_imu = on_imu
        self.on_temp = on_temp
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
            # Subscribe the light, low-rate paths first, before ECG/IMU's
            # continuous high-rate streams start competing for the
            # notification channel - if sent last, their acks sometimes
            # never arrive at all (observed this with ref=44 previously).
            await self._gsp_subscribe("/Meas/Temp", ref=TEMP_SUBSCRIBE_REF)
            await asyncio.sleep(0.5)
            await self._gsp_subscribe(f"/Meas/ECG/{self.ecg_hz}/mV", ref=ECG_SUBSCRIBE_REF)
            await asyncio.sleep(0.5)
            await self._gsp_subscribe(f"/Meas/IMU9/{self.imu_hz}", ref=IMU_SUBSCRIBE_REF)
            self.gsp_available = True
        except Exception as e:
            print(f"GSP not available on this sensor (likely firmware < 2.3.0): {e}")

    async def set_rates(self, ecg_hz: int, imu_hz: int):
        """Re-subscribe ECG and IMU9 at new sample rates, live.

        GSP has no "change the rate" command, so the only way is to drop
        both subscriptions and take them out again at the new paths. The
        pacing here mirrors connect(): unsubscribing and resubscribing
        back-to-back on a busy notification channel is exactly the
        situation where acks were seen to go missing.

        The subscribe itself proves nothing - an unsupported rate is
        accepted and then simply never delivers. The caller is
        responsible for confirming that packets actually arrive.
        """
        if ecg_hz not in VALID_ECG_RATES_HZ:
            raise ValueError(f"ECG rate {ecg_hz} Hz not in {VALID_ECG_RATES_HZ}")
        if imu_hz not in VALID_IMU_RATES_HZ:
            raise ValueError(f"IMU9 rate {imu_hz} Hz not in {VALID_IMU_RATES_HZ}")
        if not self.gsp_available:
            raise RuntimeError("GSP not available on this sensor - cannot change rates")

        if (ecg_hz, imu_hz) == (self.ecg_hz, self.imu_hz):
            return

        for ref in (ECG_SUBSCRIBE_REF, IMU_SUBSCRIBE_REF):
            await self._gsp_send(CMD_UNSUBSCRIBE, ref=ref, data=b"")
            await asyncio.sleep(0.3)

        await self._gsp_subscribe(f"/Meas/ECG/{ecg_hz}/mV", ref=ECG_SUBSCRIBE_REF)
        await asyncio.sleep(0.5)
        await self._gsp_subscribe(f"/Meas/IMU9/{imu_hz}", ref=IMU_SUBSCRIBE_REF)

        self.ecg_hz = ecg_hz
        self.imu_hz = imu_hz

    def _handle_unexpected_disconnect(self, _client):
        print("Sensor connection dropped unexpectedly")
        if self.on_disconnect:
            self.on_disconnect()

    async def disconnect(self):
        if self.client and self.client.is_connected:
            if self.gsp_available:
                for ref in (ECG_SUBSCRIBE_REF, IMU_SUBSCRIBE_REF, TEMP_SUBSCRIBE_REF):
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
                elif ref == TEMP_SUBSCRIBE_REF:
                    timestamp, celsius = _decode_temp(payload)
                    if self.on_temp:
                        self.on_temp(timestamp, celsius)
            except (struct.error, IndexError) as e:
                print(f"[GSP] failed to decode payload for ref={ref}: {e}")

