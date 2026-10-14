# Wiener deconvolution vs the plain matched filter (2026-10-14)

Question: does the PC-side Wiener deconvolution on `origin/add-wiener-deconvolution`
(`pc/deconvolve.py`) beat the plain matched filter on this hardware?

Method: `python bench/wiener_eval.py`. The branch's `deconvolve.py` is run with *our*
6-cycle 3.3 kHz tone template (not the branch's chirp template) on two clean captures
with a known target range: 77 in (195.6 cm, target run alone) and 100 in (254 cm,
target minus a back-to-back no-target run). Parameters swept: resonance 3300/4400 Hz,
Q 5/9, regularization lambda 0.001-0.1. Metrics, in cm against the known range: peak
offset, -6 dB width of the echo peak, peak / std of the quiet gap ("snr"), and the
5-sigma onset offset.

| | 77 in: width / snr / onset | 100 in: width / snr / onset |
|---|---|---|
| plain matched filter | 38 cm / 11.1 / -15 | 41 cm / 18.3 / -15 |
| Wiener, lambda 0.01 (3300, Q5) | 34 / 5.9 / n.a. | 21 / 20.1 / -28 |
| Wiener, lambda 0.003 | 21 / 6.5 / -6 | 8 / 21.4 / -29 |
| Wiener, lambda 0.001 | 18 / 7.2 / -8 | 6 / 21.5 / -29 |

Findings:
- Wiener narrows the echo peak. How much depends strongly on lambda and on the
  capture: 41 -> 21 cm at lambda 0.01 on the 100 in case, but only 38 -> 34 cm on the
  77 in case.
- At small lambda the peak collapses to 6-8 cm. The system bandwidth (~600 Hz at -3 dB)
  limits real range resolution to roughly c/2B = 29 cm, so widths well below that are
  more likely deconvolved noise and ringing than resolved structure.
- On the 77 in capture (no background subtraction) the SNR roughly halves (11 -> 6-7).
- Neither method improves where the peak lands: the strongest feature is +11..+14 cm
  past the target on 100 in, and the echo peak is always later than its leading edge.
- Wiener makes the 5-sigma onset *earlier* on the 100 in case (-29 vs -15 cm): it adds
  pre-ringing ahead of the echo. The raw coherent-mean leading edge (`echo_onset.py`)
  measured +-2 cm with no deconvolution, so Wiener is no help for ranging.

Verdict: no verified advantage yet. It sharpens echo peaks, but the one claim that
would justify merging (it separates targets the matched filter cannot) is untested, and
it costs SNR and adds pre-ringing. A two-target test would settle it: two flat targets
~30-40 cm apart in range, matched filter vs Wiener on the same capture.
If it passes, bring only `pc/deconvolve.py` (+ tests, `--deconv` flags) over as an
opt-in, default off, with parameters retuned to this hardware (about 3300-4400 Hz,
Q 5-9, lambda 0.01-0.03) - not the branch's firmware, which conflicts with master's
phase-locked burst, 300 ms interval and ADC pair-swap fix.
