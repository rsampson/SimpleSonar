"""Synthetic-signal tests: a known preamp model must come back out of the analysis."""

import csv

import numpy as np
import pytest

import analyze

FS = 100_000.0
N = 2048
OVERSAMPLE = 16
GAIN_V_PER_V = 83.0
F_HP = 145.0      # each of two first-order high-pass poles
F_LP = 8000.0     # each of two first-order low-pass poles
COUNTS_PER_MV = GAIN_V_PER_V * 1e-3 / (3.3 / 4096)
BIAS = 1440.0
NOISE = 3.0


def H(f):
    """Model preamp, counts per mV of input."""
    s = 1j * np.asarray(f, dtype=float)
    hp = (s / F_HP) / (1 + s / F_HP)
    lp = 1 / (1 + s / F_LP)
    return COUNTS_PER_MV * hp ** 2 * lp ** 2


def model_corners():
    f = np.logspace(0, 5, 20000)
    return analyze.corners_3db(f, np.abs(H(f)))


def crosstalk_waveform():
    t = np.arange(N) / FS
    return 200 * np.exp(-t / 3e-3) + 60 * np.exp(-t / 0.4e-3) * np.sin(2 * np.pi * 3300 * t)


def make_pings(stimulus_period_s, stimulus_one_period_mv, n_pings, rng):
    """Pass a periodic input (one period, sampled at FS*OVERSAMPLE) through H,
    then sample N points per ping at a random phase, as an unsynced generator."""
    spec = np.fft.rfft(stimulus_one_period_mv)
    f = np.fft.rfftfreq(len(stimulus_one_period_mv), 1 / (FS * OVERSAMPLE))
    out = np.fft.irfft(spec * H(f), len(stimulus_one_period_mv))
    period = len(out)
    xt = crosstalk_waveform()
    pings = []
    for _ in range(n_pings):
        start = rng.integers(period)
        idx = (start + np.arange(N) * OVERSAMPLE) % period
        pings.append(BIAS + out[idx] + xt + rng.normal(0, NOISE, N))
    return np.round(np.array(pings))


def sine_pings(f_hz, amp_mv_pk, n_pings, rng):
    xt = crosstalk_waveform()
    t = np.arange(N) / FS
    h = H(f_hz)
    return np.round(np.array([BIAS + xt + amp_mv_pk * abs(h) * np.sin(2 * np.pi * f_hz * t + np.angle(h) + rng.uniform(0, 2 * np.pi))
                              + rng.normal(0, NOISE, N) for _ in range(n_pings)]))


def test_noise_floor_and_ping_locked_part_are_separated():
    rng = np.random.default_rng(1)
    x = np.round(np.array([BIAS + crosstalk_waveform() + rng.normal(0, NOISE, N) for _ in range(60)]))
    r = analyze.analyze_noise(x, FS)
    assert r['rms_counts'] == pytest.approx(NOISE, rel=0.1)
    assert r['ping_locked_rms_counts'] == pytest.approx(crosstalk_waveform().std(), rel=0.05)


def test_step_recovers_gain_and_high_corner():
    rng = np.random.default_rng(2)
    period_samples = int(FS * OVERSAMPLE / 50)                  # 50 Hz square
    step_mv = 5.0
    square = np.where(np.arange(period_samples) < period_samples // 2, step_mv / 2, -step_mv / 2)
    x = make_pings(1 / 50, square, 60, rng)
    r = analyze.analyze_step(x, FS, step_mv)
    lo, hi, peak, _ = model_corners()
    assert r['edges'] > 60
    assert r['peak_gain_counts_per_mv'] == pytest.approx(peak, rel=0.05)
    assert r['high_corner_hz'] == pytest.approx(hi, rel=0.08)
    # the 9 ms window limits low-frequency resolution to ~110 Hz: only a coarse check
    assert r['low_corner_hz'] == pytest.approx(lo, rel=0.5)


@pytest.mark.parametrize('f_hz', [1000.0, 4400.0, 20000.0])
def test_sine_gain_matches_model(f_hz):
    rng = np.random.default_rng(3)
    x = sine_pings(f_hz, 2.5, 20, rng)
    r = analyze.analyze_sine(x, FS, f_hz, 2.5)
    assert r['gain_counts_per_mv'] == pytest.approx(abs(H(f_hz)), rel=0.03)
    assert r['thd_pct'] < 1.0
    assert not r['aliased']


def test_sine_above_nyquist_is_measured_at_its_alias():
    rng = np.random.default_rng(4)
    f_true, f_alias = 60000.0, 40000.0
    t = np.arange(N) / FS
    amp = 2.5 * abs(H(f_true))
    x = np.round(np.array([BIAS + amp * np.sin(2 * np.pi * f_alias * t + rng.uniform(0, 6.3)) + rng.normal(0, NOISE, N)
                           for _ in range(20)]))
    r = analyze.analyze_sine(x, FS, f_true, 2.5)
    assert r['aliased']
    assert r['f_observed_hz'] == pytest.approx(f_alias, abs=20)
    assert r['gain_counts_per_mv'] == pytest.approx(abs(H(f_true)), rel=0.1)


def test_clipping_is_detected():
    rng = np.random.default_rng(5)
    x = np.clip(sine_pings(1000.0, 30.0, 10, rng), 0, 4095)
    r = analyze.analyze_sine(x, FS, 1000.0, 30.0)
    assert r['clip_fraction'] > 0.05
    assert r['thd_pct'] > 5


def test_crosstalk_reports_ping_locked_signal():
    rng = np.random.default_rng(6)
    x = np.round(np.array([BIAS + crosstalk_waveform() + rng.normal(0, NOISE, N) for _ in range(60)]))
    r = analyze.analyze_crosstalk(x, FS, t0_ms=2.7)
    assert r['rms_counts'] == pytest.approx(crosstalk_waveform().std(), rel=0.05)
    assert r['rms_counts_below_300hz'] > r['rms_counts_in_band']
    # the 3.3 kHz component decays with a 0.4 ms time constant from 60 counts
    assert 2.7 < r['in_band_below_floor_after_ms'] < 6


def test_load_capture_drops_corrupted_packets(tmp_path):
    path = tmp_path / 'cap.csv'
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Timestamp', 'PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us'] + [f'Int_{i}' for i in range(4)])
        w.writerow(['t', 1, 1000, 3700, 4, 10, 1, 2, 3, 4])
        w.writerow(['t', 2, 2000, 4700, 4, 10, 1, -30000, 3, 4])
        w.writerow(['t', 3, 3000, 5700, 4, 10, 5, 6, 7, 8])
    metas, x, fs, t0 = analyze.load_capture(str(path))
    assert [m['PingID'] for m in metas] == [1, 3]
    assert fs == 100_000
    assert t0 == pytest.approx(2.7)
