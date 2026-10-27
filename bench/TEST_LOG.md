# Test log

Hardware test sessions, newest first. Tests run on the test laptop; development
happens mostly on the office PC, which learns the results from this file (see
"Test results across machines" in `CLAUDE.md`). Each entry gives the hardware
state, what was captured and where, the results, decisions, and what is left open.
Durable findings also go in `bench/NOTES.md`; raw captures stay on the laptop
(`bench/profiles/*/raw/`, gitignored), so name their paths here.

---

## 2026-10-26 (starfish, host) — shared TX/RX: the transmit array as the receiver

**Hardware:** the 40R-12 receiver is out of the signal path. The transmit array
(DRV8871, 12 V) feeds a new single-stage LF356 preamp (see "Working notes" in
`CLAUDE.md`): OUT1 → 3.3 kΩ → antiparallel diodes → 3.3 kΩ → summing junction,
OUT2 → 3.3 kΩ, 1 MΩ feedback, gain ~100, + input on a 100k/100k virtual ground
with 10 µF. The output is capacitor-coupled to GPIO34 through 1 kΩ.
**Firmware:** the bridge now coasts (IN1 = IN2 = 0) between pings: 100 µs of
brake to wake the DRV8871, burst, brake `BRAKE_AFTER_US`, then coast. New
`DAMP <n>` command (anti-phase half-cycles after the burst, default 0).
**Page:** blanks below 40 cm and the matched filter's last template-length; the axis starts at 40 cm.

**Captures** (5–8 s over serial, analysed in the session, not kept): framing
clean throughout; burst to sampling 1251–1253 µs.

- **Wiring faults found:** with the diodes returned to ground the stage
  saturated (ADC flat at 1055 counts, no signal). With OUT2's 3.3 kΩ to ground,
  the network recharged the array through the summing junction after each brake.
  That put a DC step of about +1.3 V at the start of sampling (τ ≈ 0.3 ms). Both
  now go to virtual ground, and the step is down to +0.6 V at 22 cm and gone by 30 cm.
- **Coast timing:** `bridge_coast()` via `pinMode()` took ~200 µs (sampling at
  1453 µs); it is now a register write. With `BRAKE_AFTER_US` = 500 the ADC sat
  flat at 4095 until the DRV8871 slept (~31 cm); at 100 that flat stretch is gone.
- **Near-range overload:** 40 kHz content clips the ADC to ~30–35 cm and stays
  at 400–500 counts to ~43 cm (no target). That makes the bright band at
  40–50 cm that tops the echogram. A target at 1 ft showed up only as this band
  at ~1.5 ft; its true echo falls in the overload.
- **Active damping didn't help:** with DAMP 0–8, only DAMP 1 helped, a little at
  40–45 cm (1421 → 1023 counts); higher values were worse, and the 2–3 m echo
  fell from 10186 to 7500. Beyond 42 cm the ping-locked residue is a steady
  46.0 kHz tone (sub-kHz resolution) decaying with τ ≈ 3.5 ms. That is far
  longer than the transducer's ringing, and damping at 40 kHz can't reach it.
  `CYCLES 2` cut real echoes by 7–10 dB but the band by only 4–6 dB, and the
  clipping still ran to 37 cm. The band seems to be set off by the switching steps, not stored burst energy.
- **Noise** (before the bias fixes): ~22 counts RMS past 3 m, with tones near
  24 kHz and 46–48 kHz. The old chain had ~1.3 counts. Not re-measured since.

**Open items:**
- Scope the LF356 output (DC-coupled, before the coupling cap; trigger on
  GPIO25, 200 µs/div): find the source of the overload and of the 46 kHz tone.
  It could be a real ring, an alias of ~154 kHz, or a mechanical mode; try a
  finger or foam on the array face and on the mount.
- Target at 2 ft (61 cm) to confirm range accuracy beyond the overload.
- Re-measure noise with the fixed bias; crosstalk/noise bench run in coast.
- `DAMP` kept for further tuning, but it defaults to 0.

## 2026-10-24 (starfish, host) — DRV8871 H-bridge drive replaces the audio amplifier

**Hardware:** DRV8871 breakout, VM = 11.36 V (no measurable sag during a
burst), IN1/IN2 from GPIO25/26. 10 Ω in series with each output, then the
10-element 40T-12 array (12.5 nF). Preamp and 40R-12 receiver unchanged.
Sonar on starfish's USB power.
**Firmware:** complementary burst timed off the CPU cycle counter, the bridge
held in brake (IN1 = IN2 = 1) between bursts, `TONE` on the LEDC with IN2 inverted.

**Scope (user):**
- OUT1 and OUT2 are in antiphase, with OUT1 high first.
- About 21 V swing across the array, against 2 × VM = 22.7 V.
- A very brief undershoot on the falling edges, about 4 V. It is probably the probe ground lead.
- Both outputs brake to 0 V after the burst, and both GPIOs are high between bursts. No glitches on the GPIOs.
- Bursts are 300 ms apart, and the array's ringing is gone by about 150 µs.
- Not yet checked: that each burst is exactly 10 cycles.

**Mishap:** the scope ground clip on the array's OUT1 side shorted OUT1 to
ground through its 10 Ω resistor during `TONE`. That is 1.2 A, about 7 W average
in a 1/4 W part, and the resistor burned open: the echo dropped to the noise.
The DRV8871 was undamaged (1.2 A is below its 3.7 A overcurrent trip). With the
resistor replaced, the echo came back to the same strength as before, so both
halves of the bridge still work. Keep the ground clip on circuit ground and use
CH1 − CH2 for the voltage across the array.

**Captures** (15 s each, 50 pings, analysed in the session, not kept): framing
clean, 300.0 ms period, burst-to-sampling 1251/1252 µs (1255 on the first ping
after START only). No clipping; raw 1463–2154.

| | old amp (`wifi_off_0/1`, 10-22) | DRV8871 |
|---|---|---|
| out-of-band noise RMS | 2.35 / 2.36 counts | 1.31 |
| out-of-band peak per ping (median) | 27.9 / 27.1 | 10.8 |
| ping-locked (scene) | 7.3 / 8.1 | 26.4 |

The ~27-count out-of-band peak in every ping (open since 10-22) is gone with
the amplifier. It most likely came from the amplifier, though that isn't proven.
The low signal seen late on 10-22 (±70 counts) no longer applies: echoes are now ±300 counts and up.

**Sweeps** (`sweep-drv8871-2026-10-24`, `-50cyc`; target 139.7 cm, 55 in;
10-22's were at 141.0 cm):

| | peak | echo at peak | −3 dB band | leading edge |
|---|---|---|---|---|
| 10 cycles, old amp | 39.74 kHz | 158.8 counts | 3520 Hz | +0.5 cm (8σ) |
| 10 cycles, DRV8871 | 39.75 kHz | 676.5 (+12.6 dB) | 3530 Hz | +0.2 cm |
| 50 cycles, old amp | 40.00 kHz | 278.3 | 1764 Hz | +0.5 cm (8σ) |
| 50 cycles, DRV8871 | 39.75 kHz | 723.7 (+8.3 dB) | 2470 Hz | −3.7 cm |

- **Gain:** +8 to +13 dB, against +9.3 dB expected from 22.7 V vs 7.8 V peak-to-peak.
- **Leading-edge fix:** `freq_sweep.py` first read −3.1 cm (10 cycles). The cause
  was a weak return 3–7 cm before the echo, which now rises above the 8σ noise
  threshold. The threshold is now the higher of 8σ and 10% of the echo's peak
  (`ONSET_PEAK_FRACTION`), and both profiles were reanalysed. 10-22's profiles
  weren't reanalysed; by the new rule their 10-cycle edge would read 142.4 cm (+1.4).
- **50-cycle early echo:** in the 50-cycle run the echo itself starts rising at
  ~135 cm (49% of peak by 137 cm), so it isn't a precursor. The target may have
  been closer during that run, or a reflection may build up only with a long
  burst. It needs repeating.
- **Echo tail:** at 10 cycles the echo falls to 20% of its peak by 148 cm, where
  the old amp's was still at 79%. That fits the user's echogram showing very
  little dead zone beyond ~0.5 ft. The cause is not separated from geometry yet.

**Echogram:** `plot_echogram_live.py` looked very good, with little dead zone
beyond ~0.5 ft. The ESP32's web echogram ran on the tablet (1 station, 1 web client).

**Unexplained:** once, before the first sweep, the ESP32 stopped sending
anything and didn't answer commands. A reset pulse over RTS fixed it (normal
POWERON boot). It didn't recur over ~10 minutes of sweeps with the tablet connected.

**Decisions (user):** keep the DRV8871 drive. Crosstalk test left open.

## 2026-10-22 (starfish, host) — web echogram on the tablet (branch `esp32-wifi-ap`)

**Setup:** firmware brings up the `SimpleSonar` AP and starts pinging at boot.
It serves the echogram page at `http://192.168.4.1` and sends pings over the
`/ws` WebSocket. Sonar on starfish's USB power. Android tablet browser as the
client.

**Results:**
- **The page ran on the tablet for a long time, then froze.** Over USB, the
  firmware was barely running: no pings in 10 s, and `WIFI STATUS` took 14 s
  to answer, with a bridge client connected. Cause: the Termux `nc` loop from
  the A/B test was frozen by Android in the background, so its TCP connection
  stayed open but unread. `WiFiClient::write()` retries for up to 10 s per
  call, and the bridge write runs in the ping loop.
- **Fix:** bridge writes are non-blocking with a 250 ms limit, and a client
  that can't keep up is dropped. Reproduced deliberately (`nc` then Ctrl+Z on
  the tablet): the page kept scrolling, and USB showed 3.3-3.4 pings/s with no
  gaps over 20 s. The bridge client was dropped and the web client stayed
  connected.
- **Off the tablet:** the page was also checked in headless Chrome on starfish
  via `pc/web_preview.py`: 148 pings in 40 s, none missed, no JS errors,
  Stop/Start round trip, portrait layout. `pc/tests/test_web_page_dsp.py` runs
  the page's DSP under Node and matches the Python tools.

## 2026-10-22 (starfish, host) — Wi-Fi interference A/B (branch `esp32-wifi-ap`)

**Hardware:** as in the sweep entry below, but the target was removed and the
user was near the sonar during the run. Sonar powered from starfish's USB.
Firmware: `WIFI ON|OFF|STATUS`, which runs an open AP `SimpleSonar` with a TCP
bridge on 192.168.4.1:3333.

**Setup:** the Android tablet was the Wi-Fi client, reading the bridge in
Termux (`while true; do nc 192.168.4.1 3333 > /dev/null; sleep 1; done`).
starfish can't be the client: its Wi-Fi is its only internet connection.
Android doesn't rejoin a network without internet by itself after the AP
drops, so the tablet was rejoined by hand every round.

**Captures** (`bench/profiles/wifi-2026-10-22/raw/`): `wifi_off_0`,
`wifi_on_0`, `wifi_off_1`, `wifi_on_1`, 60 pings each. Analysis:
`bench/wifi_ab.py`; plot `wifi_ab.png`.

**Results: no measurable interference on USB power.**
- **Out-of-band noise** (non-repeating residual, 0.5-30 and 50-99 kHz): 2.35
  and 2.36 counts RMS off, 2.33 and 2.28 on (−0.19 dB). The noise spectrum
  matches across 1-100 kHz.
- **Spikes and spurs:** 0/120 pings with spikes in either state. Worst peak
  37.1 counts off, 37.3 on. No new out-of-band spurs.
- **Timing:** burst-to-sampling 1243-1244 µs in all states; echo phase jitter
  26°/30° off, 35°/29° on (≈2 µs; one outlier ping in `wifi_on_0`).
- **Echoes weaker during the "on" captures** (ping-locked 7.3/8.1 off vs
  5.1/5.1 on; per-ping in-band 8.4/9.7 vs 6.6/6.1): the scene. The user was
  next to the sonar handling the tablet, with the target gone.
- **Every ping has a ~27-count out-of-band peak** that varies ping to ping.
  It's the same with Wi-Fi off; not investigated.
- **Tool fix:** the first analysis flagged spikes in every ping and an in-band
  "spur" at 39.06 kHz, because varying echoes dominate the in-band residual.
  The checks now use the out-of-band residual, and the profile was reanalyzed.

**Decision (user):** proceed to stage 2 (the web server on the ESP32's AP).

## 2026-10-22 (starfish, host) — two-way frequency response sweeps

**Hardware:** as in the conversion entry below. Transducers side by side, as for
echo work. A large flat board 55.5 in (141.0 cm) away, squared and centred on the
beam. Firmware gains `FREQ <hz>` and `CYCLES <n>`; the burst is timed in ns.

**Captures** (`bench/profiles/<profile>/raw/sweep_<hz>.csv`):
- `sweep-2026-10-22`: 36-44 kHz in 250 Hz steps, 20 pings per step, normal
  10-cycle burst.
- `sweep-2026-10-22-50cyc`: the same sweep with a 50-cycle burst (steady state).

No clipping in either. Analysis: `bench/freq_sweep.py`; plots `freq_sweep.png`
in each profile.

**Results:**
- **Steady state (50 cycles):** peak 40.00 kHz; −3 dB band 39.21-40.97 kHz
  (1.76 kHz); −6 dB band 38.0-41.3 kHz. The upper side falls steeply (−40 dB at
  44 kHz). The lower side has a shoulder at 38.0-38.5 kHz (−4 dB), perhaps
  array elements resonating low.
- **10-cycle burst:** peak 39.74 kHz; 40.0 kHz is −0.4 dB. −3 dB band 37.7-41.2 kHz
  (3.5 kHz), wider than steady state because the 250 µs burst spans ~3.6 kHz
  itself.
- **Ranging at 40 kHz:** the echo's leading edge read 141.5 cm vs 141.0 cm on the
  tape (+0.5 / +0.6 cm in the two sweeps).
- **Tool fix:** the sweep first read long-burst echoes at their maximum, which at
  some frequencies caught the start-up overshoot (peaks at 8.5 ms instead of
  ~9.3 ms). Long bursts are now read as the settled mean, and the 50-cycle
  profile was reanalyzed.

**Decisions (user):** keep the 40.0 kHz drive. Chirp and Wiener deconvolution are
not worth revisiting. A two-way bandwidth of ~1.8 kHz limits resolution to
c/2B ≈ 10 cm whatever the waveform, and the 10-cycle burst already reaches it.
The echo is strong (~150 counts at 1.4 m with 10 cycles).

## 2026-10-22 (starfish, host) — conversion to 40 kHz piezo transducers

**Hardware:** transmit is an 11-element array of 40T-12 piezos (~25 mm average
spacing, staggered unevenly to blur sidelobes), driven through the audio amp;
receive is a single 40R-12. Preamp: the AD828's 20 kΩ feedback caps went from
1 nF to 130 pF (~61 kHz pole per stage), and the electret bias network was
removed. The 40R-12 sees the 2.2 kΩ input resistor directly.
Firmware (commits `42ebfd8`, `345fd9b`, `08c9812`): 40 kHz, 10-cycle burst,
ADC at 200 kHz, 4096 samples/ping (~3.5 m), 300 ms interval; new `TONE` command
(continuous 40 kHz on GPIO25).

**Captures:** none kept. The runs went to `pc/sensor_stream.csv`, which each run
overwrote, plus session scratch files.

**Results:**
- **First run, no signal:** noise ~1 count RMS (ADC floor), flat spectrum, no
  direct sound. Cause: the speaker amp's power (fixed by the user). The same
  symptom as the 2026-10-14 amp-supply failure.
- **Amp powered:** spectrum peak 40.04 kHz; ping-locked ~10 counts RMS against
  1.8 counts residual; raw ±75 counts. Direct path at 20-40 cm; echoes at
  ~255 cm (+31 dB) and ~300 cm.
- **After the user fixed a preamp fault** (found with `TONE` and a scope): raw
  ±360 counts (1458-2177). Direct path +19 dB; strongest returns at 280-360 cm
  (+24 dB); 240-260 cm +12 dB.
- **ADC at 200 kHz:**
  - The pair un-swap is still correct: the 60 kHz image is −27.5 dB re 40 kHz
    (a wrong swap would put it at +9.8 dB).
  - With the zero-glitch repair disabled, there were 0 exact-zero samples in 96
    raw pings, so the repair was removed.
- **Start-of-run corrupted packet: solved.** `receive.py` slept 0.1 s after START
  before reading. With 8 KB packets, ~4.7 KB of buffering overflowed and one byte
  dropped; that misaligned packet 1 and destroyed packet 2's sync. The START
  before the read loop now has no settle sleep (also in `bench/capture.py`).
  0 bad packets since.
- **Live echogram dimming: fixed.** A strong close return (pings 334-340, raw
  223-3620) was 28 dB above typical. The all-time-peak scaling then held
  everything else 22-29 dB down. Scaling is now to the visible window's peak;
  checked live by the user.

**Decisions (user):** 200 kHz sampling rather than 150 kHz. 150 kHz isn't a whole
number of µs per sample, and its Nyquist sits near the preamp poles. Remove the
glitch repair. A MOSFET-input buffer ahead of the preamp is deferred; see how the
2.2 kΩ load performs first.

**Last observation:** the final recordings swung only 1733-1886 (±70) against
±360 after the preamp fix. Possibly just the aim/scene; not investigated.

## 2026-10-14 (laptop) — two-target resolution test; Wiener decision

**Hardware:** speaker amp on its own 5 V supply (it died twice during the session
and was repaired); preamp supply filtered; mic and speaker clamped side by side.
Firmware: master (phase-locked burst, 300 ms interval, ADC pair-swap fix).

**Captures** (`bench/profiles/two-target/raw/`): flat wood boards side by side at
77 in and 93 in (40.6 cm apart in range), squared to the transducers. Runs:
`none1`, `AB_sq`, `A_sq`, `B_sq`, `none2`. The earlier `A_77`, `B_93`, `AB_93`,
`none`, `A_77_sq` runs are invalid (boards not square, then the amp supply failed).

**Results** (`bench/two_target_eval.py`, `bench/WIENER_EVALUATION.md`):
- The plain matched filter already separates the two boards (valley −12.9 dB).
  Wiener is no better (−12.2 dB at λ=0.01); λ=0.003 deepens the valley but adds
  spurious peaks on single-board runs.
- The A+B capture is not the sum of A and B (residual as large as either echo):
  shadowing, multipath or drift.

**Decision (user):** Wiener deconvolution isn't worth the added complexity. Don't
merge `add-wiener-deconvolution`; move on to other tests.

**Lessons:** when the speaker amp's supply fails, the ping-locked signal vanishes
(direct sound 3-15 counts against 260-460 when working) while the ADC still shows
noise. Check the direct-sound level before trusting a run. Square the targets.

## 2026-10-14 (laptop) — Wiener evaluation on single targets

The branch's `pc/deconvolve.py` (run with our template) narrows echo peaks
(41→21 cm on the 100 in capture) but halves SNR on 77 in, adds pre-ringing, and
doesn't fix the peak position. Details in `bench/WIENER_EVALUATION.md`.

## 2026-10-14 (laptop) — target echo ranging at 77 in and 100 in

**Hardware:** as above. **Captures:** `bench/profiles/echo-77in/raw/target.csv`;
`bench/profiles/echo-100in/raw/{target,none}.csv`.

**Results** (`bench/echo_onset.py`, leading edge at 5σ):
- 77 in (195.6 cm): onset 195.0 cm (−0.6 cm). Echo 358 counts, quiet gap 7-14,
  direct sound 568.
- 100 in (≈254 cm, target − none): onset 254.9 cm (+0.9 cm). Echo 183 counts. DC
  matched exactly (1810.6) between runs.
- Leading edge is the range marker; the envelope/matched-filter peak is ~20 cm late.
- With the quiet front end, a target run alone is enough beyond ~1.2 m. Closer
  targets need a back-to-back no-target run (DC within ~1 count, nobody in the beam).

## 2026-10-13/14 (laptop) — bench characterization and front-end fixes

Profiles `bench/profiles/ad828-v1*` (summary in `bench/NOTES.md`): preamp
gain/bandwidth, the ADC DMA pair swap (fixed in firmware), supply isolation
(slow transient 767 → 12 counts, noise 22 → 5.8 counts RMS), direct-path pulse.
Fixes found with the user: speaker amp supply disconnected, a loose resistor.

## 2026-10-12 (laptop) — first end-to-end tests in the garage

Mixed results, mostly superseded. Lessons: baselines recorded minutes apart
drift as much as an echo, so record target and no-target runs back to back and
check the DC match. Read the leading edge, not a peak. Pings 74 ms apart let room
reverberation pile up, hence the 300 ms interval.

---

## Open items (update as they close)

- ADC frame-boundary glitches every 256 samples (−10..−20 counts, ping-locked):
  consider a firmware fix. Measured at 100 kHz; not rechecked at 200 kHz.
- Bench T3 linearity (clip level): needs the injection pad back.
- A third target distance (~1 m, where the echo overlaps the direct-sound tail).
- Re-profile the preamp with the 130 pF caps (bench defaults are now 40 kHz /
  35-45 kHz); `ad828-v1` predates the change.
- 40 kHz ranging: verified at 141 cm (+0.5 cm, old amp) and 139.7 cm (+0.2 cm,
  DRV8871, 10 cycles). Other distances are still to check, and
  `bench/echo_onset.py`'s timing windows were sized for the 3.3 kHz burst.
- The 50-cycle DRV8871 sweep's echo started ~4 cm early (2026-10-24): repeat it
  with the target position checked.
- MOSFET-input buffer for the 40R-12 (2.2 kΩ load costs ~6-9 dB per the datasheet).
- Wi-Fi on battery power (separate regulators for preamp and H-bridge): repeat
  the A/B with data over the bridge.
- Long unattended tablet run of the web echogram since the bridge-write fix.
- DRV8871: `run_test.py ... crosstalk` after the wiring change (edge-current
  spikes); scope-check that a burst is exactly 10 cycles.
- The ESP32 went silent once on 2026-10-24 until reset: watch for a recurrence.
- Stage 2 of the H-bridge drive: anti-phase damping cycles (`DAMP <n>`). The
  faster echo tail seen with the bridge may leave less to gain.
- `bench/run_test.py` and `bench/README.md` prompts still say "speaker amp".
