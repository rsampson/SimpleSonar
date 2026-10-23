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

## Frequency sweep (transducers in place)

`freq_sweep.py` measures the two-way (transmit × receive) frequency response
with the transducers installed, from a reflector's echo. It doesn't need the
injection pad or the generator.

1. Put a large, flat, rigid board (at least 30 × 30 cm) about 2 m from the
   transducers. Square it to them and centre it on the array's beam, which is
   only about ±7 cm wide at 2 m in the plane along the array. Keep ~30 cm
   clear in range in front of and behind it: a floor or wall echo at nearly
   the same range adds ripple to the response.
2. Measure the distance to the board's face.
3. Keep the amp volume, the aim and the room fixed for the whole run (~4
   minutes at the defaults), with nobody in the beam.
4. Run:

   ```bash
   python bench/freq_sweep.py --profile sweep-YYYY-MM-DD --target-m 2.00
   ```

   Options: `--start`/`--stop`/`--step` in Hz (defaults 36000/44000/250),
   `--pings` per step (20), `--cycles` for the burst length (10), and
   `--reanalyze` to recompute from the saved captures.

   The 10-cycle burst spans ~3.6 kHz itself, so its curve is the response
   to the burst the sonar actually uses, smoothed by that width. Use
   `--cycles 50` (~0.7 kHz) in a separate profile to measure the
   transducers' own steady-state response. Its echo window is 1.25 ms
   longer, so keep anything else that reflects ~50 cm or more clear behind
   the board.

Each step's echo amplitude is read from the coherent mean, demodulated at
the drive frequency, in a window from 0.5 ms before to 2 ms after the
expected round trip. The direct sound near range is reported too, as a
rougher second estimate. The script prints the peak frequency, the −3 dB
band and the echo's leading-edge range at the peak (compare it with the
tape). It warns if any capture clipped: clipping flattens the peak, so back
off the volume or move the board further away and rerun.

Results go to `profile.json` (`freq_sweep`), `response.npz` (`sweep_*`) and
`freq_sweep.png`; raw captures to `raw/sweep_<hz>.csv`. At the end, even if
interrupted, the script sends `RESET`, which returns the firmware to
`PING_FREQUENCY` and `PING_CYCLES`. Reopening the port doesn't reliably reboot
the ESP32, so without that the last step's settings would carry over.

## Wi-Fi interference (branch `esp32-wifi-ap`)

`wifi_ab.py` checks whether the ESP32's Wi-Fi adds noise. Over USB it
alternates `WIFI OFF` and `WIFI ON` (`--rounds`, default 2) and captures
`--pings` (60) in each state, with the transducers in their normal setup.

While Wi-Fi is on, the radio has to carry real traffic. Join the tablet to the
open `SimpleSonar` network and read the bridge, e.g. in Termux (`pkg install
netcat-openbsd`):

```bash
nc 192.168.4.1 3333 > /dev/null
```

Keep it running for the whole test. Android often won't rejoin a network
without internet by itself once it drops, so rejoin `SimpleSonar` by hand
each time it reappears (once per round). The script waits up to 10 minutes
(`--client-wait`) for the client before each "on" capture. Don't
use starfish as the client: its Wi-Fi is its only internet connection.

```bash
python bench/wifi_ab.py --profile wifi-YYYY-MM-DD
```

It reports, Wi-Fi on against off, from the part of each capture that doesn't
repeat ping to ping, outside the sonar band (500 Hz-30 kHz and 50 kHz up):
the noise RMS, pings with spikes (an out-of-band peak over 1.5x the Wi-Fi-off
median per-ping peak, where transmit bursts would show), and spurs that only
appear with Wi-Fi on. Inside the band that residual is dominated by echoes
varying with the room, so the in-band and ping-locked levels are printed as
"scene" figures: someone standing near the sonar changes them. Results go to `profile.json` (`wifi_ab`), `response.npz` and
`wifi_ab.png` (noise spectra). Wi-Fi is left off.

This tests USB power. In the field the sonar runs on a battery, with separate
regulators for the preamp and the output amp, and the data goes over Wi-Fi.
To check that setup, capture over the bridge (`capture.open_sonar('socket://192.168.4.1:3333')`)
and compare with the USB Wi-Fi-off baseline.

## Output

`bench/profiles/NAME/`: `profile.json` (description, pad, test conditions and
results), `response.npz` (step/impulse/frequency response, noise spectrum,
crosstalk waveform), `raw/*.csv` (captures in `sensor_stream.csv` format, so
the plotting scripts in `pc/` read them too), and `report.md` with plots.

## Tests

`pytest bench/` runs the analysis against a synthetic preamp model (known
gain and corners, noise, ping-locked crosstalk) and an offline end-to-end run
of the CLI.
