# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An ESP32-based sonar front end and PC-side tooling for capturing and visualizing ping data over air:

- **firmware/** — PlatformIO/Arduino project for the ESP32. Drives a 10-element 40 kHz piezo transmit array (40T-12 in parallel, staggered ~25 mm spacing, through a DRV8871 H-bridge on 12 V) with a short tone burst, samples the receive signal via the ESP-IDF ADC continuous/DMA driver (since 2026-10-26 the transmit array itself is the receiver; before that a separate 40R-12), and streams framed ping packets over the flashing/debug UART.
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
python web_preview.py          # the ESP32's web echogram page at http://localhost:8000, replaying sensor_stream.csv

pytest                         # pc/tests/ (also `pytest pc/` from repo root)
```

All plotting scripts read `sensor_stream.csv` from the current directory. The tests cover packet parsing (`parse_next_event`), `BufferedReader`, CSV loading, live tailing, and the matched-filter/envelope math. They do not cover the serial handshake (`_probe_port`/`find_sonar_port`) or matplotlib rendering, which need real hardware. `firmware/test/` is an untouched scaffold.

### Bench (from repo root)

```bash
python bench/run_test.py --profile NAME setup|noise|step|sine|linearity|crosstalk|reanalyze|report
python bench/freq_sweep.py --profile NAME --target-m 2.0   # two-way response from a reflector's echo
python bench/wifi_ab.py --profile NAME                     # does Wi-Fi add noise?
pytest bench/
```

`freq_sweep.py` steps the drive frequency with `FREQ` (36–44 kHz, 250 Hz steps by default) with the transducers in place and a flat reflector at `--target-m`. It reads each step's echo in a window around the round trip and reports the peak frequency, the −3 dB band and the echo's leading-edge range. At the default 10 cycles the burst's own ~3.6 kHz width smooths the curve; `--cycles 50` measures the transducers' steady-state response.

With the mic removed, a bench generator drives the mic node through a ÷1000 (or ÷100) pad; the tools measure noise, step/frequency response, linearity and burst crosstalk and save each build as `bench/profiles/NAME/`. The firmware pings normally; the ping-locked part of each capture (across-ping mean) is subtracted before analysis and is itself the crosstalk measurement. `bench/` reuses `pc/` code via `sys.path` and must not modify `pc/`. Procedure: `bench/README.md`; past findings: `bench/NOTES.md`.

## Architecture

### Single shared UART

Packets, debug text, and START/STOP/TONE commands share **one** `Serial` port at 921600 baud (`SERIAL_BAUD` in `firmware/src/main.cpp` must match `BAUD_RATE` in `pc/receive.py`). `receive.py` treats a leading `0xAA` byte as the start of a packet and anything else as a text line up to `\n`. **Firmware must never print text while a packet is being written** (see `send_data()`), or framing breaks.

`TONE` hands IN1/IN2 to the LEDC for a continuous complementary square wave at the drive frequency (for scope debugging of the drive and receive chain) until STOP or START, which return them to coast. Keep the port open while it runs. Opening the port doesn't reliably reboot the ESP32 on this adapter (`ping_id` continues across sessions), so don't count on a reopen to clear anything.

`FREQ <hz>` (30–50 kHz) changes the drive frequency for pings and `TONE` until `RESET` or a reboot. Bursts get the requested frequency exactly, and the reply's `actual` is that; a running `TONE` is restarted on the LEDC, which can be a few Hz off and is reported separately. `CYCLES <n>` (1–200) likewise sets the burst length, and `DAMP <n>` (0–20, default 0) adds n half-cycles in anti-phase after the burst to oppose the array's ringing (it didn't help on 2026-10-26; see `bench/TEST_LOG.md`). The packet header carries none of them, so whoever sends them must record them.

### Wi-Fi and the web echogram

At boot the ESP32 starts an open access point, SSID `SimpleSonar`, and starts pinging (`PING_AUTOSTART`), so on a battery it runs with no PC. Join the network and open `http://192.168.4.1` for the live echogram. `WIFI ON|OFF|STATUS` switches the AP at run time (`wifi_start()`/`wifi_stop()`).

- **TCP bridge, `192.168.4.1:3333`:** a client gets the same byte stream as the UART and can send the same commands, so `receive.py --port socket://192.168.4.1:3333` works over Wi-Fi. All firmware output goes through `out`, which writes to both.
- **Web server, port 80** (ESP-IDF `esp_http_server`): `GET /` serves `firmware/src/web_page.h`; `GET /config` returns the drive settings as JSON; the `/ws` WebSocket sends each ping as one binary message (the packet, byte for byte) and takes text commands (START, STOP). Handlers run on the server's task: commands are queued for `loop()`, and pings go out via `httpd_queue_work()`. A ping still being sent to a slow browser makes the next one skip the browsers (the UART and bridge always get it); a client stalled for over 1 s is dropped. Up to `WEB_MAX_CLIENTS` (4) sessions.
- **The page** (`web_page.h`) does the processing of `plot_echogram_live.py` in the browser: matched filter, Hilbert envelope, blanking (below `MIN_DISPLAY_RANGE_CM` and the last template-length of samples, where the matched filter runs out; the range axis starts where the blanking ends), 40 dB scale with the 1 dB/ping reference. `GET /` is sent with `Cache-Control: no-store`. Its `<script id="dsp">` block is pure functions that `pc/tests/test_web_page_dsp.py` runs under Node against the Python versions; keep the two in step. `pc/web_preview.py` serves the same page on a PC, fed from a recording, for developing it without the AP.
- starfish's Wi-Fi is its only internet connection, so use the tablet as the Wi-Fi client, not starfish. Android won't rejoin a network without internet by itself after it drops.

`RESET` restores `PING_FREQUENCY`, `PING_CYCLES` and `PING_DAMP_HALF_CYCLES` (and stops `TONE`). `receive.py` sends it before START, so normal pinging always uses the defaults; `freq_sweep.py` sends it when it finishes.

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

1. `emit_burst()` brakes for `BRAKE_BEFORE_US` (100 µs) to wake the DRV8871, then drives its IN1/IN2 (`PING_PIN_A` GPIO25, `PING_PIN_B` GPIO26) complementary: `PING_CYCLES` × (forward, reverse), then back to brake. The edges are timed off the CPU cycle counter, from IRAM with interrupts off, so the cycle count is exact at any frequency and every ping is phase-locked to the ADC window, which allows coherent averaging and background subtraction. The function returns the burst-start time (`t_ping_us`); the DRV8871 adds a fixed ~1 µs (0.17 mm). The receive element is on `ANALOG_PIN` (GPIO34).
   - **Idle is coast (IN1 = IN2 = 0, outputs high-Z), so the array can also receive** (shared TX/RX transducer). `loop()` holds brake for `BRAKE_AFTER_US` (100 µs; 500 µs left the preamp pinned at a rail until the chip slept) after the burst, then coasts with a register write (`pinMode()` takes ~200 µs and shifted sampling); it must stay below `AFTER_DELAY_US` so coast starts before sampling. After 1 ms at 0/0 the DRV8871 sleeps, and waking it takes ~55 µs (the first two cycles of a burst), hence the brake before each burst. Brake shorts the array but adds little damping (that would need ~1/(ωC₀) ≈ 320 Ω). In coast the body diodes still clamp each output to −0.7 V / VM + 0.7 V.
2. After `AFTER_DELAY_US`, `capture_samples()` reads `BUFFER_SIZE` (4096) samples at a hardware-clocked `ADC_SAMPLE_FREQ_HZ` (200 kHz; `BUFFER_SIZE` must match `NUM_SAMPLES` in `receive.py`). Don't use `analogRead()`: it is software-timed and jittery.
3. `capture_samples()` un-swaps each adjacent sample pair (the original ESP32's ADC DMA delivers them swapped; captures from before 2026-10-14 still have the swap). The exact-zero glitch runs the ADC emitted at 100 kHz don't occur at 200 kHz, so their repair was removed; a 0 in the data is a real reading (clipping).
4. `send_data()` writes the header and raw `int16_t` payload to `Serial`.

The burst length, not transducer ringing, sets range resolution. (Measured on the 3.3 kHz speaker and microphone. At 40 kHz one element's ringing time constant is ~130 µs, comparable to the 250 µs burst, so this may no longer hold.) Use an echo's **leading edge**, not its envelope peak, as the range marker (`bench/direct_path.py`, `bench/echo_onset.py`).

### PC receive path (`pc/receive.py`)

- `SERIAL_PORT = None` auto-detects the port by sending START to each of `SERIAL_PORT_CANDIDATES` (`/dev/ttyUSB0`, `/dev/ttyUSB1`); set it (or `--port`) to a path or a `socket://` URL to skip detection. `open_port()` opens either; `SocketPort` fixes pyserial's socket backend, whose `in_waiting` only reports 0 or 1.
- The START that begins the read loop is sent with `settle_s=0` (also in `bench/capture.py`). With 8 KB packets, any pause before reading lets more than ~4.7 KB (tty buffer + USB-serial FIFO) pile up. That drops one byte, which misaligns the first packet's samples and destroys the next packet's sync.
- A `BufferedReader` wraps the port, because reading one byte per syscall can't keep up at 921600 baud.
- Framing and resync live in `parse_next_event()`, which is separate from the serial/CSV I/O so it can be unit-tested (`pc/tests/test_receive_parse.py`). On desync (e.g. an in-flight ping after STOP), it resyncs on the next `0xAA 0xBB` and logs a `ping_id` gap instead of writing corrupt rows.

### PC plotting scripts

- `plot_sensor_stream.py` — raw waveform per packet. Its `range_axis_cm()` (used by every plot script) converts samples to one-way range from burst start using `SampleDt_us`, `SPEED_OF_SOUND_M_S`, and `T_sample_us - T_ping_us` (sampling starts ~1.24 ms / ~21 cm after the burst).
- `plot_matched_filter.py` — `make_ping_template()` rebuilds the actual transmitted burst and cross-correlates it with the signal. `PING_FREQUENCY_HZ`/`PING_CYCLES` here must match the firmware.
- `plot_echogram.py` — matched-filter envelope (Hilbert) waterfall in dB, blanked below `MIN_DISPLAY_RANGE_CM` (40 cm, for the shared transducer's near-range overload; the web page has its own copy) and over the matched filter's last template-length of samples. `--background NO_TARGET.csv` subtracts a no-target capture's mean waveform to cancel static clutter; record it back-to-back with the data, with nobody in the beam. The matched-filter band peaks ~20 cm beyond an echo's true leading edge.
- `plot_echogram_live.py` — the same view, tailing `sensor_stream.csv` live (`WINDOW_PINGS` wide). Its dB scale is relative to the strongest return in the visible window, so a strong transient stops dimming the display once it scrolls off. It never touches the serial port.
- `run_pipeline.py` — runs the three stages in sequence.

### Branches

- `chirp-drive` — stepped-chirp drive, reverted for the old narrowband piezo. Unlikely to help the narrowband 40 kHz piezos.
- `add-wiener-deconvolution` — PC-side Wiener deconvolution. It is unmerged, and its firmware conflicts with master. `bench/WIENER_EVALUATION.md` found no verified advantage over the matched filter, which already separates targets 40 cm apart.

## Working notes

- **Supply isolation matters most in the analog front end.** Keep the H-bridge on its own supply, keep the preamp supply filtered (22 Ω + 100 µF + 0.1 µF at its pins), use a single-point ground, and re-run `bench/run_test.py ... crosstalk` after any wiring change.
- **Transmit drive:** a DRV8871 breakout on 12 V gives 24 V peak-to-peak across the array (the audio amplifier it replaced gave 7.8 V, so about +10 dB). The array is 10 × 40T-12 in parallel, 12.5 nF in total. Each 24 V edge into that capacitance would draw ~3 A spikes, so each output has 10 Ω in series (peak ~1.2 A, ~250 ns edges), with 47 µF + 0.1 µF at VM and a twisted pair to the array. The spikes are shorter than the DRV8871's 2 µs current-limit blanking, so its ILIM setting never chops a burst.
- **Preamp (since 2026-10-26, shared TX/RX):** one LF356 inverting stage, 1 MΩ feedback with no capacitor (bandwidth ≈ 5 MHz GBW / noise gain ~100 ≈ 50 kHz), + input on a virtual ground (100k/100k divider, 10 µF across the lower one). From the array: OUT1 → 3.3 kΩ → antiparallel diode clamp to virtual ground → 3.3 kΩ → summing junction; OUT2 → 3.3 kΩ → virtual ground. Every return in that network must go to virtual ground, not ground: the diodes to ground saturated the stage, and OUT2's resistor to ground gave a large DC step after each brake. The output is capacitor-coupled to GPIO34 through 1 kΩ.
- Before that, the preamp was an AD828. Profile `ad828-v1` (93 counts/mV, 37.5 dB, −3 dB band 75 Hz–5.1 kHz, ~1 count RMS noise) predates the 40 kHz conversion: the feedback caps across the 20 kΩ resistors are now 130 pF (~61 kHz pole per stage, ~−3 dB at 40 kHz for both), and the electret bias network has been removed. The receive element is loaded directly by the 2.2 kΩ input resistor.
- The transducers' two-way steady-state response peaks at 40.0 kHz, with a −3 dB band of 39.2–41.0 kHz (1.8 kHz; profile `sweep-2026-10-22-50cyc`). That limits range resolution to ~10 cm whatever the waveform, so chirps and deconvolution have little to gain.
- The hardware build is fragile. If the data looks physically implausible (noise, drift, gain, oscillation) but framing is clean (`ping_id` continuous, no resync warnings), suspect the hardware or wiring before the code.

## Test results across machines

Hardware tests run on the test laptop; most development happens on the office PC. The machines are **starfish** (the host) and **crab** (the laptop); label `bench/TEST_LOG.md` entries with the hostname. Tests can also be run on starfish. Git is the only channel between them, so test results go in the repo, not in Claude's per-machine memory.

- **After a test session (laptop):** add a dated entry at the top of `bench/TEST_LOG.md` (hardware state, captures and their paths, results, decisions, open items), update its open-items list, then commit and push to master. Move durable findings into `bench/NOTES.md` or this file.
- **At the start of a session (either machine):** the `SessionStart` hook in `.claude/settings.json` runs `git pull --ff-only` and prints the newest log entries. If it reports a failed pull, pull manually before relying on the log.
- Raw captures (`bench/profiles/*/raw/`) are gitignored and stay on the laptop. Commit plots, `profile.json` and the analysis scripts so results can be reproduced and reviewed elsewhere.
