---
status: proposed
---

# Single-ended stepped-chirp drive replaces differential fixed-tone burst

Swapping to an 8 Ω speaker driven single-ended by an outboard 2 W amplifier (with a separate electret microphone for receive) removes the two-terminal differential pair the prior drive design assumed, so `emit_differential_burst()`'s two-pin/inverted-output mechanism no longer applies. Because this speaker is expected to be far more wideband/lower-Q than either prior transducer, we're reviving the `chirp-drive` branch's approach — a stepped LEDC frequency sweep (±20% around the measured 4100 Hz resonance) — rather than reverting to a fixed tone, since a wideband speaker can actually benefit from the matched filter's autocorrelation compression. We chose to adapt the branch's discrete-step LEDC mechanism (rather than build a buffered/bit-banged continuous sweep) for lower implementation risk, since this is an active experiment that may be reverted if it doesn't perform well on the bench. The chirp replaces the fixed-tone burst outright — not a runtime toggle — matching this project's existing convention of encoding drive-topology choices as separate branches/commits rather than runtime options.
