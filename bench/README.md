# Signal-chain characterization bench

Injects known electrical signals where the microphone normally connects,
records what the ESP32 actually captures, and saves the results as a named
**hardware profile**. This separates the electronics (preamp + ADC) from the
room and the transducers. Plan: `hardware/signal_chain_characterization_plan.md`.

The sonar firmware runs unchanged and keeps pinging during every test. The
generator is not synchronised to it; the analysis estimates the part of each
capture that is locked to the ping (burst feedthrough, start-up transients)
as the average over pings and subtracts it, leaving the injected signal.

## Injection pad

Remove the electret mic. Build the divider so the generator's volts become
millivolts at the mic+ node:

```
                 ÷1000 tap                         ÷100 tap (overload tests only)
generator ──47 kΩ──┬──────── 10 µF ──► mic+ node   generator ──4.7 kΩ──┬── 10 µF ──► mic+ node
                   │  (+ side toward the node)                         │
                  47 Ω                                                47 Ω
                   │                                                   │
generator GND ─────┴──────────────────── sonar GND                   ──┴── GND
```

- Ratio ≈ 1/1001 (÷1000 tap) and 1/101 (÷100 tap). Measure the actual
  resistors with a DMM and enter them in `setup`; the tool uses the exact ratio.
- The pad's output impedance (≈47 Ω) is small next to the ≈1.1 kΩ mic node.
- Predicted input clipping is ≈14 mV peak (≈28 mV p-p): up to 10 V p-p on the
  ÷1000 tap stays linear; use the ÷100 tap to drive into clipping.

**Calibration:** measure the generator output on the scope *before* the pad
(volt levels read cleanly) and enter that p-p value when asked. Optionally put
a second probe on the preamp output as an independent gain check.

**Speaker amp:** disconnect its input for T0-T3; reconnect it for T4.

## Running

From the repo root, with the `pc/.venv` active (no extra dependencies):

```bash
python bench/run_test.py --profile NAME setup --description "AD828 preamp, rev ..." \
        --r-series 47000 --r-series-div100 4700 --r-shunt 47      # measured values
python bench/run_test.py --profile NAME step  --vpp 5.0           # T1
python bench/run_test.py --profile NAME sine  --vpp 5.0           # T2 (prompts per frequency)
python bench/run_test.py --profile NAME noise                     # T0
python bench/run_test.py --profile NAME linearity --freq 3300     # T3 (prompts per level)
python bench/run_test.py --profile NAME crosstalk                 # T4
python bench/run_test.py --profile NAME report                    # regenerate report.md + plots
python bench/run_test.py --profile NAME reanalyze                 # rerun analysis on saved raw captures
```

Run T1/T2 before T0 so the noise can be quoted input-referred (µV). Each
command prints the generator settings, waits for Enter, captures (60 pings, or
20 per sine point; `--pings` overrides), analyses and updates the profile.

| Test | Generator | Measures |
|---|---|---|
| T0 `noise` | off, pad installed | noise RMS (counts, µV input-referred), spectrum, spurs, ping-locked signal |
| T1 `step` | square 50 Hz, ~5 V p-p, ÷1000 | step response → impulse and frequency response, −3 dB corners, rise and settling |
| T2 `sine` | sine, fixed ~5 V p-p, ÷1000, stepped 100 Hz–150 kHz | gain per frequency, THD, anti-alias rolloff, residual aliasing above 50 kHz |
| T3 `linearity` | sine at the operating frequency, rising level, then ÷100 | compression, clip level, THD vs level |
| T4 `crosstalk` | off, speaker amp reconnected | burst feedthrough and settling inside the capture window |

Notes:
- The step response is averaged over ~9 ms windows, so its frequency
  resolution is ~110 Hz: the high corner is accurate, the low corner only
  coarse. T2's low-frequency points pin down the high-pass corner.
- Gains are in ADC counts per mV of input. "dB nominal" converts counts to
  volts with 3.3 V/4096, which the ESP32 ADC only approximates.

## Output

`bench/profiles/NAME/`: `profile.json` (description, pad, test conditions and
results), `response.npz` (step/impulse/frequency response, noise spectrum,
crosstalk waveform), `raw/*.csv` (captures in `sensor_stream.csv` format, so
the plotting scripts in `pc/` read them too), and `report.md` with plots.

## Tests

`pytest bench/` runs the analysis against a synthetic preamp model (known
gain and corners, noise, ping-locked crosstalk) and an offline end-to-end run
of the CLI.
