# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An ESP32-based sonar front end and PC-side tooling for capturing and visualizing ping data over air:

- **firmware/** — PlatformIO/Arduino project for the ESP32. Drives an 11-element 40 kHz piezo transmit array (40T-12, staggered ~25 mm spacing, through an audio amplifier) with a short tone burst, samples a single 40R-12 receive element via the ESP-IDF ADC continuous/DMA driver, and streams framed ping packets over the flashing/debug UART.
- **pc/** — Python tools that receive, log, and visualize ping data.
- **bench/** — characterization of the electronics alone (see below).
- **hardware/** — KiCad designs: `sonar_preamp/` (older TL081 preamp, kept deliberately) and `sonar_amp.*` (separate board, untracked). Edit KiCad files only through the Konnect MCP tools.

## Commands

### Firmware (from `firmware/`)

```bash
pio run --target upload   # build and flash
pio run                   # build only
pio device monitor        # serial monitor (921600 baud)
```

If the system `pio` fails (broken apt package), use `~/.platformio/penv/bin/pio`. The upload/monitor port is hardcoded in `firmware/platformio.ini` (`/dev/ttyUSB0`); the ESP32 sometimes enumerates as `/dev/ttyUSB1` instead.

### PC (from `pc/`)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python receive.py              # record to sensor_stream.csv (Ctrl+C to stop; --fresh discards existing CSV)
python plot_sensor_stream.py   # raw per-packet waveform, ←/→ to navigate
python plot_matched_filter.py  # raw + matched-filter view, ←/→ to navigate
python plot_echogram.py        # full-recording waterfall echogram
python plot_echogram_live.py   # live echogram; run alongside receive.py
python run_pipeline.py         # receive.py → plot_echogram.py → plot_matched_filter.py

pytest                         # pc/tests/ (also `pytest pc/` from repo root)
```

All plotting scripts read `sensor_stream.csv` from the current directory. The tests cover packet parsing (`parse_next_event`), `BufferedReader`, CSV loading, live tailing, and the matched-filter/envelope math. They do not cover the serial handshake (`_probe_port`/`find_sonar_port`) or matplotlib rendering, which need real hardware. `firmware/test/` is an untouched scaffold.

### Bench (from repo root)

```bash
python bench/run_test.py --profile NAME setup|noise|step|sine|linearity|crosstalk|reanalyze|report
python bench/freq_sweep.py --profile NAME --target-m 2.0   # two-way response from a reflector's echo
pytest bench/
```

`freq_sweep.py` steps the drive frequency with `FREQ` (36–44 kHz, 250 Hz steps by default) with the transducers in place and a flat reflector at `--target-m`. It reads each step's echo in a window around the round trip and reports the peak frequency, the −3 dB band and the echo's leading-edge range. At the default 10 cycles the burst's own ~3.6 kHz width smooths the curve; `--cycles 50` measures the transducers' steady-state response.

With the mic removed, a bench generator drives the mic node through a ÷1000 (or ÷100) pad; the tools measure noise, step/frequency response, linearity and burst crosstalk and save each build as `bench/profiles/NAME/`. The firmware pings normally; the ping-locked part of each capture (across-ping mean) is subtracted before analysis and is itself the crosstalk measurement. `bench/` reuses `pc/` code via `sys.path` and must not modify `pc/`. Procedure: `bench/README.md`; past findings: `bench/NOTES.md`.

## Architecture

### Single shared UART

Packets, debug text, and START/STOP/TONE commands share **one** `Serial` port at 921600 baud (`SERIAL_BAUD` in `firmware/src/main.cpp` must match `BAUD_RATE` in `pc/receive.py`). `receive.py` treats a leading `0xAA` byte as the start of a packet and anything else as a text line up to `\n`. **Firmware must never print text while a packet is being written** (see `send_data()`), or framing breaks.

`TONE` holds `PING_PIN` on as a continuous `PING_FREQUENCY` square wave (for scope debugging of the drive and receive chain) until STOP or START. The port must stay open while it runs, because reopening it resets the ESP32.

`FREQ <hz>` (30–50 kHz) changes the drive frequency for pings and `TONE` until reset, and replies with the LEDC's actual frequency, which can differ by a few Hz. `emit_burst()` times the burst in ns, so frequencies whose period isn't a whole µs still give exactly `PING_CYCLES` cycles. `CYCLES <n>` (1–200) likewise sets the burst length until reset. The packet header carries neither, so whoever sends them must record them.

### Ping packet format

The packed `PingHeader` struct in `main.cpp` is mirrored by `HEADER_FORMAT`/`HEADER_SIZE` in `receive.py`. There is no shared schema, so change both together.

| Field | Type | Notes |
|---|---|---|
| `sync` | 2 bytes | `0xAA 0xBB` |
| `ping_id` | `uint32_t` | monotonic sequence number |
| `t_ping_us` | `int64_t` | `esp_timer_get_time()` at burst start (ESP32-relative) |
| `t_sample_us` | `int64_t` | `esp_timer_get_time()` at start of sampling |
| `num_samples` | `uint16_t` | = `BUFFER_SIZE` |
| `sample_dt_us` | `uint32_t` | nominal `1e6 / ADC_SAMPLE_FREQ_HZ` |
| payload | `num_samples × int16_t` | raw ADC samples |

`sensor_stream.csv` has one row per packet: `Timestamp, PingID, T_ping_us, T_sample_us, NumSamples, SampleDt_us, Int_0..Int_{N-1}`. Older CSVs without the `PingID`/timing columns are not supported.

### Firmware signal path (`firmware/src/main.cpp`)

Key tunables are at the top of the file: `PING_FREQUENCY` (40000 Hz), `PING_CYCLES` (10), `PING_INTERVAL_MS` (300), ADC settings, `SERIAL_BAUD`.

1. `emit_burst()` drives a single-ended square wave on `PING_PIN` (GPIO25) via one LEDC PWM channel, gated on for `PING_CYCLES` cycles. GPIO26 (`UNUSED_PIN`) is left as a high-impedance input. The LEDC timer is reset before each burst so every ping is phase-locked to the ADC window, which allows coherent averaging and background subtraction. The function returns the true burst-start time (`t_ping_us`). The receive element is on `ANALOG_PIN` (GPIO34).
2. After `AFTER_DELAY_US`, `capture_samples()` reads `BUFFER_SIZE` (4096) samples at a hardware-clocked `ADC_SAMPLE_FREQ_HZ` (200 kHz; `BUFFER_SIZE` must match `NUM_SAMPLES` in `receive.py`). Don't use `analogRead()`: it is software-timed and jittery.
3. `capture_samples()` un-swaps each adjacent sample pair (the original ESP32's ADC DMA delivers them swapped; captures from before 2026-10-14 still have the swap). The exact-zero glitch runs the ADC emitted at 100 kHz don't occur at 200 kHz, so their repair was removed; a 0 in the data is a real reading (clipping).
4. `send_data()` writes the header and raw `int16_t` payload to `Serial`.

The burst length, not transducer ringing, sets range resolution. Use an echo's **leading edge**, not its envelope peak, as the range marker (`bench/direct_path.py`, `bench/echo_onset.py`).

### PC receive path (`pc/receive.py`)

- `SERIAL_PORT = None` auto-detects the port by sending START to each of `SERIAL_PORT_CANDIDATES` (`/dev/ttyUSB0`, `/dev/ttyUSB1`); set it to a path to skip detection.
- The START that begins the read loop is sent with `settle_s=0` (also in `bench/capture.py`). With 8 KB packets, any pause before reading lets more than ~4.7 KB (tty buffer + USB-serial FIFO) pile up. That drops one byte, which misaligns the first packet's samples and destroys the next packet's sync.
- A `BufferedReader` wraps the port, because reading one byte per syscall can't keep up at 921600 baud.
- Framing and resync live in `parse_next_event()`, which is separate from the serial/CSV I/O so it can be unit-tested (`pc/tests/test_receive_parse.py`). On desync (e.g. an in-flight ping after STOP), it resyncs on the next `0xAA 0xBB` and logs a `ping_id` gap instead of writing corrupt rows.

### PC plotting scripts

- `plot_sensor_stream.py` — raw waveform per packet. Its `range_axis_cm()` (used by every plot script) converts samples to one-way range from burst start using `SampleDt_us`, `SPEED_OF_SOUND_M_S`, and `T_sample_us - T_ping_us` (sampling starts ~1.24 ms / ~21 cm after the burst).
- `plot_matched_filter.py` — `make_ping_template()` rebuilds the actual transmitted burst and cross-correlates it with the signal. `PING_FREQUENCY_HZ`/`PING_CYCLES` here must match the firmware.
- `plot_echogram.py` — matched-filter envelope (Hilbert) waterfall in dB, with near-range blanking. `--background NO_TARGET.csv` subtracts a no-target capture's mean waveform to cancel static clutter; record it back-to-back with the data, with nobody in the beam. The matched-filter band peaks ~20 cm beyond an echo's true leading edge.
- `plot_echogram_live.py` — the same view, tailing `sensor_stream.csv` live (`WINDOW_PINGS` wide). Its dB scale is relative to the strongest return in the visible window, so a strong transient stops dimming the display once it scrolls off. It never touches the serial port.
- `run_pipeline.py` — runs the three stages in sequence.

### Branches

- `chirp-drive` — stepped-chirp drive, reverted for the old narrowband piezo. Unlikely to help the narrowband 40 kHz piezos.
- `add-wiener-deconvolution` — PC-side Wiener deconvolution. It is unmerged, and its firmware conflicts with master. `bench/WIENER_EVALUATION.md` found no verified advantage over the matched filter, which already separates targets 40 cm apart.

## Working notes

- **Supply isolation matters most in the analog front end.** Keep the speaker amp on its own supply, keep the preamp supply filtered (22 Ω + 100 µF + 0.1 µF at its pins), use a single-point ground, and re-run `bench/run_test.py ... crosstalk` after any wiring change.
- The fitted preamp is an AD828. Profile `ad828-v1` (93 counts/mV, 37.5 dB, −3 dB band 75 Hz–5.1 kHz, ~1 count RMS noise) predates the 40 kHz conversion: the feedback caps across the 20 kΩ resistors are now 130 pF (~61 kHz pole per stage, ~−3 dB at 40 kHz for both), and the electret bias network has been removed. The receive element is loaded directly by the 2.2 kΩ input resistor.
- The transducers' two-way steady-state response peaks at 40.0 kHz, with a −3 dB band of 39.2–41.0 kHz (1.8 kHz; profile `sweep-2026-10-22-50cyc`). That limits range resolution to ~10 cm whatever the waveform, so chirps and deconvolution have little to gain.
- The hardware build is fragile. If the data looks physically implausible (noise, drift, gain, oscillation) but framing is clean (`ping_id` continuous, no resync warnings), suspect the hardware or wiring before the code.

## Test results across machines

Hardware tests run on the test laptop; most development happens on the office PC. The machines are **starfish** (the host) and **crab** (the laptop); label `bench/TEST_LOG.md` entries with the hostname. Tests can also be run on starfish. Git is the only channel between them, so test results go in the repo, not in Claude's per-machine memory.

- **After a test session (laptop):** add a dated entry at the top of `bench/TEST_LOG.md` (hardware state, captures and their paths, results, decisions, open items), update its open-items list, then commit and push to master. Move durable findings into `bench/NOTES.md` or this file.
- **At the start of a session (either machine):** the `SessionStart` hook in `.claude/settings.json` runs `git pull --ff-only` and prints the newest log entries. If it reports a failed pull, pull manually before relying on the log.
- Raw captures (`bench/profiles/*/raw/`) are gitignored and stay on the laptop. Commit plots, `profile.json` and the analysis scripts so results can be reproduced and reviewed elsewhere.
