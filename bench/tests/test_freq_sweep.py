"""Synthetic sweep: a known two-way response must come back out of the analysis."""

import numpy as np
import pytest

import freq_sweep
from echo_onset import C

FS = 200_000.0
N = 4096
T0_MS = 1.24
TARGET_M = 2.0
F0, Q = 40_300.0, 15.0        # model two-way resonance
BIAS = 1810.0


def response(f):
    """Second-order band-pass magnitude, peak 1 at F0."""
    return 1 / np.sqrt(1 + Q ** 2 * (f / F0 - F0 / f) ** 2)


def pings(f, echo_amp, direct_amp=300.0, n=20, noise=2.0, seed=0, burst_ms=0.25, overshoot=0.0,
          precursor_amp=0.0, precursor_cm=5.0):
    """Phase-locked direct-sound ring-down plus a gated echo burst at the target's round trip,
    optionally with a weak copy of the echo (a precursor) precursor_cm nearer."""
    rng = np.random.default_rng(seed)
    t_ms = T0_MS + np.arange(N) / FS * 1e3
    direct = direct_amp * np.exp(-(t_ms - T0_MS) / 0.4) * np.sin(2 * np.pi * f * t_ms * 1e-3)
    if precursor_amp:
        x = pings(f, precursor_amp, 0.0, n, 0.0, seed, burst_ms, overshoot)
        direct = direct + np.roll(x[0] - BIAS, -int(round(2 * precursor_cm / 100 / C * FS)))
    t_echo = 2 * TARGET_M / C * 1e3
    rel = t_ms - t_echo
    # resonant transducers: exponential build-up over the burst, then ring-down
    tau = 0.08
    shape = np.where(rel < 0, 0.0, np.where(rel < burst_ms, 1 - np.exp(-rel / tau),
                                            (1 - np.exp(-burst_ms / tau)) * np.exp(-(rel - burst_ms) / tau)))
    # off resonance the start-up transient can overshoot the steady state
    transient = np.where(rel < 0, 0.0, overshoot * np.exp(-rel / 0.15) * (1 - np.exp(-rel / tau)))
    echo = (echo_amp / shape.max() * shape + echo_amp * transient) * np.sin(2 * np.pi * f * t_ms * 1e-3)
    clean = BIAS + direct + echo
    return np.round(clean + rng.normal(0, noise, (n, N)))


def test_echo_amplitude_and_position():
    p = freq_sweep.sweep_point(pings(40_000.0, 200.0), FS, T0_MS, 40_000.0, TARGET_M)
    half = freq_sweep.sweep_point(pings(40_000.0, 100.0, seed=1), FS, T0_MS, 40_000.0, TARGET_M)
    # the demodulator's smoothing rounds off a short echo's peak, equally at every
    # frequency, so the reading is proportional to the echo rather than equal to it
    assert 0.7 * 200 < p['echo_amp_counts'] <= 200
    assert p['echo_amp_counts'] / half['echo_amp_counts'] == pytest.approx(2.0, rel=0.05)
    assert abs(p['echo_peak_ms'] - 2 * TARGET_M / C * 1e3) < 0.6
    assert p['direct_amp_counts'] > 150    # direct sound read separately, near range
    assert p['clip_fraction'] == 0


def test_echo_gate_excludes_direct_sound():
    p = freq_sweep.sweep_point(pings(40_000.0, 0.0, direct_amp=1500.0), FS, T0_MS, 40_000.0, TARGET_M)
    assert p['echo_amp_counts'] < 5


def test_sweep_recovers_peak_and_band():
    freqs = np.arange(36_000.0, 44_001.0, 250.0)
    points = []
    for i, f in enumerate(freqs):
        p = freq_sweep.sweep_point(pings(f, 300.0 * response(f), seed=i), FS, T0_MS, f, TARGET_M)
        p['f_actual_hz'] = f
        points.append(p)
    s = freq_sweep.summarize(points)
    assert abs(s['peak_hz'] - F0) <= 250
    # -3 dB bandwidth of the model is F0 / Q
    assert s['bandwidth_3db_hz'] == pytest.approx(F0 / Q, rel=0.15)


def test_target_range_from_leading_edge():
    # noise near the measured room floor (quiet gap 7-14 counts, TEST_LOG 2026-10-14)
    rng_cm = freq_sweep.target_range_cm(pings(40_000.0, 200.0, noise=40.0), FS, T0_MS, TARGET_M)
    assert rng_cm == pytest.approx(TARGET_M * 100, abs=3)


def test_target_range_ignores_weak_precursor_at_high_snr():
    # strong echo, low noise, and a return at 2% of it 5 cm nearer: above
    # 8 sigma of the noise, but below a tenth of the echo's peak
    x = pings(40_000.0, 800.0, noise=0.5, precursor_amp=16.0)
    rng_cm = freq_sweep.target_range_cm(x, FS, T0_MS, TARGET_M)
    assert rng_cm == pytest.approx(TARGET_M * 100, abs=1.5)
    # without the peak-fraction floor the precursor sets the edge
    assert freq_sweep.target_range_cm(x, FS, T0_MS, TARGET_M, peak_fraction=0.0) < TARGET_M * 100 - 3


def test_long_burst_reaches_steady_state_inside_window():
    # 50 cycles (1.25 ms): the echo settles at full amplitude, and the window
    # stretches to cover the burst, so the reading is the steady-state level
    x = pings(40_000.0, 200.0, burst_ms=50 / 40_000.0 * 1e3)
    p = freq_sweep.sweep_point(x, FS, T0_MS, 40_000.0, TARGET_M, cycles=50)
    assert p['echo_amp_counts'] == pytest.approx(200.0, rel=0.05)


def test_long_burst_ignores_start_up_overshoot():
    x = pings(38_500.0, 200.0, burst_ms=50 / 38_500.0 * 1e3, overshoot=1.0)
    p = freq_sweep.sweep_point(x, FS, T0_MS, 38_500.0, TARGET_M, cycles=50)
    assert p['echo_method'] == 'settled mean'
    assert p['echo_amp_counts'] == pytest.approx(200.0, rel=0.05)
