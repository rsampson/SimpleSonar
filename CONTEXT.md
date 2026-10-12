# SimpleSonar

An ESP32-based sonar front end and PC-side tooling for capturing and visualizing airborne ping/echo data.

## Language

**Speaker**:
The single-ended 8 Ω voice-coil output transducer, driven via GPIO 25 through an outboard 2 W amplifier.
_Avoid_: Transducer (ambiguous — could mean speaker or microphone), array (refers to the retired 11-element differential part).

**Microphone**:
The electret receive-side transducer, physically separate from the speaker.
_Avoid_: Transducer.

**Ping**:
One transmit burst plus the sampled window of reflected signal captured after it, identified by a monotonic `ping_id`.
_Avoid_: Pulse (used loosely elsewhere for the transmitted burst specifically; "ping" is the whole round-trip event).

**Burst**:
The drive waveform emitted by the speaker for one ping — currently a stepped chirp (discrete LEDC frequency steps), previously a fixed tone.
_Avoid_: Ping (a ping includes the receive window too, not just the transmitted signal).
