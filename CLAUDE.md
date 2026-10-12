# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

An ESP32-based sonar front end and PC-side tooling for capturing and visualizing ping data over air. Two halves:

- **firmware/** — PlatformIO/Arduino project for the ESP32. Drives a single-ended speaker, samples the reflected signal (via a separate microphone) using the ESP-IDF ADC continuous/DMA driver, and streams framed ping packets over the same UART used for flashing/debug output.
- **pc/** — Python tools that receive, log, and visualize ping data (including a Wiener deconvolution stage that removes ringdown/settling from each echo).

The transducer setup is under active iteration — see [Transducer](#firmware-signal-path-firmwaresrcmaincpp) below. Several tunables must track whichever speaker/microphone pair is currently fitted; see [Working notes](#working-notes).

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

1. `emit_ping_burst()` drives a single-ended speaker on one hardware LEDC PWM channel (`PING_PIN_A`, GPIO 25) — see [ADR 0001](docs/adr/0001-single-ended-chirp-drive.md) for why this replaced the prior differential two-pin drive. **Transducer (under active iteration):** an 8 Ω voice-coil speaker driven by a separate 2 W amplifier (single-ended, so no complementary second drive pin), with a separate electret microphone as the receive element — replacing the retired 11-element differential voice-coil array. Measured speaker resonance ≈ 4100 Hz. The burst is a **stepped chirp** (`PING_CHIRP_START_HZ`..`PING_CHIRP_END_HZ`, ±20% around the measured resonance, `PING_CHIRP_STEPS` discrete LEDC frequency steps of `PING_CHIRP_CYCLES_PER_STEP` cycles each) rather than a fixed tone — reviving the approach from the old `chirp-drive` branch, redesigned for single-ended drive, on the expectation that this speaker is much more wideband/lower-Q than either prior transducer. This is an active experiment: kept if it performs well, reverted to a fixed tone otherwise. After the burst, a brief opposite-polarity pulse (`BRAKE_PULSE_US`, unverified/TBD) brakes the speaker coil before parking the pin low — the single-ended analog of the old design's two-pin short.
2. After an `AFTER_DELAY_US` settle delay — now understood as letting the front-end amplifier settle, not waiting out the emitting element's own ringdown, since emit and receive are separate transducers — `capture_samples()` reads `BUFFER_SIZE` (2048) samples via the ESP-IDF ADC continuous/DMA driver at a true hardware-clocked `ADC_SAMPLE_FREQ_HZ` (100 kHz) — not `analogRead()`, which is software-timed and jittery (~25-40 kHz).
3. `repair_adc_glitches()` linearly interpolates across short runs of spurious exact-zero samples that the original ESP32's ADC digital controller emits at each hardware burst restart (a driver artifact, not real signal).
4. `send_data()` writes the header + raw `int16_t` payload straight to `Serial`.

Key tunables live at the top of `main.cpp`: `PING_CHIRP_START_HZ`/`PING_CHIRP_END_HZ`/`PING_CHIRP_STEPS`/`PING_CHIRP_CYCLES_PER_STEP`, `BRAKE_PULSE_US`, ADC resolution/attenuation/`ADC_SAMPLE_FREQ_HZ`, `SERIAL_BAUD`.

### PC receive path (`pc/receive.py`)

- Auto-detects the sonar's port by probing `SERIAL_PORT_CANDIDATES` (`/dev/ttyUSB0`, `/dev/ttyUSB1`) and checking which one responds correctly to a START command; set `SERIAL_PORT` to a literal path to skip this. Set to `None` by default because the enumerated port has been observed to change.
- Uses a `BufferedReader` wrapper around the serial port rather than one-byte-at-a-time reads — at 921600 baud, syscall-per-byte reading can't keep up and risks silent kernel receive-buffer overflow/desync.
- The packet-framing/resync state machine lives in `parse_next_event()`, a standalone function (takes a `BufferedReader`-like reader + a small state dict, returns a `ParseResult` describing what happened) kept separate from the serial/CSV I/O in `continuous_serial_to_csv()` specifically so it's unit-testable without a real serial port — see `pc/tests/test_receive_parse.py`.
- If packet framing desyncs (e.g. from a STOP/START race where an in-flight ping still transmits after STOP), the read loop resyncs on the next `0xAA 0xBB` sync bytes and logs a `ping_id` gap warning rather than writing corrupted rows.

### PC plotting scripts

- `plot_sensor_stream.py` — raw waveform per packet; x-axis converted from sample index to one-way range in cm using `SampleDt_us` and `SPEED_OF_SOUND_M_S`.
- **Range axis:** all plot scripts build it with `plot_sensor_stream.range_axis_cm()`, which adds the capture offset `T_sample_us − T_ping_us` (burst + brake + `AFTER_DELAY_US` + ADC startup, currently ~8.2 ms ≈ 142 cm) — sample 0 is *not* 0 cm, and targets closer than that offset are outside the window. Near-range blanking likewise blanks only samples whose true range is within the burst length (none, with current timing).
- `plot_matched_filter.py` — `make_ping_template()` reconstructs the *actual* transmitted burst shape (not an idealized sinusoid) and cross-correlates it against the raw signal to compress each echo into a sharp peak.
- `plot_echogram.py` — applies the matched filter across the whole recording and renders amplitude envelope (Hilbert transform) as a range-vs-time waterfall in dB (`DYNAMIC_RANGE_DB`), blanking the near-range region still ringing from the transmit burst.
- `plot_echogram_live.py` — same matched-filter/envelope waterfall as `plot_echogram.py`, but tails `sensor_stream.csv` as `receive.py` writes it and shows a live-scrolling fixed-width window (`WINDOW_PINGS`) instead of the whole finished recording. Run as a second process alongside `receive.py`; never touches the serial port. `--deconv` enables the Wiener stage live.
- `plot_lambda_sweep.py` — offline tuning aid for `WIENER_LAMBDA`: matched-filters + Wiener-deconvolves every ping at each λ in a list, averages the envelope over all pings (stationary reflections reinforce, noise averages down), and overlays one dB curve per λ with a marker at a known target range (`--target-cm`). Pick the smallest λ with a sharp target peak and a clean background.
- `run_pipeline.py` — chains `receive.py` → `plot_echogram.py` → `plot_matched_filter.py`, each stage starting once the previous exits.

### Ringdown deconvolution (`pc/deconvolve.py`)

A high-Q ringing element makes each echo ring for many cycles, which smears targets in range — a matched filter can't fix this because there's no bandwidth to compress. `deconvolve.py` estimates the reflectivity series by dividing the received spectrum by a model of the ringdown/settling impulse response `H(f)`, regularized (Wiener) so spectral nulls and out-of-band noise don't blow up. Pure numpy rfft/irfft — no scipy, mirroring `plot_echogram.hilbert_envelope`'s "reimplement the one transform we need" precedent.

- **Cascade:** `matched_filter` first (SNR + band-limiting), then `wiener_deconvolve` on its output against the matched-filter-of-`h` kernel (`domain='mf'`, the default). `domain='raw'` skips the matched filter and deconvolves the raw trace — measurably worse resolution here (the raw signal isn't band-limited, so the inverse filter smears more), kept only as an experimental knob.
- **Impulse-response model:** synthetic damped sinusoid `exp(-π·f0·t/Q)·sin(2π·f0·t)`, optionally convolved with the drive template. Tuned by two constants: `RINGDOWN_FREQUENCY_HZ` and `RINGDOWN_Q` (read off a strong single echo: Q ≈ π × cycles from the ring peak to the 1/e envelope point). This is a single lumped model regardless of how many physical causes (acoustic speaker ring, front-end amplifier settling) contribute to what the microphone observes. Currently f0 = 4400 Hz, Q = 9, fitted 2026-09-26 against the 8 Ω speaker + electret mic pair (per-ping fit of `conv(template, h)` to a single ~210 cm target's echo). Note the lumped echo response peaks at ~4400 Hz, above the speaker's ~4100 Hz bench resonance, and the chirp's two lowest steps (3280/3690 Hz) barely come back.
- **Regularization:** `WIENER_LAMBDA` (fraction of peak `|H|²`) trades ring-collapse against noise/clutter amplification. Currently 0.01, picked 2026-09-26 from a sweep on the same capture (target -6 dB width 12.1 cm vs 17.8 cm matched-filter-only, at ~3 dB higher background); the sweep table is in the `WIENER_LAMBDA` comment.
- **Bandwidth limit:** with a narrowband ringing element the fundamental range resolution is c/(2B). Deconvolution widens the *effective* band past the -3 dB points where SNR allows, so it visibly tightens the ring but does not produce a true spike.

**On a transducer swap**, re-tune together: `PING_CHIRP_START_HZ`/`PING_CHIRP_END_HZ` (firmware) + the matching constants in `plot_matched_filter.py` to the new resonance/band; `RINGDOWN_FREQUENCY_HZ` + `RINGDOWN_Q` (`deconvolve.py`) to the new ringdown; then re-run `plot_lambda_sweep.py` to re-pick `WIENER_LAMBDA`.

## Working notes

- This is a real physical hardware project with an admittedly fragile current build (transducer/preamp/wiring). When captured data looks physically implausible (noise, drift, gain, oscillation) but the serial framing/protocol layer is clean (`ping_id` continuity, no resync warnings), suspect the hardware/wiring before the firmware or PC code.
- Firmware and PC-side packet definitions (`PingHeader` in main.cpp, `HEADER_FORMAT` in receive.py) must be changed together — there's no shared schema file.
- **Transducer-dependent tunables must all track the fitted speaker/microphone pair's resonance/ringdown:** `PING_CHIRP_START_HZ`/`PING_CHIRP_END_HZ`/`PING_CHIRP_STEPS`/`PING_CHIRP_CYCLES_PER_STEP` (firmware `main.cpp`), the matching constants in `pc/plot_matched_filter.py` (must match firmware), and `RINGDOWN_FREQUENCY_HZ`/`RINGDOWN_Q`/`WIENER_LAMBDA` (`pc/deconvolve.py`). See [Ringdown deconvolution](#ringdown-deconvolution-pcdeconvolvepy) for the swap checklist.
- **Open question:** the 2026-09-26 tuning capture put a target the user estimated at ~250 cm at 209–214 cm by every method (matched filter, deconvolution, raw echo onset minus the filtered-out chirp steps). Either the estimate was off or there's a ~2.3 ms fixed delay unaccounted for — confirm with a tape measure before trusting absolute range.
- **`BRAKE_PULSE_US` and `AFTER_DELAY_US` are unverified placeholders** for the new single-ended speaker/microphone hardware — verify on a scope once hardware is on the bench.
