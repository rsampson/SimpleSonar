# SAS Sonar

An ESP32-based sonar front end and PC-side tooling for capturing and
visualizing ping data over air.

## Layout

- **[firmware/](firmware/)** — PlatformIO project for the ESP32. Drives a
  differential ultrasonic transducer pair, samples the reflected signal, and
  streams framed ping packets (sync bytes + header + `int16` sample payload)
  to the PC over a second UART.
- **[pc/](pc/)** — Python tools that run on the PC:
  - [receive.py](pc/receive.py) connects over serial, parses ping packets,
    and logs them to a CSV file.
  - [plot_sensor_stream.py](pc/plot_sensor_stream.py) loads a recorded CSV
    and plots each ping's raw amplitude trace against one-way range.
  - [plot_matched_filter.py](pc/plot_matched_filter.py) reconstructs the
    transmitted ping burst and cross-correlates ("matched filter"/pulse
    compression) each packet against it, plotting the raw and filtered
    traces stacked for the same packet.
  - [plot_echogram.py](pc/plot_echogram.py) applies the matched filter to
    every packet in a recording and renders the result as a waterfall
    echogram (range vs. time, colored by echo strength).
  - [run_pipeline.py](pc/run_pipeline.py) runs the above end-to-end:
    `receive.py` (stop with `Ctrl+C`), then `plot_echogram.py`, then
    `plot_matched_filter.py`, each starting once the previous one exits.

See [firmware/README](#firmware) and [pc/README](#pc) below for details on
each half.

## Firmware

Build/upload with [PlatformIO](https://platformio.org/):

```bash
cd firmware
pio run --target upload
```

Key configuration lives at the top of
[firmware/src/main.cpp](firmware/src/main.cpp): ping frequency/cycle count,
ADC resolution/attenuation, and the `Serial2` baud rate used to talk to the
PC (must match `BAUD_RATE` in [pc/receive.py](pc/receive.py)).

Each ping packet sent to the PC consists of:

| Field | Type | Description |
|---|---|---|
| `sync` | 2 bytes | `0xAA 0xBB` frame sync |
| `ping_id` | `uint32_t` | Monotonically increasing packet sequence number |
| `t_ping_us` | `int64_t` | ESP32 timestamp at burst start (`esp_timer_get_time()`) |
| `t_sample_us` | `int64_t` | ESP32 timestamp at first sample read |
| `num_samples` | `uint16_t` | Number of samples in this packet |
| `sample_dt_us` | `uint32_t` | Measured per-sample interval, in microseconds |
| payload | `num_samples` × `int16_t` | Raw sample values |

## PC

Set up a virtual environment and install dependencies:

```bash
cd pc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Record a sensor stream

Edit `SERIAL_PORT` and `BAUD_RATE` in [pc/receive.py](pc/receive.py) if
needed (defaults: `/dev/ttyUSB0` at 921600 baud, matching the firmware's
`Serial2` config), then run:

```bash
python receive.py
```

This streams incoming packets into `sensor_stream.csv` (appending if the
file already exists) until stopped with `Ctrl+C`. Pass `--fresh` to discard
any existing `sensor_stream.csv` and start a new file instead of appending.

### 2. Plot a recorded stream

```bash
python plot_sensor_stream.py    # raw per-packet waveform, ←/→ to navigate
python plot_matched_filter.py   # raw + matched-filter view, ←/→ to navigate
python plot_echogram.py         # full-recording waterfall echogram
```

All three read `sensor_stream.csv` from the current directory.

- **plot_sensor_stream.py** displays each packet's raw waveform. The x-axis
  is converted from raw sample index to one-way range in centimeters, using
  the per-packet sample interval (`SampleDt_us`) and the speed of sound in
  air — see `SPEED_OF_SOUND_M_S` in
  [plot_sensor_stream.py](pc/plot_sensor_stream.py) to adjust for different
  conditions. The plot title shows the packet index, timestamp, ping ID,
  inter-ping interval, and sample interval.
- **plot_matched_filter.py** additionally reconstructs the transmitted burst
  (see `make_ping_template`) and cross-correlates it against the raw signal
  to collapse each echo into a sharp peak at its round-trip delay, plotting
  raw and filtered traces stacked for the same packet.
- **plot_echogram.py** runs the matched filter across every packet in the
  recording and displays the amplitude envelope (via a Hilbert transform) as
  a color-mapped range-vs-time waterfall, in dB relative to the strongest
  echo in the recording (`DYNAMIC_RANGE_DB`). The near-range region still
  ringing from the transmit burst is blanked rather than shown as an echo.

### Running the full pipeline

```bash
python run_pipeline.py
```

Runs `receive.py`, then `plot_echogram.py`, then `plot_matched_filter.py` in
sequence — each stage starts only once the previous one has exited (stop
`receive.py` with `Ctrl+C`; close a plot window to move to the next stage).

### Data format

Each row in `sensor_stream.csv` corresponds to one ping packet:

| Column | Description |
|---|---|
| `Timestamp` | Host arrival time (subject to USB/serial latency) |
| `PingID` | Monotonically increasing packet sequence number |
| `T_ping_us` | ESP32 timestamp at burst start (`esp_timer_get_time()`) |
| `T_sample_us` | ESP32 timestamp at first sample read |
| `NumSamples` | Number of samples in this packet |
| `SampleDt_us` | Measured per-sample interval, in microseconds |
| `Int_0` … `Int_{N-1}` | Raw `int16` sample values |

Only this current packet format is supported — older CSVs recorded before
the header carried `PingID`/timing fields are not compatible with
`plot_sensor_stream.py`.

## License

[MIT](LICENSE)
