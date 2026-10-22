# Measurement notes

Findings moved out of CLAUDE.md (2026-10-21). Profiles referenced are in `bench/profiles/`.

## Ping interval (300 ms)

At the ~74 ms the serial transfer alone allows, the garage's reverberation from earlier
pings piled up under later echoes (a 162 cm target visibly changed the signal at 60 cm,
before any echo could arrive). At 300 ms that pre-echo contamination dropped ~8x.

## Direct-path speaker→mic pulse (`ad828-v1-mic-direct100`)

Mic 50 in and 100 in from the speaker. The received pulse is ~3.2 kHz with a
~2.9-3.5 kHz -3 dB band and Q≈7: it builds over ~1.7 ms (≈ the 1.82 ms, 6-cycle burst)
and decays to 1/e in ~0.7 ms, so the burst length, not transducer ringing, sets range
resolution. Arrival times agreed with the 345.8 m/s prediction to within the
tape-measure uncertainty (~±5 cm ≈ ±150 µs), so there is no large fixed delay in the
timestamps/range axis — but this is not a ~20 µs calibration. With the mic clamped,
arrival jitter is ~2 µs.

The captures were in a closed, cluttered garage with a hard surface ~20 in behind the
mic, so the pulse-shape figures (Q, ring-down) are rough: the strongest return in the
2026-10-14 100 in capture, 2.95 ms after the first arrival, is exactly the 2×20 in extra
path from that surface.

The slow (<300 Hz) transient early in every ping is electrical coupling, not acoustic
(it persists with the mic 2.5 m away).

## Transducer history

The old piezo was narrowband (Q~14), which is why the cycle count was kept small and a
stepped-chirp drive was reverted; that implementation is on the `chirp-drive` branch
and may be worth revisiting with the lower-Q speaker.

## ADC DMA pair swap

The original ESP32's ADC DMA delivers each sample pair swapped. Found on the bench and
confirmed by the image amplitude tracking tan(2πf/fs) from 100 Hz to 15 kHz: it put
every sample up to one sample period from its true time (alternating), added an image of
every tone at fs/2 − f, and attenuated the fundamental by cos(2πf/fs). Captures recorded
before the firmware fix (everything before 2026-10-14) have the swap; bench profile data
was corrected offline.

## Supply isolation (`ad828-v1*`, 2026-10-14)

With the mic-bias resistor fitted, the speaker amp sharing the ESP32's VIN rail put a
767 counts RMS slow transient (−1178..+1979 counts) on the preamp output after every
burst; the amp on its own supply cut it to ~12. A further ~4-7x drop in per-ping noise
and ping-locked pickup (22 → 5.8 and 18 → 2.9 counts RMS) came from filtering the
preamp's supply (22 Ω + 100 µF + 0.1 µF at its pins) and joining the grounds at a single
point.

## Echogram background subtraction

Without `--background`, a target at ~2.5 m was lost in static clutter in the echogram
(the 77 in target was visible without it).
