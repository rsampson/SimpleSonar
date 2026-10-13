# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An ESP32-based sonar front end and PC-side tooling for capturing and visualizing ping data over air. Two halves:

- **firmware/** — PlatformIO/Arduino project for the ESP32. Drives a speaker (through an audio amplifier) with a short tone burst, samples the reflected signal from a microphone via the ESP-IDF ADC continuous/DMA driver, and streams framed ping packets over the same UART used for flashing/debug output.
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
python plot_echogram_live.py   # live-scrolling echogram - run alongside receive.py in a second terminal
python run_pipeline.py         # runs receive.py, plot_echogram.py, plot_matched_filter.py in sequence

pytest                         # run the pc/tests/ suite (also runnable as `pytest pc/` from repo root)
```

All plotting scripts read `sensor_stream.csv` from the current directory. `pc/` has a pytest suite (`pc/tests/`, config in `pc/pyproject.toml`) covering packet framing/parsing (`receive.py`'s `parse_next_event`), `BufferedReader`, CSV loading, the live-tailing logic in `plot_echogram_live.py`, and the matched-filter/envelope math — it does not cover the hardware-facing serial handshake (`_probe_port`/`find_sonar_port`) or matplotlib rendering, which need real hardware/manual verification. `firmware/test/` is an untouched PlatformIO scaffold.

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

1. `emit_burst()` drives a single-ended square wave on `PING_PIN` (GPIO25) into an audio amplifier and 2.5" 8 Ω speaker via one hardware LEDC PWM channel, free-running at `PING_FREQUENCY` and gated on for `PING_CYCLES` cycles. (The earlier piezo transducer needed a differential pair on GPIO25/26; GPIO26 is now `UNUSED_PIN`, left as a high-impedance input.) The receiver is an electret microphone on `ANALOG_PIN` (GPIO34, input-only). The LEDC timer is reset before each burst (`ledc_timer_rst` on timer 0, channel pinned via `PING_CHANNEL`) so the burst starts exactly one period after the reset instead of at a random point in the free-running cycle (a duty change only latches at the next period boundary); this phase-locks every ping to the ADC window so waveforms can be averaged coherently. `emit_burst()` returns the true burst-start timestamp, which becomes `t_ping_us`. `PING_INTERVAL_MS` (300) spaces successive bursts: at the ~74 ms the serial transfer alone allows, the garage's reverberation from earlier pings piled up under later echoes (a 162 cm target visibly changed the signal at 60 cm, before any echo could arrive); at 300 ms that pre-echo contamination dropped ~8x. Direct-path speaker→mic measurements (mic 50 in and 100 in away) put the received pulse at ~3.2 kHz with a ~2.9-3.5 kHz -3 dB band and Q≈7: it builds over ~1.7 ms (≈ the 1.82 ms, 6-cycle burst) and decays to 1/e in ~0.7 ms, so the burst length, not transducer ringing, now sets range resolution. Arrival times matched the 345.8 m/s prediction to ~20 µs, i.e. the timestamps/range axis carry no meaningful fixed delay; use an echo's leading edge, not its envelope peak (~1.7 ms late), as the range marker. The slow (<300 Hz) transient present early in every ping is electrical coupling, not acoustic (it persists with the mic 2.5 m away). The old piezo was narrowband (Q~14), which is why the cycle count was kept small and a stepped-chirp drive was reverted; that implementation is preserved on the `chirp-drive` git branch and may be worth revisiting with the lower-Q speaker.
2. After an `AFTER_DELAY_US` settle delay (transducer mechanical ringdown), `capture_samples()` reads `BUFFER_SIZE` (2048) samples via the ESP-IDF ADC continuous/DMA driver at a true hardware-clocked `ADC_SAMPLE_FREQ_HZ` (100 kHz) — not `analogRead()`, which is software-timed and jittery (~25-40 kHz).
3. `repair_adc_glitches()` linearly interpolates across short runs of spurious exact-zero samples that the original ESP32's ADC digital controller emits at each hardware burst restart (a driver artifact, not real signal).
4. `send_data()` writes the header + raw `int16_t` payload straight to `Serial`.

Key tunables live at the top of `main.cpp`: `PING_FREQUENCY`, `PING_CYCLES`, ADC resolution/attenuation/`ADC_SAMPLE_FREQ_HZ`, `SERIAL_BAUD`.

### PC receive path (`pc/receive.py`)

- Auto-detects the sonar's port by probing `SERIAL_PORT_CANDIDATES` (`/dev/ttyUSB0`, `/dev/ttyUSB1`) and checking which one responds correctly to a START command; set `SERIAL_PORT` to a literal path to skip this. Set to `None` by default because the enumerated port has been observed to change.
- Uses a `BufferedReader` wrapper around the serial port rather than one-byte-at-a-time reads — at 921600 baud, syscall-per-byte reading can't keep up and risks silent kernel receive-buffer overflow/desync.
- The packet-framing/resync state machine lives in `parse_next_event()`, a standalone function (takes a `BufferedReader`-like reader + a small state dict, returns a `ParseResult` describing what happened) kept separate from the serial/CSV I/O in `continuous_serial_to_csv()` specifically so it's unit-testable without a real serial port — see `pc/tests/test_receive_parse.py`.
- If packet framing desyncs (e.g. from a STOP/START race where an in-flight ping still transmits after STOP), the read loop resyncs on the next `0xAA 0xBB` sync bytes and logs a `ping_id` gap warning rather than writing corrupted rows.

### PC plotting scripts

- `plot_sensor_stream.py` — raw waveform per packet; x-axis is one-way range in cm from burst start, computed by `range_axis_cm()` from `SampleDt_us`, `SPEED_OF_SOUND_M_S`, and the per-packet `T_sample_us - T_ping_us` offset (sampling starts ~2.8 ms / ~48 cm after the burst begins). All the plotting scripts use this helper.
- `plot_matched_filter.py` — `make_ping_template()` reconstructs the *actual* transmitted burst shape (not an idealized sinusoid) and cross-correlates it against the raw signal to compress each echo into a sharp peak.
- `plot_echogram.py` — applies the matched filter across the whole recording and renders amplitude envelope (Hilbert transform) as a range-vs-time waterfall in dB (`DYNAMIC_RANGE_DB`), blanking the near-range region still ringing from the transmit burst.
- `plot_echogram_live.py` — same matched-filter/envelope waterfall as `plot_echogram.py`, but tails `sensor_stream.csv` as `receive.py` writes it and shows a live-scrolling fixed-width window (`WINDOW_PINGS`) instead of the whole finished recording. Run as a second process alongside `receive.py`; never touches the serial port.
- `run_pipeline.py` — chains `receive.py` → `plot_echogram.py` → `plot_matched_filter.py`, each stage starting once the previous exits.

## Working notes

- This is a real physical hardware project with an admittedly fragile current build (transducer/preamp/wiring). When captured data looks physically implausible (noise, drift, gain, oscillation) but the serial framing/protocol layer is clean (`ping_id` continuity, no resync warnings), suspect the hardware/wiring before the firmware or PC code.
- Firmware and PC-side packet definitions (`PingHeader` in main.cpp, `HEADER_FORMAT` in receive.py) must be changed together — there's no shared schema file.
