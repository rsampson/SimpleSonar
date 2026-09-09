# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An ESP32-based sonar front end and PC-side tooling for capturing and visualizing ping data over air. Two halves:

- **firmware/** — PlatformIO/Arduino project for the ESP32. Drives a differential transducer pair, samples the reflected signal via the ESP-IDF ADC continuous/DMA driver, and streams framed ping packets over the same UART used for flashing/debug output.
- **pc/** — Python tools that receive, log, and visualize ping data (including a Wiener deconvolution stage that removes the transducer's ringdown from each echo).

The transducer is under active iteration — see [Transducer](#firmware-signal-path-firmwaresrcmaincpp) below. Several tunables must track whichever transducer is currently fitted; see [Working notes](#working-notes).

## Commands

### Firmware (from `firmware/`)

```bash
pio run --target upload   # build and flash
pio run                   # build only
pio device monitor        # serial monitor (921600 baud)
```

Note: system-wide `pio` (apt package) has been broken on this machine (Click library incompatibility) — use `~/.platformio/penv/bin/pio` if the `pio` on PATH fails.

Upload/monitor port is hardcoded in `firmware/platformio.ini` (currently `/dev/ttyUSB0`) and may need updating since the ESP32's USB-serial enumeration has been observed to shift between `/dev/ttyUSB0`/`/dev/ttyUSB1` depending on what else is plugged in.

### PC (from `pc/`)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python receive.py              # record a stream to sensor_stream.csv (Ctrl+C to stop; --fresh to discard existing CSV)
python plot_sensor_stream.py   # raw per-packet waveform, ←/→ to navigate packets
python plot_matched_filter.py  # raw + matched-filter + Wiener-deconvolved view, ←/→ to navigate
python plot_echogram.py        # full-recording waterfall echogram (add --deconv for the Wiener stage)
python plot_echogram_live.py   # live-scrolling echogram - run alongside receive.py in a second terminal (add --deconv)
python plot_lambda_sweep.py    # overlay several WIENER_LAMBDA values on one averaged-envelope plot to pick one
python run_pipeline.py         # runs receive.py, plot_echogram.py, plot_matched_filter.py in sequence

pytest                         # run the pc/tests/ suite (also runnable as `pytest pc/` from repo root)
```

All plotting scripts read `sensor_stream.csv` from the current directory. The `--deconv` flag on `plot_echogram.py`/`plot_echogram_live.py` turns on the Wiener deconvolution stage (`--deconv-domain mf|raw`, default `mf`); it is off by default. `plot_matched_filter.py` always shows the deconvolved trace as a third panel for A/B comparison. See [PC plotting scripts](#pc-plotting-scripts) and [Ringdown deconvolution](#ringdown-deconvolution-pcdeconvolvepy).

`pc/` has a pytest suite (`pc/tests/`, config in `pc/pyproject.toml`) covering packet framing/parsing (`receive.py`'s `parse_next_event`), `BufferedReader`, CSV loading, the live-tailing logic in `plot_echogram_live.py`, the matched-filter/envelope math, and the Wiener deconvolution (`deconvolve.py`'s `make_impulse_response`/`wiener_deconvolve`/`deconvolved_response`) — it does not cover the hardware-facing serial handshake (`_probe_port`/`find_sonar_port`) or matplotlib rendering, which need real hardware/manual verification. `firmware/test/` is an untouched PlatformIO scaffold.

## Build Verification

Always verify embedded changes compile before declaring done: run `pio run -e <env>` (or `arduino-cli compile`) after any firmware edit. If no compiler/toolchain is available in the sandbox, say so explicitly and mark the change UNVERIFIED in your summary.

## PlatformIO Library Resolution

This project uses PlatformIO with `lib_ldf_mode` conflicts against globally installed `~/Arduino/libraries` (duplicate AsyncTCP, Preferences). Pin library versions in platformio.ini rather than relying on LDF, and check for global duplicates first when a build fails on library resolution.

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

1. `emit_differential_burst()` drives a differential transducer pair via two hardware LEDC PWM channels (pin B electrically inverted relative to pin A), free-running at `PING_FREQUENCY` and gated on for `PING_CYCLES` cycles. **Transducer (under active iteration):** currently an 11-element voice-coil array (33 mm element spacing, 90 Ω, 24 Vpp drive); measured resonance f0 ≈ 2775 Hz, Q ≈ 47, ~60 Hz -3 dB bandwidth. `PING_FREQUENCY` is set to 2750 Hz (near resonance — a voice-coil array driven off resonance responds weakly and rings at its own f0 anyway). The array is *higher* Q than the original narrowband piezo it replaced (Q~14, ~3500-3700 Hz), so each echo rings ~34 cycles (~1200 of the 2048 samples) — this is what the PC-side Wiener deconvolution stage exists to undo. Cycle count is deliberately small (6): extra cycles just push out the near-field dead zone with no matched-filter compression benefit on a narrowband transducer. A stepped-chirp drive was tried and reverted for the same reason (electrical bandwidth doesn't survive the transducer's narrowband response); that implementation is preserved on the `chirp-drive` git branch if a genuinely low-Q wideband transducer is fitted.
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

- `plot_sensor_stream.py` — raw waveform per packet; x-axis converted from sample index to one-way range in cm using `SampleDt_us` and `SPEED_OF_SOUND_M_S`.
- `plot_matched_filter.py` — `make_ping_template()` reconstructs the *actual* transmitted burst shape (not an idealized sinusoid) and cross-correlates it against the raw signal to compress each echo into a sharp peak.
- `plot_echogram.py` — applies the matched filter across the whole recording and renders amplitude envelope (Hilbert transform) as a range-vs-time waterfall in dB (`DYNAMIC_RANGE_DB`), blanking the near-range region still ringing from the transmit burst.
- `plot_echogram_live.py` — same matched-filter/envelope waterfall as `plot_echogram.py`, but tails `sensor_stream.csv` as `receive.py` writes it and shows a live-scrolling fixed-width window (`WINDOW_PINGS`) instead of the whole finished recording. Run as a second process alongside `receive.py`; never touches the serial port. `--deconv` enables the Wiener stage live.
- `plot_lambda_sweep.py` — offline tuning aid for `WIENER_LAMBDA`: matched-filters + Wiener-deconvolves every ping at each λ in a list, averages the envelope over all pings (stationary reflections reinforce, noise averages down), and overlays one dB curve per λ with a marker at a known target range (`--target-cm`). Pick the smallest λ with a sharp target peak and a clean background.
- `run_pipeline.py` — chains `receive.py` → `plot_echogram.py` → `plot_matched_filter.py`, each stage starting once the previous exits.

### Ringdown deconvolution (`pc/deconvolve.py`)

A high-Q transducer makes each echo ring for many cycles (see the transducer note above), which smears targets in range — a matched filter can't fix this because there's no bandwidth to compress. `deconvolve.py` estimates the reflectivity series by dividing the received spectrum by a model of the transducer's impulse response `H(f)`, regularized (Wiener) so spectral nulls and out-of-band noise don't blow up. Pure numpy rfft/irfft — no scipy, mirroring `plot_echogram.hilbert_envelope`'s "reimplement the one transform we need" precedent.

- **Cascade:** `matched_filter` first (SNR + band-limiting), then `wiener_deconvolve` on its output against the matched-filter-of-`h` kernel (`domain='mf'`, the default). `domain='raw'` skips the matched filter and deconvolves the raw trace — measurably worse resolution here (the raw signal isn't band-limited, so the inverse filter smears more), kept only as an experimental knob.
- **Impulse-response model:** synthetic damped sinusoid `exp(-π·f0·t/Q)·sin(2π·f0·t)`, optionally convolved with the drive template. Tuned by two constants: `RINGDOWN_FREQUENCY_HZ` (≈ the transducer resonance) and `RINGDOWN_Q` (read off a strong single echo: Q ≈ π × cycles from the ring peak to the 1/e envelope point). Not tied to `PING_FREQUENCY` — the array rings at its own f0.
- **Regularization:** `WIENER_LAMBDA` (fraction of peak `|H|²`) trades ring-collapse against noise/clutter amplification. Currently 0.03, picked by eye with `plot_lambda_sweep.py` in a poor (cluttered, small) test room; re-sweep against an isolated close reflector when a better test space is available.
- **Bandwidth limit:** with ~60 Hz transducer bandwidth the fundamental range resolution is c/(2B) ≈ 2-3 m. Deconvolution widens the *effective* band past the -3 dB points where SNR allows, so it visibly tightens the ring but does not produce a true spike.

**On a transducer swap**, re-tune together: `PING_FREQUENCY` (firmware) + `PING_FREQUENCY_HZ` (`plot_matched_filter.py`) to the new resonance; `RINGDOWN_FREQUENCY_HZ` + `RINGDOWN_Q` (`deconvolve.py`) to the new ringdown; then re-run `plot_lambda_sweep.py` to re-pick `WIENER_LAMBDA`.

## Working notes

- This is a real physical hardware project with an admittedly fragile current build (transducer/preamp/wiring). When captured data looks physically implausible (noise, drift, gain, oscillation) but the serial framing/protocol layer is clean (`ping_id` continuity, no resync warnings), suspect the hardware/wiring before the firmware or PC code.
- Firmware and PC-side packet definitions (`PingHeader` in main.cpp, `HEADER_FORMAT` in receive.py) must be changed together — there's no shared schema file.
- **Transducer-dependent tunables must all track the fitted transducer's resonance/ringdown:** `PING_FREQUENCY` (firmware `main.cpp`), `PING_FREQUENCY_HZ` (`pc/plot_matched_filter.py`, must match `PING_FREQUENCY`), and `RINGDOWN_FREQUENCY_HZ`/`RINGDOWN_Q`/`WIENER_LAMBDA` (`pc/deconvolve.py`). See [Ringdown deconvolution](#ringdown-deconvolution-pcdeconvolvepy) for the swap checklist.
