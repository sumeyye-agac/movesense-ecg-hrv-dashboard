# Recording and data format

A bar across the top of the dashboard holds everything a capture needs —
sampling rates, which streams to include, a free-text label, and the
**Record** button. Nothing is behind a menu; the settings lock while a
recording runs, because changing a rate means resubscribing the stream
and would tear a hole in the data. Stopping hands back a ZIP: one CSV
per stream plus a `meta.json`.

Reloading the page mid-recording is safe. The dashboard asks the backend
what it is doing on load and adopts a running capture — button, elapsed
time, label and all — rather than coming back looking idle while the
recording it can no longer stop carries on.

Sampling rates deserve a warning. GSP has no GET verb, so there is no way
to ask the sensor which rates it supports, and subscribing at an
unsupported one is accepted and then silently delivers nothing. Starting
a recording therefore resubscribes, waits for packets to actually
arrive, and refuses to start if none do — restoring the previous working
rate rather than leaving the dashboard dead. Ten minutes of silently
empty recording is a worse outcome than being told up front.

Recordings are buffered in memory and capped at two million samples per
stream, about four hours of 125 Hz ECG or an hour at 500 Hz. On reaching
the ceiling a stream stops early and says so in the warnings; running out
of memory instead would take the whole capture with it.

Saving uses the browser's native save dialog
(`window.showSaveFilePicker`) so you choose the folder and name. That API
is Chromium-only and wants a secure context, so **serve the frontend over
HTTP rather than opening it as a `file://` URL** if you want the dialog:

```bash
python -m http.server 8080 --directory frontend
```

Elsewhere — Safari, Firefox — it falls back to an ordinary download and
the dialog text says so.

### CSV format

Every CSV starts with the same four time columns, so one loader reads
them all. `t_device_ms` is empty where the stream has no device clock.

| file | rows | columns after the time block |
|---|---|---|
| `ecg.csv` | one per sample | `ecg_mv` |
| `imu.csv` | one per sample | `acc_x…z`, `gyro_x…z`, `magn_x…z` |
| `temp.csv` | one per reading | `temp_c` |
| `hr.csv` | one per notification | `bpm`, `sdnn_ms` |
| `rr.csv` | one per beat | `rr_ms`, `index_in_packet` |

Alongside them the ZIP carries a `meta.json` and a `README.md`, both written
per recording. They hold the same facts for different readers: `meta.json`
is what a loader parses — the measured rates, the clock anchor and drift,
sample counts — while the README is what a person opening the archive
months later reads, with this recording's own numbers and warnings in it.

### The timestamp model

Two things about the timestamps are worth understanding before using
this data, because both are easy to get wrong silently:

**Samples are spread within their packet.** BLE delivers ECG and IMU9 in
batches — one device timestamp and roughly sixteen samples per packet.
Stamping every sample in a packet with the packet's own time produces a
staircase, which corrupts inter-beat intervals and anything else derived
from sample spacing. Sample *i* is placed at `packet_timestamp + i/rate`
instead.

**There are two clocks, and both are kept.** The sensor's own clock has
accurate spacing but is relative to its boot; the host's wall clock is
absolute but carries BLE batching jitter. Rather than pick one:

- `t_s` — seconds since the recording started. Convenience axis. The very
  first row can be a few milliseconds negative: the streams share one
  device clock, whichever packet lands first anchors it, and another
  stream's packet may carry an earlier timestamp because it was sampled
  just before you pressed Record and only delivered just after.
- `t_device_ms` — the sensor's own monotonic clock, uint32 wraps undone.
- `t_unix` — absolute time derived from the device clock, anchored to the
  host clock once at the first packet. **Use this one for analysis.**
- `t_recv_unix` — when the packet reached the host. Samples from one
  packet share it, so the gap against `t_unix` is BLE latency.

`meta.json` records the anchor, the clock drift measured at stop, and a
count of packets that didn't follow on from their predecessor — a
non-zero count means packets were dropped and the timeline has holes.

If the sensor loses power mid-recording its clock restarts near zero.
That is a small backward jump, not the ~2^32 one a uint32 wrap produces,
and the two are handled differently: a wrap is added back, a restart
re-anchors the timeline and is counted in `clock_restarts`. Treating a
restart as a wrap would date every later sample an hour before the
recording began, with nothing to show it had happened.

Heart rate is the exception the columns admit to: the standard Bluetooth
Heart Rate Service carries no device timestamp at all, so `t_device_ms`
is empty there and `t_unix` can only be arrival time. Its raw
RR-intervals get their own file, since they, not the derived SDNN, are
what HRV work actually wants. Absolute beat times are deliberately *not*
reconstructed from them — a single dropped notification would make a
cumulative sum silently wrong, so that judgement is left to you.

```python
import pandas as pd
ecg = pd.read_csv("ecg.csv")
ecg["t_unix"].diff().describe()   # should sit at 1/rate
```
