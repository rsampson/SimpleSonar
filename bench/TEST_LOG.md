# Test log

Hardware test sessions, newest first. Tests run on the test laptop; development
happens mostly on the office PC, which learns the results from this file (see
"Test results across machines" in `CLAUDE.md`). Each entry gives the hardware
state, what was captured and where, the results, decisions, and what is left open.
Durable findings also go in `bench/NOTES.md`; raw captures stay on the laptop
(`bench/profiles/*/raw/`, gitignored), so name their paths here.

---

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
  consider a firmware fix.
- Start-of-run corrupted packet (`ping_id` gap) in `receive.py`: uninvestigated.
- Bench T3 linearity (clip level): needs the injection pad back.
- A third target distance (~1 m, where the echo overlaps the direct-sound tail).
