# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An ESP32-based sonar front end and PC-side tooling for capturing and visualizing ping data over air. Two halves:

- **firmware/** — PlatformIO/Arduino project for the ESP32. Drives a differential ultrasonic transducer pair, samples the reflected signal via the ESP-IDF ADC continuous/DMA driver, and streams framed ping packets over the same UART used for flashing/debug output.
- **pc/** — Python tools that receive, log, and visualize ping data.

## Commands

### Firmware (from `firmware/`)

```bash
pio run --target upload   # build and flash
pio run                   # build only
pio device monitor        # serial monitor (921600 baud)
```

Note: system-wide `pio` (apt package) has been broken on this machine (Click library incompatibility) — use `~/.platformio/penv/bin/pio` if the `pio` on PATH fails.

Upload/monitor port is hardcoded in `firmware/platformio.ini` (`/dev/ttyUSB1`) and may need updating since the ESP32's USB-serial enumeration has been observed to shift between `/dev/ttyUSB0`/`/dev/ttyUSB1` depending on what else is plugged in.

### PC (from `pc/`)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python receive.py              # record a stream to sensor_stream.csv (Ctrl+C to stop; --fresh to discard existing CSV)
python plot_sensor_stream.py   # raw per-packet waveform, ←/→ to navigate packets
python plot_matched_filter.py  # raw + matched-filter view, ←/→ to navigate
python plot_echogram.py        # full-recording waterfall echogram
python run_pipeline.py         # runs the three stages above in sequence
```

All plotting scripts read `sensor_stream.csv` from the current directory. There is no test suite for either half (firmware/test/ is an untouched PlatformIO scaffold).

## Architecture

### Single shared UART

Data packets, plain-text debug/status lines, and START/STOP control commands from the PC all share **one** `Serial` port at 921600 baud (`SERIAL_BAUD` in `firmware/src/main.cpp` must match `BAUD_RATE` in `pc/receive.py`). `receive.py` distinguishes a binary packet from a debug line by peeking the leading byte: `0xAA` (start of the `0xAA 0xBB` sync sequence) means a packet follows; anything else is a text line up to `\n`. **Firmware must never call `Serial.print()`/`println()` while a packet is being written** (see `send_data()` in main.cpp) — interleaved text would land mid-packet and corrupt framing.

### Ping packet format

Defined by the packed `PingHeader` struct in `firmware/src/main.cpp` and mirrored by `HEADER_FORMAT`/`HEADER_SIZE` in `pc/receive.py` — the two must be kept in sync manually if either changes:

| Field | Type | Notes |
|---|---|---|
| `sync` | 2 bytes | `0xAA 0xBB` |
| `ping_id` | `uint32_t` | monotonic sequence number |
| `t_ping_us` | `int64_t` | `esp_timer_get_time()` at burst start — ESP32-relative, not wall-clock |
| `t_sample_us` | `int64_t` | `esp_timer_get_time()` at start of sampling |
| `num_samples` | `uint16_t` | = `BUFFER_SIZE` |
| `sample_dt_us` | `uint32_t` | nominal `1e6 / ADC_SAMPLE_FREQ_HZ` |
| payload | `num_samples × int16_t` | raw ADC samples |

`sensor_stream.csv` (written by `receive.py`) stores one row per packet with columns `Timestamp, PingID, T_ping_us, T_sample_us, NumSamples, SampleDt_us, Int_0..Int_{N-1}`. Only this current format is supported — older CSVs without `PingID`/timing columns won't load in `plot_sensor_stream.py`.

### Firmware signal path (`firmware/src/main.cpp`)

1. `emit_differential_burst()` drives a differential transducer pair via two hardware LEDC PWM channels (pin B electrically inverted relative to pin A), free-running at `PING_FREQUENCY` and gated on for `PING_CYCLES` cycles. The transducer is narrowband (measured Q~14, ~250 Hz -3dB bandwidth), which is why cycle count is deliberately small (6) rather than long — extra cycles just push out the near-field dead zone with no matched-filter compression benefit. A stepped-chirp drive was tried and reverted for the same reason (electrical bandwidth doesn't survive the transducer's narrowband response); that implementation is preserved on the `chirp-drive` git branch if a lower-Q transducer becomes available.
2. After an `AFTER_DELAY_US` settle delay (transducer mechanical ringdown), `capture_samples()` reads `BUFFER_SIZE` (2048) samples via the ESP-IDF ADC continuous/DMA driver at a true hardware-clocked `ADC_SAMPLE_FREQ_HZ` (100 kHz) — not `analogRead()`, which is software-timed and jittery (~25-40 kHz).
3. `repair_adc_glitches()` linearly interpolates across short runs of spurious exact-zero samples that the original ESP32's ADC digital controller emits at each hardware burst restart (a driver artifact, not real signal).
4. `send_data()` writes the header + raw `int16_t` payload straight to `Serial`.

Key tunables live at the top of `main.cpp`: `PING_FREQUENCY`, `PING_CYCLES`, ADC resolution/attenuation/`ADC_SAMPLE_FREQ_HZ`, `SERIAL_BAUD`.

### PC receive path (`pc/receive.py`)

- Auto-detects the sonar's port by probing `SERIAL_PORT_CANDIDATES` (`/dev/ttyUSB0`, `/dev/ttyUSB1`) and checking which one responds correctly to a START command; set `SERIAL_PORT` to a literal path to skip this. Set to `None` by default because the enumerated port has been observed to change.
- Uses a `BufferedReader` wrapper around the serial port rather than one-byte-at-a-time reads — at 921600 baud, syscall-per-byte reading can't keep up and risks silent kernel receive-buffer overflow/desync.
- If packet framing desyncs (e.g. from a STOP/START race where an in-flight ping still transmits after STOP), the read loop resyncs on the next `0xAA 0xBB` sync bytes and logs a `ping_id` gap warning rather than writing corrupted rows.

### PC plotting scripts

- `plot_sensor_stream.py` — raw waveform per packet; x-axis converted from sample index to one-way range in cm using `SampleDt_us` and `SPEED_OF_SOUND_M_S`.
- `plot_matched_filter.py` — `make_ping_template()` reconstructs the *actual* transmitted burst shape (not an idealized sinusoid) and cross-correlates it against the raw signal to compress each echo into a sharp peak.
- `plot_echogram.py` — applies the matched filter across the whole recording and renders amplitude envelope (Hilbert transform) as a range-vs-time waterfall in dB (`DYNAMIC_RANGE_DB`), blanking the near-range region still ringing from the transmit burst.
- `run_pipeline.py` — chains `receive.py` → `plot_echogram.py` → `plot_matched_filter.py`, each stage starting once the previous exits.

## Working notes

- This is a real physical hardware project with an admittedly fragile current build (transducer/preamp/wiring). When captured data looks physically implausible (noise, drift, gain, oscillation) but the serial framing/protocol layer is clean (`ping_id` continuity, no resync warnings), suspect the hardware/wiring before the firmware or PC code.
- Firmware and PC-side packet definitions (`PingHeader` in main.cpp, `HEADER_FORMAT` in receive.py) must be changed together — there's no shared schema file.
