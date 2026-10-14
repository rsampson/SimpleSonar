# Signal-chain characterization bench (`bench/`)

## Context

At the moment the sonar's captures mix three things that can't be told apart: the acoustic environment, the transducers, and the electronics (preamp + ADC). This makes it hard to know whether odd data comes from the room or from the electronics. It also makes transducer/preamp swaps a matter of re-tuning by eye. The goal is to inject *known* electrical stimuli where the microphone normally connects, measure what the ESP32 actually records, and save the result as a named **hardware profile** per build.

The current preamp, as corrected by the user (the KiCad files in `hardware/sonar_preamp/` are **out of date**):
- Both stages are now the two halves of an **AD828**. Each is an inverting stage with a 2.2 kΩ input resistor and a 20 kΩ feedback resistor, with **1 nF across each 20 kΩ** as an anti-alias filter. Everything else is assumed unchanged from the old netlist until confirmed: the mic bias (R7, 2.2 kΩ to 3.3 V), the coupling caps, and the ADC bias (R1/R2, about 1.16 V at GPIO34).
- Predicted figures for the tests to confirm or refute:
  - Gain is 20k/2.2k ≈ 9.1 per stage, so **≈ 83 (38.3 dB)** in total, not exactly 100.
  - Each 20k‖1 nF pair gives a low-pass pole at **≈ 8.0 kHz**. Two of them in cascade give a combined −3 dB point near **5.1 kHz**. At the 4.4 kHz operating band that means ≈ −2.3 dB and ≈ −58° phase. That is in-band shaping, and part of it may be included in the "lumped" 4.4 kHz ringdown model in `deconvolve.py`. At 50 kHz (Nyquist) the attenuation is ≈ −32 dB.
  - If the coupling caps are still 0.5 µF, each input high-pass corner moves to about 145 Hz (with 2.2 kΩ).
  - Input clipping is near **14 mV peak**, assuming the ADC is still biased at 1.16 V. The mic node's AC impedance is ≈ 1.1 kΩ (2.2 kΩ bias resistor in parallel with the 2.2 kΩ input resistor).

Decisions made with the user: the second ESP32 is an **original ESP32 DevKit** (has DACs). An **oscilloscope** is available for calibration. The bench generator comes first (Phase 1) and the ESP32 generator second (Phase 2). Profiles are stored as **JSON + a generated Markdown report**.

## Phase 1 — bench signal generator (build now)

### Injection hardware (documented in `bench/README.md`, not code)
- Remove the electret mic. Inject at the mic+ node through a **÷1000 pad**: 47 kΩ in series, 47 Ω shunt to GND, then a 10 µF DC-blocking cap. The pad's output impedance is ≈ 47 Ω, about 4% of the ≈ 1.1 kΩ node impedance. The generator's 0–10 V p-p becomes **0–10 mV p-p** at the node, which stays inside the ≈ 28 mV p-p linear range at full generator output. A second **÷100 tap** (4.7 kΩ series, same 47 Ω shunt) gives up to 100 mV p-p for the overload tests. The same pad will serve the Phase 2 DAC: 3.3 V full scale becomes 3.3 mV.
- **Calibration:** measure the generator output on the scope *before* the pad, at volt levels, so the reading is clean. Measure the pad resistors with a DMM to get the exact ratio. The tool multiplies the two to get the node amplitude. A second probe on the preamp output gives an independent gain check (that signal is volt-level too).
- Disconnect the speaker amp's input for the preamp-only tests. It gets reconnected for T4.

### Test procedures (each a subcommand of `run_test.py`)
For each test, the tool prints the exact generator settings, waits for Enter, captures N pings, saves the raw CSV, analyses it, and updates the profile.

| ID | Stimulus (user sets) | Measures |
|---|---|---|
| T0 `noise` | Input terminated (pad installed, generator output off/0 mV) | Noise floor: RMS in ADC counts and input-referred µV, spectrum, spurs (USB, supply) |
| T1 `step` | Square, 50 Hz, ~5 V p-p generator → ~5 mV p-p at node (2 edges per 20.48 ms capture) | Average the edges across pings (sub-sample alignment). Step response → derivative = **impulse response** → FFT = magnitude/phase response up to 50 kHz; −3 dB corners; settling time |
| T2 `sine` | Stepped sine list (≈100 Hz … 150 kHz, extra points around 3–8 kHz); amplitude fixed | Gain per frequency (cross-checks T1), THD, the shape of the 8 kHz anti-alias rolloff, and **residual aliasing** above 50 kHz (e.g. 60 kHz showing up at 40 kHz) |
| T3 `linearity` | 4.4 kHz sine stepped 1 → 10 V p-p on the ÷1000 tap, then the ÷100 tap up to 100 mV p-p at the node; then a large square wave | Gain compression, clip level, THD vs level, overload recovery time |
| T4 `crosstalk` | Input terminated, speaker amp reconnected, normal pinging | Purely electrical burst feedthrough plus preamp settling inside the capture window. This answers the open `AFTER_DELAY_US` question |

There is no clock sync with the bench generator. The analysis finds the edges and sine phases in each capture by itself, so no sync is needed. The sonar firmware and normal pinging stay unchanged.

### Profile output
`bench/profiles/<name>/` contains:
- `profile.json`: a free-text hardware description (mic, preamp revision, amp, speaker), date, git commit, test conditions, gain at the reference frequency, corners, noise floor, clip level, THD, crosstalk, settling time.
- `response.npz`: the impulse response and frequency-response arrays.
- `raw/<test>.csv`: raw captures in the same format as `sensor_stream.csv`, so they can be re-analysed later.
- `report.md` + PNG plots, regenerated from the JSON.

### Files (Phase 1)
| File | Change |
|---|---|
| `bench/README.md` | New: purpose, injection pad wiring diagram (ASCII), scope calibration points, step-by-step procedure for each test |
| `bench/capture.py` | New: captures N pings to a named CSV. **Reuses** `pc/receive.py`'s `find_sonar_port`, `BufferedReader`, `new_parse_state`, `parse_next_event`, `send_control_command` (imported via a `sys.path` insert). No changes to `pc/` |
| `bench/analyze.py` | New, pure numpy: edge detection + averaging, step→impulse→FFT response, −3 dB corners, sine fit/THD, noise RMS/spectrum, clip detection. **Reuses** `plot_sensor_stream.load_packets` and `plot_echogram.hilbert_envelope` |
| `bench/profile.py` | New: reads/writes `profile.json` + `response.npz`, renders `report.md` + plots (matplotlib, already a dependency) |
| `bench/run_test.py` | New: CLI (`--profile <name> noise\|step\|sine\|linearity\|crosstalk\|report`) that prompts for generator settings and the scope-measured amplitude, then capture → analyse → profile |
| `bench/tests/test_analyze.py` (+ `conftest.py`) | New: pytest on synthetic signals (a known two-pole high-pass + gain + noise model of the preamp; check that corners, gain and noise RMS are recovered within tolerance) |
| `bench/pyproject.toml` | New: pytest config (testpaths) so `pytest bench/` works; uses the existing `pc/.venv`, **no new dependencies** |
| `CLAUDE.md` | Add a short `bench/` section (commands, what profiles are) |
| `firmware/src/main.cpp` | **OPTIONAL:** a `LISTEN` command that captures without emitting a burst. This gives a cleaner T0 (the GPIO25 burst not running at all), but T0 still works without it, with the speaker amp disconnected |

## Phase 2 — ESP32 generator (plan in outline; detailed plan after Phase 1 data)
- `bench/generator_fw/`: a separate PlatformIO project for the second ESP32 DevKit. It plays waveforms through **DAC GPIO25 via I2S/DMA**: impulse, step, stepped/log-swept sine, an exact replica of the chirp, and MLS noise. GPIO edges are also available for fast steps. It is controlled from the PC over its own USB serial with commands distinct from START/STOP, so `receive.py`'s port probe never mistakes it for the sonar.
- **Sync:** the generator's input pin is wired to the sonar's GPIO25 ping output (common ground). It fires the stimulus a programmed number of µs after the first burst edge, so stimuli land at known positions inside the capture window. This allows coherent averaging over many pings, true impulse response with timing, and measured group delay. That delay feeds directly into the open **~2.3 ms range discrepancy** question.
- **Injection:** the same ÷1000 / ÷100 pad as Phase 1.
- `run_test.py` gains `--generator esp32`, which drives the generator automatically instead of prompting. `analyze.py`/`profile.py` are reused unchanged.
- After both phases are stable, **package the procedures as a skill** (`.claude/skills/characterize-signal-chain/`). Later, optionally, let `deconvolve.py` load the measured electronics impulse response from a profile instead of the synthetic model.

## Verification
1. `pytest bench/` — the synthetic-signal tests recover the known corners, gain and noise.
2. `pytest pc/` — still passes (`pc/` is untouched).
3. On the bench: run T0 and T1 against the current preamp and compare with the predictions (≈ 38.3 dB; low-pass corner ≈ 5.1 kHz combined; high-pass corners per the actual coupling caps). Check the gain at 1 kHz against the scope ratio between the generator output (divided by the pad ratio) and the preamp output.
4. Re-run `run_test.py report` on the saved raw CSVs and confirm the profile is regenerated identically.
5. If the OPTIONAL `LISTEN` command is included: `~/.platformio/penv/bin/pio run` must build cleanly.
