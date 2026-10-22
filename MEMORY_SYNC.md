# Memory sync (2026-10-21) — temporary, delete after applying

On another machine, ask Claude to apply this to its project memory, then delete this file.

Delete these memories if present: sonar_project_status.md, capture_window_offset.md, hardware_gain_issue.md, hardware_oscillation_fixed.md, wiener_deconvolution_stage.md.

Replace the remaining files with exactly the contents below.

## MEMORY.md

````markdown
- [Sonar Hardware Fragility](sonar_hardware_fragility.md) — build is fragile; past anomalies were all hardware; lead hardware-first when framing is clean
- [Preamp Actual Circuit](preamp_actual_circuit.md) — fitted AD828 preamp has no schematic; sonar_preamp/ is the kept TL081 design; sonar_amp is a different board
- [Signal-Chain Bench Plan](signal_chain_bench_plan.md) — bench/ Phase 1 done; Phase 2, bench skill, LISTEN command still open
````

## sonar_hardware_fragility.md

````markdown
---
name: sonar-hardware-fragility
description: "The SimpleSonar build is physically fragile; past anomalies (oscillation, low gain, drift) were all hardware, so lead with hardware hypotheses when framing is clean"
metadata:
  node_type: memory
  type: project
  originSessionId: 2e6cfe0c-de68-4ecd-99a8-05ccfb31c8fe
  modified: 2026-10-22T02:42:22.589Z
---

The user calls their sonar hardware construction "very poor" and works with it as-is. Past anomalies that were all hardware, not code:
- 2026-08-19: sustained full-window oscillation, fixed by shortening receive-circuit wires (antenna/feedback pickup).
- 2026-08-20: amplitude dropped to ~1.5% of full scale right after that wiring change. The cause was a low-sensitivity mic swapped in at the same time, not the wiring.
- A slow ~1 Hz DC drift across the whole buffer, never root-caused (supply/ground).

**Why:** These anomalies are much more likely to be hardware than firmware/PC bugs. The framing layer has been solid.
**How to apply:** If the data looks wrong but framing is clean (`ping_id` continuous, no resync warnings), suspect the hardware first. Any component swapped at the same time as a wiring change is a suspect too. Check the transducer/mic early.
````

## preamp_actual_circuit.md

````markdown
---
name: preamp-actual-circuit
description: "Fitted preamp is a two-stage AD828 (2.2k in / 20k fb, 1 nF across each 20k) with no schematic in the repo; hardware/sonar_preamp/ is the old TL081 design, kept on purpose"
metadata:
  node_type: memory
  type: project
  originSessionId: 2e6cfe0c-de68-4ecd-99a8-05ccfb31c8fe
  modified: 2026-10-22T02:42:23.642Z
---

The fitted preamp is not in any KiCad file. It uses both halves of an **AD828**, each an inverting stage with a 2.2 kΩ input and 20 kΩ feedback (the user calls it "10 each / 100"). It has **1 nF across each 20 kΩ** as an anti-alias filter (~8 kHz pole per stage). Measured figures are in CLAUDE.md (profile `ad828-v1`). The mic-bias network has since been modified (see the `bias3k6`, `biasRC`, `R2k2` bench profiles), so don't assume the TL081 netlist's bias values.

`hardware/sonar_preamp/` is the older **TL081** design. The user keeps it deliberately, so don't delete or "fix" it. `hardware/sonar_amp.*` (LM358 + TDA1308 + ESP32 mini) is a different board, not the AD828 preamp.

**Why:** Reading the TL081 files as the current build gives wrong gain and bandwidth figures.
**How to apply:** For questions about the fitted circuit, use the bench profiles and ask the user. Don't infer from the KiCad files. See [[signal-chain-bench-plan]].
````

## signal_chain_bench_plan.md

````markdown
---
name: signal-chain-bench-plan
description: "bench/ characterization Phase 1 is implemented and in use; Phase 2 (second-ESP32 generator), a bench skill, and an optional firmware LISTEN command remain open"
metadata:
  node_type: memory
  type: project
  originSessionId: 2e6cfe0c-de68-4ecd-99a8-05ccfb31c8fe
  modified: 2026-10-22T02:42:24.685Z
---

The approved plan (2026-10-13) is at `/home/richard/.claude/plans/pasted-content-id-40f1-you-are-eager-lantern.md`. Phase 1 (manual bench generator through a ÷1000 pad, with noise/step/sine/linearity/crosstalk tests) is built in `bench/` and has produced the `ad828-v1*` profiles.

Still open, not started:
- Phase 2: a second original ESP32 DevKit as the generator (DAC via I2S/DMA), synced to the sonar's GPIO25 ping output.
- Turning the bench procedures into a skill.
- OPTIONAL firmware `LISTEN` command (capture without a burst). Not decided.

The user has an oscilloscope. Calibrate by measuring the generator output before the pad and multiplying by the DMM-measured pad ratio.

**How to apply:** Re-read the plan file before starting any of the open items. Circuit facts are in [[preamp-actual-circuit]].
````
