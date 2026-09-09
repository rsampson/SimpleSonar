"""Wiener (regularized inverse) deconvolution of the transducer ringdown.

The 11-element voice-coil array is driven at its mechanical resonance
(~2750 Hz) where it is loud but very lightly damped: a ringdown capture
measured Q ~ 47, f0 ~ 2775 Hz, -3 dB bandwidth ~ 60 Hz, and the ring
falls to 10% only after ~34 cycles (~1200 of the 2048 samples). Every
echo is therefore true_reflectivity * h(t) with a long h(t), so returns
smear in range and adjacent targets merge into one ring train.

This module estimates the reflectivity series by dividing the received
spectrum by H(f), the transfer function of a synthetic damped-sinusoid
model of h(t), regularized (Wiener) so spectral nulls of H and
out-of-band noise don't blow up. It is cascaded *after* the matched
filter: matched_filter() supplies SNR and band-limiting, this collapses
the residual ring.

Pure numpy rfft/irfft - no scipy, mirroring plot_echogram.hilbert_envelope's
"reimplement the one transform we need" precedent.

Fundamental limit: with B ~ 60 Hz the bandwidth-limited range resolution
is c/(2B) ~ 2-3 m at the -3 dB points. Deconvolution widens the effective
processing band past those points only where SNR allows, so it visibly
tightens the ring but the gain is noise-limited.
"""

import numpy as np

from plot_matched_filter import make_ping_template, matched_filter

# --- Impulse-response model (synthetic) ------------------------------------
RINGDOWN_FREQUENCY_HZ = 2775.0   # measured array resonance (ringdown capture).
                                 # NOT PING_FREQUENCY_HZ (2750) - the array rings
                                 # at its own resonance regardless of the exact
                                 # drive tone.
RINGDOWN_Q = 47.0                # measured quality factor of the ringdown
                                 # envelope. h(t) ~ exp(-pi*f0*t/Q)*sin(2pi*f0*t):
                                 # amplitude decays to 1/e in Q/pi cycles
                                 # (~15 cycles, ~5.3 ms here). Re-estimate per
                                 # array: Q = pi * (cycles from ring peak to the
                                 # 1/e envelope point) read off a strong single
                                 # echo.
IMPULSE_RESPONSE_SPAN_CYCLES = 40.0  # ringdown cycles of h(t) to model. The array
                                     # rings ~34 cycles to 10%; 40 cycles captures
                                     # the part worth undoing (~-3.4 Np, ~3% left)
                                     # without an unbounded kernel. ~1450 samples
                                     # at sample_dt_us=10.
CONVOLVE_WITH_TEMPLATE = True    # convolve the bare ringdown with
                                 # make_ping_template() so h models the drive
                                 # burst *as reshaped by the ringing array* - what
                                 # a point reflector actually returns - matching
                                 # matched_filter's "the template is the real
                                 # transmitted burst" reasoning.

# --- Wiener regularization -----------------------------------------------
WIENER_LAMBDA = 3e-2   # constant noise-to-signal ratio, as a FRACTION of peak
                       # |H|^2 (dimensionless, independent of how h is scaled):
                       #   X_hat = Y * conj(H) / (|H|^2 + WIENER_LAMBDA*max|H|^2)
                       # caps inverse-filter gain at ~1/(2*sqrt(lambda)) and makes
                       # spectral nulls of H fall to 0, not infinity. Lower =
                       # sharper spike, noisier; higher = safer, less ring
                       # collapse.
                       #
                       # 3e-2 picked by visual sweep (plot_lambda_sweep.py, 450
                       # pings averaged, domain='mf') on a capture in a ~10x10 ft
                       # room, transducer angled at a wall ~275 cm away (slant
                       # range; the transducer is tilted). Chosen over 1e-2 for a
                       # visibly smoother between-target background at a
                       # near-identical target-peak width (~44 vs ~41 cm at
                       # -6 dB); 1e-1 was too strong (ring not undone, near-field
                       # blob dominates). CAVEAT: a cluttered small room is a poor
                       # air-sonar test platform - that capture's strongest return
                       # sat near the far edge of the 2048-sample window (~354 cm,
                       # room clutter / edge pile-up) rather than cleanly on the
                       # wall, so this is a sensible middle value, not a
                       # precisely-optimised one. Re-sweep against an isolated
                       # close reflector (2-3 ft, nothing else in view), or in a
                       # larger/quieter space, when a better setup is available.

# --- Toggle (single source of truth for all three call sites) -----------
DECONV_ENABLED = False  # default OFF; the plot scripts flip it via --deconv
DECONV_DOMAIN = 'mf'    # 'mf'  = deconvolve the matched-filter output (default)
                        # 'raw' = deconvolve raw values, skip the matched filter


def make_impulse_response(sample_dt_us, *, q=RINGDOWN_Q,
                          f0_hz=RINGDOWN_FREQUENCY_HZ,
                          span_cycles=IMPULSE_RESPONSE_SPAN_CYCLES,
                          convolve_with_template=CONVOLVE_WITH_TEMPLATE):
    """Return the array's echo impulse response h(t), sampled at the receiver rate.

    Synthetic second-order mechanical-resonator ringdown:

        h0(t) = exp(-pi * f0 * t / q) * sin(2 * pi * f0 * t)

    sampled at dt = sample_dt_us microseconds for
    n = round(span_cycles / f0 / dt) samples (floored at 1). With
    convolve_with_template=True it is then convolved (np.convolve, 'full')
    with make_ping_template(sample_dt_us) so h represents the gated drive
    burst *as reshaped by the ringing array* - i.e. the waveform a single
    point reflector actually sends back.

    Returned float64, peak-normalized so abs(h).max() == 1.0. Zero-mean is
    deliberately NOT enforced: a physical impulse response already has
    near-zero DC from the sinusoid, and forcing exact zero-mean would
    distort its low-frequency tail.
    """
    dt_s = sample_dt_us * 1e-6
    n = max(int(round(span_cycles / f0_hz / dt_s)), 1)
    t = np.arange(n) * dt_s
    h = np.exp(-np.pi * f0_hz * t / q) * np.sin(2 * np.pi * f0_hz * t)

    if convolve_with_template:
        template = make_ping_template(sample_dt_us)
        h = np.convolve(h, template, mode='full')

    peak = np.max(np.abs(h))
    if peak > 0:
        h = h / peak
    return h.astype(np.float64)


def _next_fast_len(n):
    """Smallest 5-smooth integer (2^a * 3^b * 5^c) >= n.

    numpy's FFT has no next_fast_len helper and is correct on any length,
    but prime or near-prime sizes hit its slow Bluestein path; rounding the
    zero-pad length up to a 5-smooth number keeps the rfft on the fast
    mixed-radix path.
    """
    if n <= 1:
        return 1
    best = None
    p2 = 1
    while p2 < n * 2:
        p23 = p2
        while p23 < n * 2:
            p235 = p23
            while p235 < n:
                p235 *= 5
            if best is None or p235 < best:
                best = p235
            p23 *= 3
        p2 *= 2
    return best


def wiener_deconvolve(x, h, *, lam=WIENER_LAMBDA):
    """Regularized inverse filter of a 1-D real signal.

        X_hat(f) = Y(f) * conj(H(f)) / ( |H(f)|^2 + lam * max|H(f)|^2 )

    `lam` is a fraction of peak |H|^2 (dimensionless), so the result is
    independent of how h is scaled and spectral nulls of H are handled
    gracefully: as |H(f)| -> 0 the ratio -> Y*conj(H)/(lam*max|H|^2) -> 0,
    not infinity. Worst-case inverse gain is ~1/(2*sqrt(lam)).

    Linear (non-circular) convolution semantics: x and h are zero-padded to
    nfft = _next_fast_len(len(x) + len(h) - 1) before the rfft, so the
    inverse filter can't wrap ring energy from the end of the trace around
    to the start.

    No group-delay offset is applied: as lam -> 0 the transfer function
    Y*conj(H)/|H|^2 -> Y/H, whose impulse response is a unit spike at lag 0,
    so a lone echo at input sample k deconvolves to a spike at output
    sample k. This requires h to be causal with its leading edge at index 0
    (make_impulse_response satisfies this; deconvolved_response trims its
    matched-filter kernel to its peak before calling here). Any residual
    rise time of h shows up as a few-sample lag, matching the physical
    onset delay.

    Returns float64, len == len(x). The last ~len(h) samples of the trace
    have truncated convolution support - acceptable, that region is beyond
    the usable display range anyway.
    """
    x = np.asarray(x, dtype=np.float64)
    h = np.asarray(h, dtype=np.float64)
    n = len(x)
    nfft = _next_fast_len(n + len(h) - 1)

    Y = np.fft.rfft(x, nfft)
    H = np.fft.rfft(h, nfft)
    mag2 = np.abs(H) ** 2
    peak = mag2.max()
    denom = mag2 + (lam * peak if peak > 0 else lam)
    X_hat = Y * np.conj(H) / denom

    return np.fft.irfft(X_hat, nfft)[:n].astype(np.float64)


def _matched_filter_of(h, template):
    """The point-target response the matched filter itself produces for h.

    Cross-correlates h against the template the same way matched_filter()
    does, then trims the acausal precursor so the kernel starts at its own
    peak: matched_filter's autocorrelation lobe is centred on the target
    lag, so the raw correlation has ~len(template)/2 of ramp-up before the
    peak. wiener_deconvolve() assumes a causal, peak-at-index-0 kernel, so
    that precursor is dropped here (it would otherwise advance the
    deconvolved trace off the range axis).
    """
    x = np.asarray(h, dtype=np.float64)
    x = x - x.mean()
    L = len(template)
    result = np.correlate(x, template, mode='full')
    kernel = result[L - 1:L - 1 + len(x)]
    return kernel[int(np.argmax(np.abs(kernel))):]


def deconvolved_response(values, sample_dt_us, *, template=None, h=None,
                         domain=DECONV_DOMAIN, lam=WIENER_LAMBDA):
    """Run the chosen deconvolution cascade; one call per packet for the scripts.

    domain='mf' (default): matched-filter for SNR, then Wiener-deconvolve
    its output against _matched_filter_of(h, template) - the point-target
    response the matched filter produces - so what remains is the collapsed
    reflectivity series.

    domain='raw': skip the matched filter, Wiener-deconvolve the
    mean-subtracted raw trace directly against h. Noisier (the raw signal
    is not band-limited); kept for experimentation.

    Returns float64, len == len(values), on the same range axis as
    matched_filter().
    """
    if template is None:
        template = make_ping_template(sample_dt_us)
    if h is None:
        h = make_impulse_response(sample_dt_us)

    if domain == 'raw':
        x = np.asarray(values, dtype=np.float64)
        x = x - x.mean()
        return wiener_deconvolve(x, h, lam=lam)

    mf = matched_filter(values, template)
    hk = _matched_filter_of(h, template)
    return wiener_deconvolve(mf, hk, lam=lam)
